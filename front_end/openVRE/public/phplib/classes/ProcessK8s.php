<?php

namespace OpenVRE;

use Monolog\Logger;

/**
 * ProcessK8s — Kubernetes batch Jobs and interactive sessions.

 *
 *
 * Batch Jobs are sent to the scheduler as the template "openvre-batch-job" plus
 * parameters (see scheduler/templates/). The Job's user, volumes, node placement
 * and security settings are set by the scheduler, not here.
 *
 * Environment variables consumed:
 *   OPENVRE_K8S_NAMESPACE       — target namespace (required)
 *   OPENVRE_K8S_JOB_IMAGE       — fallback tool image when the tool sets none (required, may be empty)
 *   OPENVRE_K8S_SHARED_PVC      — PVC for /shared_data (required; also the session fallback PVC)
 *   OPENVRE_K8S_SCHEDULER_URL   — base URL of the scheduler service (required)
 *   OPENVRE_K8S_SCHEDULER_TOKEN — Bearer token for the scheduler (required)
 *   OPENVRE_K8S_JOB_TTL         — seconds to keep a finished Job (default 120)
 *   OPENVRE_K8S_JOB_DEADLINE    — max seconds a Job may run (default 86400)
 *   OPENVRE_K8S_JOB_MEMORY_REQUEST / _LIMIT — memory when the tool sets none (else template default)
 *
 * Interactive sessions are sent as a session template ("openvre-web-session",
 * "openvre-desktop-session" or the tool's infrastructure.k8s.template) plus the
 * tool's parameters and OpenVRE's context. Volumes, user/group and node placement
 * for both Jobs and sessions are set by the scheduler (OPENVRE_JOB_RUN_AS_UID/GID,
 * DEFAULT_USERDATA_PVC, ...).
 */
class ProcessK8s
{
    /** Prefix for batch Job names in Kubernetes (OpenVRE pid). */
    public const BATCH_JOB_PREFIX = 'openvre-bj-';

    private Logger $logger;

    /** Kubernetes Job name, also used as the OpenVRE "pid" */
    private string $pid = "";

    /** Path to the submission script inside the Job pod */
    private string $command = "";

    /** Working directory inside the Job pod */
    private string $workDir = "";

    /** Human-readable job identifier (used to derive the K8s name) */
    private string $jobname = "";

    /** Full API URL used for submission (for logging/debugging) */
    private string $fullcommand = "";

    /** Captured stdout from the submission response */
    private string $stdout = "";

    /** Captured stderr / error message from submission */
    private string $stderr = "";

    /** Kubernetes namespace where Jobs are created */
    private string $namespace;

    /** Container image used for the Job pod */
    private string $jobImage;

    /** PVC name mounted at /shared_data (shared between frontend, sge, and jobs) */
    private string $sharedPvc;

    /** Base URL of the scheduler HTTP service */
    private string $schedulerUrl;

    /** Bearer token sent to the scheduler for authentication */
    private string $schedulerToken;

    /** Seconds to keep a finished Job before auto-deletion */
    private int $jobTtl;

    /** Max seconds a Job is allowed to run before being killed */
    private int $jobDeadline;

    /** Interactive session URL returned by the scheduler */
    private string $accessUrl = "";


    /** Additional environment variables to inject into the Job pod (from tool Mongo definition) */
    private array $jobEnv = [];

    /** Image pull secret names for the Job pod (from tool infrastructure.image_pull_secrets) */
    private array $jobPullSecrets = [];

    /** Maps Kubernetes Job condition strings to OpenVRE internal state labels */
    private array $jobState = [
        "Running"   => "RUNNING",
        "Pending"   => "PENDING",
        "Succeeded" => "FINISHING",
        "Failed"    => "ERROR",
        "NotFound"  => "FINISHING",
    ];

    // ---------------------------------------------------------------
    // Constructor
    // ---------------------------------------------------------------

    /**
     * When $cl is a script path, builds and submits a Kubernetes Job immediately.
     * When $cl is false, creates a status-only instance (getRunningJobInfo, status, stop).
     *
     */
    public function __construct(false|string $cl = false, string $workDir = "", string $jobname = "", int $cpu = 1, int $mem = 0, array $jobOptions = [])
    {
        $this->logger = LoggerFactory::getLogger("K8s interface");

        $this->namespace = $this->env("OPENVRE_K8S_NAMESPACE");
        $this->jobImage = $this->env("OPENVRE_K8S_JOB_IMAGE");
        $this->sharedPvc = $this->env("OPENVRE_K8S_SHARED_PVC");
        $this->schedulerUrl = rtrim($this->env("OPENVRE_K8S_SCHEDULER_URL"), "/");
        $this->schedulerToken = $this->env("OPENVRE_K8S_SCHEDULER_TOKEN");
        $this->jobTtl = (int)$this->optionalEnv("OPENVRE_K8S_JOB_TTL", "120");
        $this->jobDeadline = (int)$this->optionalEnv("OPENVRE_K8S_JOB_DEADLINE", "86400");

        // Allow per-tool overrides:
        // container image and extra environment variables via $jobOptions.
        if (!empty($jobOptions["image"])) {
            $this->jobImage = $jobOptions["image"];
        }
        if (!empty($jobOptions["env"]) && is_array($jobOptions["env"])) {
            $this->jobEnv = $jobOptions["env"];
        }
        if (!empty($jobOptions["image_pull_secrets"]) && is_array($jobOptions["image_pull_secrets"])) {
            $this->jobPullSecrets = array_values(array_filter(array_map("strval", $jobOptions["image_pull_secrets"])));
        }

        // If a submission script path was provided, immediately create and submit
        // the Kubernetes Job. When $cl is false, this instance is used only for
        // status queries (getRunningJobInfo, status, stop).
        if ($cl) {
            $this->workDir = $workDir;
            $this->command = $cl;
            $this->jobname = $jobname ? $jobname : basename($cl);
            $this->runCom($cpu, $mem);
        }
    }



    private function env(string $name): string
    {
        $value = getenv($name);
        if ($value === false) {
            throw new \RuntimeException("Environment variable {$name} is not set");
        }
        return $value;
    }

    private function optionalEnv(string $name, string $default = ""): string
    {
        $value = getenv($name);
        return ($value === false) ? $default : $value;
    }

    public static function isBatchJobPid(string $pid): bool
    {
        return str_starts_with($pid, self::BATCH_JOB_PREFIX);
    }

    private function sanitizeName(string $name): string
    {
        $prefix = self::BATCH_JOB_PREFIX;
        $maxSlugLen = 63 - strlen($prefix) - 9; // "-{8-char hash}"

        $name = strtolower($name);
        $name = preg_replace('/[^a-z0-9-]+/', '-', $name);
        $name = trim($name, '-');
        if ($name === "") {
            $name = "job";
        }
        if (strlen($name) > $maxSlugLen) {
            $name = substr($name, 0, $maxSlugLen);
            $name = rtrim($name, '-');
        }

        return $prefix . $name . "-" . substr(md5(uniqid("", true)), 0, 8);
    }


    /** Name of the scheduler template batch runs use (scheduler/templates/openvre-batch-job.yaml). */
    public const BATCH_TEMPLATE = 'openvre-batch-job';

    /** Submits the Job to the scheduler as a template name plus parameters. */
    private function runCom(int $cpu, int $mem): void
    {
        // Abort if no container image is configured — there's nothing to run.
        if ($this->jobImage === "") {
            $this->stderr = "OPENVRE_K8S_JOB_IMAGE is not set";
            $_SESSION['errorData']['Error'][] = $this->stderr;
            return;
        }

        if ($this->schedulerUrl === "") {
            $this->stderr = "OPENVRE_K8S_SCHEDULER_URL is not set";
            $_SESSION['errorData']['Error'][] = $this->stderr;
            return;
        }

        // Generate a unique, K8s-safe Job name and use it as the OpenVRE "pid".
        $jobName = $this->sanitizeName($this->jobname);
        $this->pid = $jobName;

        // The scheduler builds the Job from its template. User, volumes, node
        // placement and security settings come from the template and scheduler
        // config; only these parameters come from here.
        $this->fullcommand = "POST " . $this->schedulerUrl . "/v1/jobs (template " . self::BATCH_TEMPLATE . ")";
        $this->logger->info("K8s job submission via scheduler '" . $this->fullcommand . "'");
        $response = $this->schedulerRequest("POST", "/v1/jobs", array(
            "namespace" => $this->namespace,
            "template" => self::BATCH_TEMPLATE,
            "params" => $this->buildJobParams($jobName, $cpu, $mem),
        ));

        // Handle submission result.
        if ($response["ok"] !== true) {
            $this->stderr = $response["error"];
            $this->stdout = "";
            $msg = "K8s job submission failed: " . trim($this->stderr);
            $this->logger->error($msg);
            $_SESSION['errorData']['Error'][] = $msg;
            $this->pid = "";
        } else {
            $this->stdout = isset($response["data"]["stdout"]) ? (string)$response["data"]["stdout"] : "";
            $this->stderr = isset($response["data"]["stderr"]) ? (string)$response["data"]["stderr"] : "";
            $this->logger->info("K8s job submitted: " . $this->pid . ". Output: " . trim($this->stdout));
        }
    }


    /**
     * Parameters for the openvre-batch-job template. The scheduler checks each
     * one against the type the template declares.
     */
    private function buildJobParams(string $jobName, int $cpu, int $mem): array
    {
        $params = array(
            "job_name" => $jobName,
            "image" => $this->jobImage,
            "workdir" => $this->workDir,
            "submit_script" => $this->command,
            "cpu" => $cpu > 0 ? (string)$cpu : "1",
            "deadline_seconds" => $this->jobDeadline > 0 ? $this->jobDeadline : 86400,
            "ttl_seconds" => max(0, $this->jobTtl),
        );

        // Memory: the tool's own value, else the deployment default, else the template default.
        $memRequest = $mem > 0 ? ($mem . "Gi") : $this->optionalEnv("OPENVRE_K8S_JOB_MEMORY_REQUEST");
        $memLimit = $mem > 0 ? ($mem . "Gi") : $this->optionalEnv("OPENVRE_K8S_JOB_MEMORY_LIMIT");
        if ($memRequest !== "") {
            $params["memory_request"] = $memRequest;
        }
        if ($memLimit !== "") {
            $params["memory_limit"] = $memLimit;
        }

        // Tool variables (infrastructure.container_env). Sent only when there are
        // some: an empty PHP array would encode as a JSON list, not a mapping.
        $env = array();
        foreach ($this->jobEnv as $envName => $envValue) {
            if ((string)$envName === "" || $envValue === null || $envValue === "") {
                continue;
            }
            $env[(string)$envName] = (string)$envValue;
        }
        if (!empty($env)) {
            $params["env"] = $env;
        }
        if (!empty($this->jobPullSecrets)) {
            $params["image_pull_secrets"] = $this->jobPullSecrets;
        }
        return $params;
    }


    private function schedulerRequest(string $method, string $path, ?array $payload = null): array
    {
        if ($this->schedulerUrl === "") {
            return array("ok" => false, "error" => "OPENVRE_K8S_SCHEDULER_URL is not set");
        }
        if (!function_exists("curl_init")) {
            return array("ok" => false, "error" => "PHP curl extension is required");
        }

        $url = $this->schedulerUrl . $path;
        $ch = curl_init($url);
        $headers = array("Content-Type: application/json");

        // Authenticate to the scheduler with a Bearer token.
        if ($this->schedulerToken !== "") {
            $headers[] = "Authorization: Bearer " . $this->schedulerToken;
        }

        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
        curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
        curl_setopt($ch, CURLOPT_TIMEOUT, 30);
        if ($payload !== null) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($payload));
        }

        $raw = curl_exec($ch);
        if ($raw === false) {
            $err = curl_error($ch);
            curl_close($ch);
            return array("ok" => false, "error" => "Scheduler request failed: " . $err);
        }
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        $json = json_decode($raw, true);

        // Non-2xx status from the scheduler indicates an error.
        if ($code < 200 || $code >= 300) {
            $msg = is_array($json) && isset($json["error"]) ? $json["error"] : ("HTTP " . $code . " from scheduler");
            return array("ok" => false, "error" => $msg);
        }
        if (!is_array($json)) {
            return array("ok" => false, "error" => "Invalid JSON response from scheduler");
        }
        // The scheduler returns {"ok": false, "error": "..."} on application-level errors.
        if (isset($json["ok"]) && !$json["ok"]) {
            return array("ok" => false, "error" => isset($json["error"]) ? $json["error"] : "Scheduler error");
        }
        return array("ok" => true, "data" => $json);
    }

    /**
     * Status of a batch Job. An empty array means "finished": OpenVRE then
     * collects outputs and stops polling. So an empty array is returned only
     * when the scheduler says the Job is gone or done. If the scheduler cannot
     * be reached, the Job is reported as still pending, so a scheduler restart
     * does not finalize running jobs with partial outputs.
     */
    public function getRunningJobInfo(string $pid): array
    {
        if ($this->isInteractivePid($pid)) {
            return $this->interactiveRunningInfo($pid);
        }
        if (!$pid) return array();

        $response = $this->schedulerRequest("GET", "/jobs/" . rawurlencode($pid) . "?namespace=" . rawurlencode($this->namespace));

        if ($response["ok"] !== true) {
            $this->logger->warning("K8s job $pid: status unknown, scheduler error: " . $response["error"]);
            return $this->unknownStateInfo($pid);
        }
        if (empty($response["data"]["exists"])) {
            return array();
        }

        $state = $response["data"]["status"]["state"] ?? $this->stateFromRawJob($response["data"]["job"] ?? "");
        if ($state === "Succeeded" || $state === "Failed") {
            if ($state === "Failed") {
                $reason = $response["data"]["status"]["reason"] ?? "";
                $this->logger->warning("K8s job $pid failed" . ($reason !== "" ? ": $reason" : ""));
            }
            return array();
        }

        return array(
            "pid" => $pid,
            "state" => $this->jobState[$state] ?? "PENDING",
            "job_name" => $pid,
        );
    }

    /** For schedulers older than v2, which return only the raw Job. */
    private function stateFromRawJob($jsonRaw): string
    {
        $json = is_string($jsonRaw) ? json_decode($jsonRaw, true) : null;
        $status = (is_array($json) && isset($json["status"]) && is_array($json["status"])) ? $json["status"] : array();
        if (!empty($status["active"])) return "Running";
        if (!empty($status["succeeded"])) return "Succeeded";
        if (!empty($status["failed"])) return "Failed";
        return "Pending";
    }

    /** Keeps monitoring a job or session while its real state cannot be read. */
    private function unknownStateInfo(string $pid): array
    {
        return array(
            "pid" => $pid,
            "state" => "PENDING",
            "job_name" => $pid,
            "scheduler_unreachable" => true,
        );
    }


    /** Returns the full API URL used for the last submission (for debug logging). */
    public function getFullCommand()
    {
        return $this->fullcommand;
    }

    public function getPid()
    {
        return $this->pid;
    }

    public function getErr()
    {
        if ($this->stderr) {
            return trim($this->stdout . " " . $this->stderr);
        }
        return null;
    }


    /**
     * Whether the submission worked. Called by execJob() right after submitting.
     * A Job that already finished was still submitted, so that counts as true;
     * so does a scheduler that accepted the Job but cannot answer the follow-up check.
     */
    public function status()
    {
        if (!$this->pid) return false;

        $response = $this->schedulerRequest("GET", "/jobs/" . rawurlencode($this->pid) . "?namespace=" . rawurlencode($this->namespace));

        if ($response["ok"] !== true) {
            $this->logger->warning("K8s job {$this->pid}: submitted, but status check failed: " . $response["error"]);
            return true;
        }
        return !empty($response["data"]["exists"]);
    }


    public function stop(?string $pid = null): array
    {
        if ($pid && $this->isInteractivePid($pid)) {
            return $this->stopInteractive($pid);
        }
        if (!$pid) {
            return array(false, "No job id '$pid' given");
        }

        $response = $this->schedulerRequest("DELETE", "/jobs/" . rawurlencode($pid) . "?namespace=" . rawurlencode($this->namespace));

        if ($response["ok"] === true) {
            $res = isset($response["data"]["stdout"]) ? trim((string)$response["data"]["stdout"]) : "";
            return array(true, $res);
        }
        return array(false, $response["error"] ?: "Failed to delete kubernetes job");
    }

    private function isInteractivePid(string $pid): bool
    {
        return strncmp($pid, "openvre-ix-", 11) === 0;
    }

    private function sanitizeSegment($value, $fallback = "tool", $maxLen = 24)
    {
        $value = strtolower((string)$value);
        $value = preg_replace('/[^a-z0-9_-]+/', '-', $value);
        $value = trim($value, '-');
        if ($value === "") {
            $value = $fallback;
        }
        return substr($value, 0, $maxLen);
    }

    private function resolveWorkspaceMountPath(array $infra)
    {
        if (!empty($infra['workspace_mount_path'])) {
            return trim((string)$infra['workspace_mount_path']);
        }
        if (empty($infra['volumes']) || !is_array($infra['volumes'])) {
            return "";
        }
        foreach ($infra['volumes'] as $hostKey => $containerPath) {
            $hostKeyNorm = "/" . trim((string)$hostKey, "/");
            if (preg_match('#(^|/)repository$#', $hostKeyNorm)) {
                continue;
            }
            if (is_array($containerPath)) {
                $containerPath = isset($containerPath['path'])
                    ? (string)$containerPath['path']
                    : (isset($containerPath['container_path']) ? (string)$containerPath['container_path'] : "");
            }
            if (is_string($containerPath) && $containerPath !== "") {
                return $containerPath;
            }
        }
        return "";
    }

    /** host path = root_dir/project + volume key (project-level mounts). */
    private function resolveVolumeMounts(array $infra, $rootDir, $project)
    {
        $mounts = array();
        if (empty($infra['volumes']) || !is_array($infra['volumes'])) {
            return $mounts;
        }
        $rootDir = rtrim((string)$rootDir, "/");
        $project = trim((string)$project, "/");
        if ($rootDir === "" || $project === "") {
            return $mounts;
        }
        foreach ($infra['volumes'] as $hostKey => $containerPath) {
            if (!is_string($hostKey) || $hostKey === "") {
                continue;
            }
            $readOnly = false;
            if (is_array($containerPath)) {
                $readOnly = !empty($containerPath['readOnly']) || !empty($containerPath['read_only']);
                $containerPath = isset($containerPath['path'])
                    ? (string)$containerPath['path']
                    : (isset($containerPath['container_path']) ? (string)$containerPath['container_path'] : "");
            }
            if (!is_string($containerPath) || $containerPath === "") {
                continue;
            }
            $hostKeyNorm = "/" . trim($hostKey, "/");
            if (!$readOnly && preg_match('#(^|/)repository$#', $hostKeyNorm)) {
                $readOnly = true;
            }
            $hostPath = preg_replace('#/+#', '/', $rootDir . "/" . $project . $hostKeyNorm);
            $mount = array(
                "host_path" => $hostPath,
                "container_path" => $containerPath,
            );
            if ($readOnly) {
                $mount["read_only"] = true;
            }
            $mounts[] = $mount;
        }
        return $mounts;
    }

    private function ensureProjectUserDirs($rootDir, $project, $workingDir, array $volumeMounts)
    {
        $rootDir = rtrim((string)$rootDir, "/");
        $project = trim((string)$project, "/");
        if ($rootDir !== "" && $project !== "") {
            foreach (array("uploads", "rstudio_data", "repository") as $leaf) {
                $dir = $rootDir . "/" . $project . "/" . $leaf;
                if (!is_dir($dir)) {
                    if (!@mkdir($dir, 0775, true)) {
                        return "Cannot create project directory: " . $dir;
                    }
                    @chmod($dir, 0775);
                }
            }
        }
        if ($workingDir !== "" && !is_dir($workingDir)) {
            if (!@mkdir($workingDir, 0775, true)) {
                return "Cannot create run directory: " . $workingDir;
            }
            @chmod($workingDir, 0775);
        }
        foreach ($volumeMounts as $vm) {
            $hostPath = $vm['host_path'] ?? "";
            if ($hostPath === "" || is_dir($hostPath)) {
                continue;
            }
            if (!@mkdir($hostPath, 0775, true)) {
                return "Cannot create interactive workspace directory: " . $hostPath;
            }
            @chmod($hostPath, 0775);
        }
        return "";
    }

    /** Session templates on the scheduler (scheduler/templates/). */
    public const WEB_SESSION_TEMPLATE = 'openvre-web-session';
    public const DESKTOP_SESSION_TEMPLATE = 'openvre-desktop-session';

    /**
     * Starts an interactive session from a scheduler template.
     *
     * Template and parameters come from the tool definition:
     *   infrastructure.k8s.template  e.g. "openvre-web-session", "openvre-desktop-session"
     *   infrastructure.k8s.params    passed to the scheduler as they are (image, port, env, ...)
     * Tools without infrastructure.k8s use their older fields (container_image,
     * container_port, interactive_access, proxy, ...), mapped by legacySessionRequest().
     *
     * This method adds the context only OpenVRE knows: user, tool, project, run
     * folder and workspace mounts. Nothing tool-specific is decided here, so a new
     * tool needs a tool definition, not a code change.
     */
    public function createSession($tool, $userId, $sessionId = "", array $sessionContext = array())
    {
        if ($this->schedulerUrl === "") {
            $this->stderr = "OPENVRE_K8S_SCHEDULER_URL is not set";
            return false;
        }
        if ($this->namespace === "") {
            $this->stderr = "OPENVRE_K8S_NAMESPACE is not set";
            return false;
        }
        if ($userId === "") {
            $this->stderr = "user id is required";
            return false;
        }

        $infra = (isset($tool['infrastructure']) && is_array($tool['infrastructure']))
            ? $tool['infrastructure']
            : array();

        // Template + parameters.
        $k8s = (isset($infra['k8s']) && is_array($infra['k8s'])) ? $infra['k8s'] : array();
        if (!empty($k8s['template'])) {
            $template = (string)$k8s['template'];
            $params = (isset($k8s['params']) && is_array($k8s['params'])) ? $k8s['params'] : array();
        } else {
            [$template, $params, $err] = $this->legacySessionRequest($infra);
            if ($err !== "") {
                $this->stderr = $err;
                return false;
            }
        }

        // An empty PHP array is encoded as a JSON list, which breaks mapping
        // parameters such as env: {}. Leave empty values out; the template default applies.
        $params = array_filter($params, fn($val) => !(is_array($val) && empty($val)));

        // Context: who and where. Same for every template.
        $workspaceMountPath = $this->resolveWorkspaceMountPath($infra);
        if ($workspaceMountPath === "") {
            $workspaceMountPath = trim((string)(getenv("OPENVRE_INTERACTIVE_DEFAULT_MOUNT") ?: ""));
        }
        if ($workspaceMountPath === "") {
            $this->stderr = "Missing workspace mount path";
            return false;
        }

        $workingDir = trim((string)($sessionContext['working_dir'] ?? ""));
        $rootDir = trim((string)($sessionContext['root_dir'] ?? ""));
        $project = trim((string)($sessionContext['project'] ?? ""));
        if ($workingDir === "") {
            $this->stderr = "Missing working_dir for interactive session";
            return false;
        }

        $volumeMounts = $this->resolveVolumeMounts($infra, $rootDir, $project);
        if (empty($volumeMounts)) {
            $volumeMounts = array(
                array(
                    "host_path" => $workingDir,
                    "container_path" => $workspaceMountPath,
                ),
            );
        }

        $dirErr = $this->ensureProjectUserDirs($rootDir, $project, $workingDir, $volumeMounts);
        if ($dirErr !== "") {
            $this->stderr = $dirErr;
            return false;
        }

        $externalBase = "";
        if (!empty($_SERVER["HTTP_HOST"])) {
            $scheme = (!empty($_SERVER["HTTPS"]) && $_SERVER["HTTPS"] !== "off") ? "https" : "http";
            $externalBase = $scheme . "://" . $_SERVER["HTTP_HOST"];
        }

        $toolId = $tool['_id'] ?? "";
        $toolName = $tool['name'] ?? "";
        $toolPrefix = $this->sanitizeSegment($toolId ?: $toolName, "interactive");
        $resolvedSessionId = $sessionId !== "" ? $sessionId : ($toolPrefix . "-" . substr(md5(uniqid("", true)), 0, 8));
        $execution = basename(rtrim($workingDir, "/"));
        if ($execution === "" || $execution === "." || $execution === "/") {
            $execution = trim((string)($sessionContext['execution'] ?? "run"));
        }

        $payload = array(
            "namespace" => $this->namespace,
            "template" => $template,
            "context" => array(
                "user_id" => (string)$userId,
                "tool_id" => (string)$toolId,
                "tool_name" => (string)$toolName,
                "session_id" => $resolvedSessionId,
                "project" => $project,
                "execution" => $execution,
                "working_dir" => $workingDir,
                "workspace_mount_path" => $workspaceMountPath,
                "volume_mounts" => $volumeMounts,
                "external_base" => $externalBase,
            ),
            // (object) keeps an empty parameter list a JSON object, not a list.
            "params" => (object)$params,
        );

        $response = $this->schedulerRequest("POST", "/v1/interactive-sessions", $payload);
        if ($response["ok"] !== true) {
            $this->stderr = $response["error"];
            return false;
        }

        $data = $response["data"];
        $this->pid = isset($data["name"]) ? (string)$data["name"] : "";
        if ($this->pid === "") {
            $this->stderr = "scheduler returned empty session name";
            return false;
        }

        // How users reach the session is declared by the template.
        $access = (isset($data["access"]) && is_array($data["access"])) ? $data["access"] : array();
        $accessType = (string)($access["type"] ?? "web");
        if ($accessType === "guacamole") {
            $protocol = strtolower(trim((string)($access["protocol"] ?? "vnc"))) ?: "vnc";
            $link = $this->createGuacamoleLink((string)($access["host"] ?? $this->pid), (int)($access["port"] ?? 0), $protocol);
            if ($link === "") {
                $this->stopInteractive($this->pid);
                return false;
            }
            $this->accessUrl = $link;
        } else {
            $this->accessUrl = (string)($access["url"] ?? ($data["access_url"] ?? ""));
        }
        return true;
    }

    /**
     * Template and parameters for tools registered before infrastructure.k8s existed.
     * Returns [template, params, error].
     */
    private function legacySessionRequest(array $infra): array
    {
        $interactiveAccess = (isset($infra['interactive_access']) && is_array($infra['interactive_access']))
            ? $infra['interactive_access']
            : array();
        $desktop = strtolower(trim((string)($interactiveAccess['mode'] ?? ''))) === 'guacamole';

        $image = (string)($infra['container_image'] ?? (getenv("OPENVRE_INTERACTIVE_DEFAULT_IMAGE") ?: ""));
        if ($image === "") {
            return array("", array(), "Missing infrastructure.container_image for interactive tool");
        }

        if ($desktop) {
            $port = (int)($interactiveAccess['port'] ?? 0);
            if ($port <= 0) {
                return array("", array(), "Guacamole interactive tool is missing interactive_access.port");
            }
        } else {
            $port = isset($infra['container_port']) ? (int)$infra['container_port'] : 0;
            if ($port <= 0) {
                $port = (int)(getenv("OPENVRE_INTERACTIVE_DEFAULT_PORT") ?: 0);
            }
            if ($port <= 0) {
                return array("", array(), "Missing infrastructure.container_port (and OPENVRE_INTERACTIVE_DEFAULT_PORT unset)");
            }
        }

        $memoryGb = (int)($infra['memory'] ?? 2);
        $cpus = (int)($infra['cpus'] ?? 1);
        $params = array(
            "image" => $image,
            "port" => $port,
            "cpu" => $cpus >= 2 ? (string)$cpus : (max(1, $cpus) * 500 . "m"),
            "memory" => max(1, $memoryGb) . "Gi",
        );
        $executable = isset($infra['executable']) ? trim((string)$infra['executable']) : "";
        if ($executable !== "") {
            $params["executable"] = $executable;
        }
        if (!empty($infra['readiness_path'])) {
            $params["readiness_path"] = (string)$infra['readiness_path'];
        }
        if (!empty($infra['image_pull_secrets']) && is_array($infra['image_pull_secrets'])) {
            $params["image_pull_secrets"] = array_values(array_map("strval", $infra['image_pull_secrets']));
        }

        if ($desktop) {
            $protocol = strtolower(trim((string)($interactiveAccess['protocol'] ?? 'vnc')));
            $params["protocol"] = $protocol !== "" ? $protocol : "vnc";
            return array(self::DESKTOP_SESSION_TEMPLATE, $params, "");
        }

        $proxy = (isset($infra['proxy']) && is_array($infra['proxy'])) ? $infra['proxy'] : array();
        if (!empty($proxy['root_path_mode'])) {
            $params["proxy_root_mode"] = strtolower(trim((string)$proxy['root_path_mode']));
        }
        if (!empty($proxy['root_path_header'])) {
            $params["proxy_root_header"] = trim((string)$proxy['root_path_header']);
        }
        if (!empty($proxy['extra_headers']) && is_array($proxy['extra_headers'])) {
            $params["proxy_headers"] = array_map("strval", $proxy['extra_headers']);
        }
        return array(self::WEB_SESSION_TEMPLATE, $params, "");
    }

    public function getAccessUrl()
    {
        return $this->accessUrl;
    }

    private function interactiveRunningInfo($pid)
    {
        if (!$pid || $this->namespace === "") {
            return array();
        }

        $response = $this->schedulerRequest(
            "GET",
            "/interactive-sessions/" . rawurlencode($pid) . "?namespace=" . rawurlencode($this->namespace)
        );
        if ($response["ok"] !== true) {
            // Do not drop the session from the user's list (its pods keep running)
            // just because the scheduler could not answer this time.
            $this->logger->warning("K8s session $pid: status unknown, scheduler error: " . $response["error"]);
            return $this->unknownStateInfo($pid);
        }
        if (empty($response["data"]["exists"])) {
            return array();
        }

        $state = $response["data"]["state"] ?? "Pending";
        $info = array(
            "pid" => $pid,
            // Failed = image pull or crash loop: show ERROR instead of waiting forever.
            "state" => $state === "Running" ? "RUNNING" : ($state === "Failed" ? "ERROR" : "PENDING"),
            "job_name" => $pid,
        );
        if (!empty($response["data"]["reason"])) {
            $info["reason"] = (string)$response["data"]["reason"];
        }
        return $info;
    }

    private function stopInteractive($pid)
    {
        if (!$pid) {
            return array(false, "No session id given");
        }
        if ($this->namespace === "") {
            return array(false, "OPENVRE_K8S_NAMESPACE is not set");
        }
        try {
            $this->deleteGuacamoleConnection($pid);
        } catch (\Exception $e) {
            $this->stderr = $e->getMessage();
        }
        $response = $this->schedulerRequest(
            "DELETE",
            "/interactive-sessions/" . rawurlencode($pid) . "?namespace=" . rawurlencode($this->namespace)
        );
        if ($response["ok"] === true) {
            return array(true, trim((string)($response["data"]["stdout"] ?? "")));
        }
        return array(false, $response["error"] ?: "Failed to delete interactive session");
    }

    private function guacamoleUsername(): string
    {
        if (!empty($_SESSION['tokenInfo']['preferred_username'])) {
            return (string)$_SESSION['tokenInfo']['preferred_username'];
        }
        if (!empty($_SESSION['User']['id'])) {
            return (string)$_SESSION['User']['id'];
        }
        return "";
    }

    private function guacamolePdo(): ?\PDO
    {
        $host = getenv('GUACAMOLE_DB_HOST');
        $name = getenv('GUACAMOLE_DB_NAME');
        $user = getenv('GUACAMOLE_DB_USER');
        $pass = getenv('GUACAMOLE_DB_PASSWORD');
        if ($host === false || $host === '' || $name === false || $name === '' || $user === false || $user === '') {
            return null;
        }
        $port = getenv('GUACAMOLE_DB_PORT');
        if ($port === false || $port === '') {
            $port = '3306';
        }
        return new \PDO(
            "mysql:host={$host};port={$port};dbname={$name};charset=utf8mb4",
            $user,
            ($pass === false) ? '' : (string)$pass,
            array(
                \PDO::ATTR_ERRMODE => \PDO::ERRMODE_EXCEPTION,
                \PDO::ATTR_DEFAULT_FETCH_MODE => \PDO::FETCH_ASSOC,
                \PDO::ATTR_EMULATE_PREPARES => false,
            )
        );
    }

    private function guacamoleClientToken(int $connectionId): string
    {
        $raw = $connectionId . "\0" . "c" . "\0" . "mysql";
        return rtrim(strtr(base64_encode($raw), '+/', '-_'), '=');
    }

    private function guacamoleEntityId(\PDO $db, string $username): int
    {
        $stmt = $db->prepare(
            "SELECT entity_id FROM guacamole_entity WHERE name = ? AND type = 'USER'"
        );
        $stmt->execute(array($username));
        $row = $stmt->fetch();
        if ($row) {
            return (int)$row['entity_id'];
        }

        $db->beginTransaction();
        try {
            $stmt = $db->prepare("INSERT INTO guacamole_entity (name, type) VALUES (?, 'USER')");
            $stmt->execute(array($username));
            $entityId = (int)$db->lastInsertId();
            $stmt = $db->prepare(
                "INSERT INTO guacamole_user (entity_id, password_hash, password_salt, password_date)
                 VALUES (?, UNHEX(?), UNHEX(?), NOW())"
            );
            $stmt->execute(array(
                $entityId,
                bin2hex(random_bytes(32)),
                bin2hex(random_bytes(32)),
            ));
            $db->commit();
            return $entityId;
        } catch (\Exception $e) {
            if ($db->inTransaction()) {
                $db->rollBack();
            }
            $stmt = $db->prepare(
                "SELECT entity_id FROM guacamole_entity WHERE name = ? AND type = 'USER'"
            );
            $stmt->execute(array($username));
            $row = $stmt->fetch();
            if ($row) {
                return (int)$row['entity_id'];
            }
            throw $e;
        }
    }

    private function createGuacamoleLink(string $k8sName, int $port, string $protocol): string
    {
        $username = $this->guacamoleUsername();
        if ($username === "") {
            $this->stderr = "Cannot create Guacamole connection: no username in the session";
            return "";
        }
        try {
            $db = $this->guacamolePdo();
            if ($db === null) {
                $this->stderr = "GUACAMOLE_DB_HOST, GUACAMOLE_DB_NAME, and GUACAMOLE_DB_USER must be set";
                return "";
            }
            $entityId = $this->guacamoleEntityId($db, $username);
            $hostname = $k8sName . "." . $this->namespace . ".svc.cluster.local";
            $db->beginTransaction();
            $stmt = $db->prepare(
                "INSERT INTO guacamole_connection (connection_name, protocol) VALUES (?, ?)"
            );
            $stmt->execute(array($k8sName, $protocol));
            $connectionId = (int)$db->lastInsertId();
            $stmt = $db->prepare(
                "INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
                 VALUES (?, 'hostname', ?)"
            );
            $stmt->execute(array($connectionId, $hostname));
            $stmt = $db->prepare(
                "INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
                 VALUES (?, 'port', ?)"
            );
            $stmt->execute(array($connectionId, (string)$port));
            $stmt = $db->prepare(
                "INSERT INTO guacamole_connection_permission (entity_id, connection_id, permission)
                 VALUES (?, ?, 'READ')"
            );
            $stmt->execute(array($entityId, $connectionId));
            $db->commit();
            return "/guacamole/#/client/" . $this->guacamoleClientToken($connectionId);
        } catch (\Exception $e) {
            if (isset($db) && $db instanceof \PDO && $db->inTransaction()) {
                $db->rollBack();
            }
            $this->stderr = "Cannot create Guacamole connection: " . $e->getMessage();
            return "";
        }
    }

    private function deleteGuacamoleConnection(string $k8sName): void
    {
        $db = $this->guacamolePdo();
        if ($db === null || $k8sName === "") {
            return;
        }
        $stmt = $db->prepare("DELETE FROM guacamole_connection WHERE connection_name = ?");
        $stmt->execute(array($k8sName));
    }
}
