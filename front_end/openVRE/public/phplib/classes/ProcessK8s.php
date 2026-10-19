<?php

namespace OpenVRE;

use Monolog\Logger;

/**
 * ProcessK8s — Kubernetes batch Jobs and interactive sessions.

 *
 *
 * Environment variables consumed (required — set defaults in deployment env / .env):
 *   OPENVRE_K8S_NAMESPACE       — target namespace for Jobs
 *   OPENVRE_K8S_JOB_IMAGE       — fallback container image for the Job pod (may be empty if set per-tool)
 *   OPENVRE_K8S_SHARED_PVC      — PVC name for /shared_data volume
 *   OPENVRE_K8S_TOOLS_PVC       — PVC name for /var/www/html/openVRE/public/tools volume
 *   OPENVRE_K8S_SCHEDULER_URL   — base URL of the scheduler service
 *   OPENVRE_K8S_SCHEDULER_TOKEN — Bearer token for scheduler authentication (may be empty)
 *   OPENVRE_K8S_RUN_AS_UID      — UID the Job pod runs as
 *   OPENVRE_K8S_RUN_AS_GID      — GID / fsGroup for the Job pod
 *   OPENVRE_K8S_JOB_TTL         — seconds to keep a finished Job before auto-deletion
 *   OPENVRE_K8S_JOB_DEADLINE    — max seconds a Job is allowed to run before being killed
 *   OPENVRE_K8S_JOB_MEMORY_REQUEST — fallback memory request when tool has no infrastructure.memory (e.g. "1Gi")
 *   OPENVRE_K8S_JOB_MEMORY_LIMIT   — fallback memory limit when tool has no infrastructure.memory (e.g. "2Gi")
 *   OPENVRE_K8S_NODE_SELECTOR   — optional "key=value,key2=value2" (same as scheduler)
 *   OPENVRE_K8S_TOLERATIONS     — optional "key=value:Effect,..." (same as scheduler)
 *   OPENVRE_K8S_EVICTION_TOLERATION_SECONDS — not-ready/unreachable eviction grace (default 60)
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

    /** PVC name mounted at the tools directory inside the Job pod */
    private string $toolsPvc;

    /** Base URL of the scheduler HTTP service */
    private string $schedulerUrl;

    /** Bearer token sent to the scheduler for authentication */
    private string $schedulerToken;

    /** UID the Job pod container runs as */
    private int $runAsUid;

    /** GID / fsGroup for the Job pod */
    private int $runAsGid;

    /** Seconds to keep a finished Job before auto-deletion */
    private int $jobTtl;

    /** Max seconds a Job is allowed to run before being killed */
    private int $jobDeadline;

    /** Optional nodeSelector for batch Job pods (project-pinned clusters) */
    private array $nodeSelector = [];

    /** Tolerations for batch Job pods (project taints + eviction) */
    private array $tolerations = [];

    /** Interactive session URL returned by the scheduler */
    private string $accessUrl = "";

    /** PVC for interactive user data. Falls back to the shared PVC. */
    private string $userdataPvc = "";

    /** PVC for interactive audit data. Falls back to the shared PVC. */
    private string $admindataPvc = "";

    /** Additional environment variables to inject into the Job pod (from tool Mongo definition) */
    private array $jobEnv = [];

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
        $this->toolsPvc = $this->env("OPENVRE_K8S_TOOLS_PVC");
        $this->schedulerUrl = rtrim($this->env("OPENVRE_K8S_SCHEDULER_URL"), "/");
        $this->schedulerToken = $this->env("OPENVRE_K8S_SCHEDULER_TOKEN");
        $this->runAsUid = (int)$this->env("OPENVRE_K8S_RUN_AS_UID");
        $this->runAsGid = (int)$this->env("OPENVRE_K8S_RUN_AS_GID");
        $this->jobTtl = (int)$this->env("OPENVRE_K8S_JOB_TTL");
        $this->jobDeadline = (int)$this->env("OPENVRE_K8S_JOB_DEADLINE");
        $this->nodeSelector = $this->parseNodeSelector($this->optionalEnv("OPENVRE_K8S_NODE_SELECTOR"));
        $evictionSeconds = (int)$this->optionalEnv("OPENVRE_K8S_EVICTION_TOLERATION_SECONDS", "60");
        if ($evictionSeconds <= 0) {
            $evictionSeconds = 60;
        }
        $this->tolerations = array_merge(
            $this->parseTolerations($this->optionalEnv("OPENVRE_K8S_TOLERATIONS")),
            $this->evictionTolerations($evictionSeconds)
        );
        $this->userdataPvc = getenv("OPENVRE_K8S_USERDATA_PVC") ?: "";
        $this->admindataPvc = getenv("OPENVRE_K8S_ADMINDATA_PVC") ?: "";

        // Allow per-tool overrides:
        // container image and extra environment variables via $jobOptions.
        if (!empty($jobOptions["image"])) {
            $this->jobImage = $jobOptions["image"];
        }
        if (!empty($jobOptions["env"]) && is_array($jobOptions["env"])) {
            $this->jobEnv = $jobOptions["env"];
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


    /** Builds a Kubernetes Job manifest and submits it to the scheduler. */
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

        $yaml = $this->arrayToYaml($this->buildJobManifest($jobName, $cpu, $mem));

        // Submit Job via the scheduler service.
        $this->fullcommand = "POST " . $this->schedulerUrl . "/jobs";
        $this->logger->info("K8s job submission via scheduler '" . $this->fullcommand . "'");
        $response = $this->schedulerRequest("POST", "/jobs", array(
            "namespace" => $this->namespace,
            "manifest" => $yaml
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


    private function buildJobManifest(string $jobName, int $cpu, int $mem): array
    {
        $cpuRequest = $cpu > 0 ? (string)$cpu : "1";
        $cpuLimit = $cpu > 0 ? (string)$cpu : "1";
        $memLimit = $mem > 0 ? ($mem . "Gi") : $this->optionalEnv("OPENVRE_K8S_JOB_MEMORY_LIMIT");
        $memRequest = $mem > 0 ? ($mem . "Gi") : $this->optionalEnv("OPENVRE_K8S_JOB_MEMORY_REQUEST");

        $requests = array("cpu" => $cpuRequest);
        $limits = array("cpu" => $cpuLimit);
        if ($memRequest !== "") {
            $requests["memory"] = $memRequest;
        }
        if ($memLimit !== "") {
            $limits["memory"] = $memLimit;
        }

        $manifest = array(
            "apiVersion" => "batch/v1",
            "kind" => "Job",
            "metadata" => array(
                "name" => $jobName,
                "namespace" => $this->namespace,
                "labels" => array(
                    "app.kubernetes.io/managed-by" => "openvre",
                    "openvre-job-id" => $jobName
                )
            ),
            "spec" => array(
                "ttlSecondsAfterFinished" => $this->jobTtl,
                "activeDeadlineSeconds" => $this->jobDeadline,
                "backoffLimit" => 0,
                "template" => array(
                    "metadata" => array(
                        "labels" => array(
                            "app.kubernetes.io/managed-by" => "openvre",
                            "openvre-job-id" => $jobName,
                            "openvre.colocate" => "true",
                            "openvre.io/batch" => "true",
                        ),
                    ),
                    "spec" => array(
                        "restartPolicy" => "Never",
                        "containers" => array(
                            array(
                                "name" => "tool-runner",
                                "image" => $this->jobImage,
                                "command" => array("bash", "-lc", "cd \"\$OPENVRE_WORKDIR\" && bash \"\$OPENVRE_SUBMIT_SCRIPT\""),
                                "env" => array(
                                    array("name" => "OPENVRE_WORKDIR", "value" => $this->workDir),
                                    array("name" => "OPENVRE_SUBMIT_SCRIPT", "value" => $this->command),
                                ),
                                "resources" => array(
                                    "requests" => $requests,
                                    "limits" => $limits,
                                ),
                                "securityContext" => array(
                                    "allowPrivilegeEscalation" => false,
                                ),
                                "volumeMounts" => array(
                                    array("name" => "shared-data", "mountPath" => $GLOBALS['shared']),
                                )
                            )
                        ),
                        "securityContext" => array(
                            "runAsUser" => max(1, $this->runAsUid),
                            "runAsGroup" => max(1, $this->runAsGid),
                            "fsGroup" => max(1, $this->runAsGid)
                        ),
                        "volumes" => array(
                            array("name" => "shared-data", "persistentVolumeClaim" => array("claimName" => $this->sharedPvc)),
                            array("name" => "tools", "persistentVolumeClaim" => array("claimName" => $this->toolsPvc))
                        )
                    )
                )
            )
        );

        if (!empty($this->jobEnv)) {
            $envList =& $manifest["spec"]["template"]["spec"]["containers"][0]["env"];
            foreach ($this->jobEnv as $envName => $envValue) {
                if (empty($envName) || empty($envValue)) {
                    continue;
                }
                $envList[] = array(
                    "name" => (string)$envName,
                    "value" => (string)$envValue
                );
            }
        }

        $podSpec =& $manifest["spec"]["template"]["spec"];
        if (!empty($this->nodeSelector)) {
            $podSpec["nodeSelector"] = $this->nodeSelector;
        }
        if (!empty($this->tolerations)) {
            $podSpec["tolerations"] = $this->tolerations;
        }

        return $manifest;
    }

    private function parseNodeSelector(string $raw): array
    {
        $selector = [];
        foreach (explode(",", $raw) as $part) {
            $part = trim($part);
            if ($part === "" || !str_contains($part, "=")) {
                continue;
            }
            [$key, $value] = array_map("trim", explode("=", $part, 2));
            if ($key !== "" && $value !== "") {
                $selector[$key] = $value;
            }
        }

        return $selector;
    }

    private function parseTolerations(string $raw): array
    {
        $tolerations = [];
        foreach (explode(",", $raw) as $part) {
            $part = trim($part);
            if ($part === "") {
                continue;
            }
            if (str_contains($part, "=")) {
                [$kv, $effect] = array_pad(explode(":", $part, 2), 2, "NoSchedule");
                [$key, $value] = array_map("trim", explode("=", $kv, 2));
                $tolerations[] = [
                    "key" => $key,
                    "operator" => "Equal",
                    "value" => $value,
                    "effect" => $effect !== "" ? $effect : "NoSchedule",
                ];
            } else {
                [$key, $effect] = array_pad(explode(":", $part, 2), 2, "NoSchedule");
                $tolerations[] = [
                    "key" => trim($key),
                    "operator" => "Exists",
                    "effect" => $effect !== "" ? $effect : "NoSchedule",
                ];
            }
        }

        return $tolerations;
    }

    private function evictionTolerations(int $seconds): array
    {
        return [
            [
                "key" => "node.kubernetes.io/not-ready",
                "operator" => "Exists",
                "effect" => "NoExecute",
                "tolerationSeconds" => $seconds,
            ],
            [
                "key" => "node.kubernetes.io/unreachable",
                "operator" => "Exists",
                "effect" => "NoExecute",
                "tolerationSeconds" => $seconds,
            ],
        ];
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

    /** Serializes the Job manifest array to YAML before sending it to the scheduler. */
    private function arrayToYaml(array $data, int $indent = 0): string
    {
        $yaml = "";
        $spaces = str_repeat("  ", $indent);
        $isAssoc = function ($arr) {
            if (!is_array($arr)) return false;
            return array_keys($arr) !== range(0, count($arr) - 1);
        };

        foreach ($data as $key => $value) {
            if (is_array($value)) {
                if ($isAssoc($value)) {
                    // Associative array → nested YAML object.
                    $yaml .= $spaces . $key . ":\n" . $this->arrayToYaml($value, $indent + 1);
                } else {
                    // Sequential array → YAML list with "- " prefix.
                    $yaml .= $spaces . $key . ":\n";
                    foreach ($value as $item) {
                        if (is_array($item)) {
                            $yaml .= $spaces . "  -\n" . $this->arrayToYaml($item, $indent + 2);
                        } else {
                            $yaml .= $spaces . "  - " . $this->yamlScalar($item) . "\n";
                        }
                    }
                }
            } else {
                $yaml .= $spaces . $key . ": " . $this->yamlScalar($value) . "\n";
            }
        }
        return $yaml;
    }


    private function yamlScalar(mixed $value): string
    {
        if (is_bool($value)) return $value ? "true" : "false";
        if (is_numeric($value)) return (string)$value;
        $escaped = str_replace('"', '\"', (string)$value);
        return '"' . $escaped . '"';
    }


    public function getRunningJobInfo(string $pid): array
    {
        if ($this->isInteractivePid($pid)) {
            return $this->interactiveRunningInfo($pid);
        }
        $job = array();
        if (!$pid) return $job;

        // Query job status via the scheduler.
        $response = $this->schedulerRequest("GET", "/jobs/" . rawurlencode($pid) . "?namespace=" . rawurlencode($this->namespace));

        if ($response["ok"] !== true) {
            return array();
        }

        $exists = isset($response["data"]["exists"]) ? (bool)$response["data"]["exists"] : false;
        if (!$exists) {
            return array();
        }

        // Parse the Job JSON to determine its current condition.
        $jsonRaw = isset($response["data"]["job"]) ? $response["data"]["job"] : "";
        $json = json_decode($jsonRaw, true);
        if (!is_array($json)) {
            return array();
        }

        $status = $json['status'] ?? array();
        $state = "Pending";
        if (!empty($status['active'])) {
            $state = "Running";
        } elseif (!empty($status['succeeded'])) {
            $state = "Succeeded";
        } elseif (!empty($status['failed'])) {
            $state = "Failed";
        }

        // Once the Job is finished (succeeded or failed), return empty so
        // OpenVRE's workspace loop finalizes outputs and stops polling.
        if ($state === "Succeeded" || $state === "Failed") {
            return array();
        }

        $job['pid'] = $pid;
        $job['state'] = $this->jobState[$state];
        $job['job_name'] = $pid;
        return $job;
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


    public function status()
    {
        if (!$this->pid) return false;

        $response = $this->schedulerRequest("GET", "/jobs/" . rawurlencode($this->pid) . "?namespace=" . rawurlencode($this->namespace));

        if ($response["ok"] !== true) {
            return false;
        }

        $exists = isset($response["data"]["exists"]) ? (bool)$response["data"]["exists"] : false;
        if (!$exists) {
            return false;
        }

        $jsonRaw = isset($response["data"]["job"]) ? $response["data"]["job"] : "";
        $json = json_decode($jsonRaw, true);
        if (!is_array($json)) {
            return true;
        }

        $status = isset($json["status"]) && is_array($json["status"]) ? $json["status"] : array();
        // Job is no longer active if it has succeeded or failed.
        if (!empty($status["succeeded"]) || !empty($status["failed"])) {
            return false;
        }

        return true;
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
        if ($this->sharedPvc === "" && $this->userdataPvc === "") {
            $this->stderr = "OPENVRE_K8S_SHARED_PVC or OPENVRE_K8S_USERDATA_PVC is not set";
            return false;
        }
        if ($this->userdataPvc === "") {
            $this->userdataPvc = $this->sharedPvc;
        }
        if ($this->admindataPvc === "") {
            $this->admindataPvc = $this->sharedPvc !== "" ? $this->sharedPvc : $this->userdataPvc;
        }
        if ($userId === "") {
            $this->stderr = "user id is required";
            return false;
        }

        $infra = (isset($tool['infrastructure']) && is_array($tool['infrastructure']))
            ? $tool['infrastructure']
            : array();

        $image = $infra['container_image'] ?? (getenv("OPENVRE_INTERACTIVE_DEFAULT_IMAGE") ?: "");
        if ($image === "") {
            $this->stderr = "Missing infrastructure.container_image for interactive tool";
            return false;
        }

        $memoryGb = (int)($infra['memory'] ?? 2);
        $cpus = (int)($infra['cpus'] ?? 1);
        $memory = max(1, $memoryGb) . "Gi";
        $cpu = $cpus >= 2 ? (string)$cpus : (max(1, $cpus) * 500 . "m");

        $containerPort = isset($infra['container_port']) ? (int)$infra['container_port'] : 0;
        if ($containerPort <= 0) {
            $containerPort = (int)(getenv("OPENVRE_INTERACTIVE_DEFAULT_PORT") ?: 0);
        }
        if ($containerPort <= 0) {
            $this->stderr = "Missing infrastructure.container_port (and OPENVRE_INTERACTIVE_DEFAULT_PORT unset)";
            return false;
        }

        $executable = isset($infra['executable']) ? trim((string)$infra['executable']) : "";
        $proxy = (isset($infra['proxy']) && is_array($infra['proxy'])) ? $infra['proxy'] : array();

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

        $runAsUid = getenv("OPENVRE_K8S_RUN_AS_UID");
        $runAsGid = getenv("OPENVRE_K8S_RUN_AS_GID");
        if ($runAsUid === false || $runAsUid === "" || $runAsGid === false || $runAsGid === "") {
            $this->stderr = "OPENVRE_K8S_RUN_AS_UID and OPENVRE_K8S_RUN_AS_GID must be set";
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
            "user_id" => $userId,
            "session_id" => $resolvedSessionId,
            "tool_id" => $toolId,
            "tool_name" => $toolName,
            "image" => $image,
            "userdata_pvc" => $this->userdataPvc,
            "admindata_pvc" => $this->admindataPvc,
            "cpu" => $cpu,
            "memory" => $memory,
            "run_as_uid" => (int)$runAsUid,
            "run_as_gid" => (int)$runAsGid,
            "external_base" => $externalBase,
            "container_port" => $containerPort,
            "workspace_mount_path" => $workspaceMountPath,
            "working_dir" => $workingDir,
            "project" => $project,
            "execution" => $execution,
            "volume_mounts" => $volumeMounts,
        );

        if ($executable !== "") {
            $payload["executable"] = $executable;
        }
        if (!empty($proxy)) {
            $payload["proxy"] = $proxy;
        }

        $response = $this->schedulerRequest("POST", "/interactive-sessions", $payload);
        if ($response["ok"] !== true) {
            $this->stderr = $response["error"];
            return false;
        }

        $data = $response["data"];
        $this->pid = isset($data["name"]) ? (string)$data["name"] : "";
        $this->accessUrl = isset($data["access_url"]) ? (string)$data["access_url"] : "";
        if ($this->pid === "") {
            $this->stderr = "scheduler returned empty session name";
            return false;
        }
        return true;
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
        if ($response["ok"] !== true || empty($response["data"]["exists"])) {
            return array();
        }

        $state = $response["data"]["state"] ?? "Pending";
        return array(
            "pid" => $pid,
            "state" => ($state === "Running") ? "RUNNING" : "PENDING",
            "job_name" => $pid,
        );
    }

    private function stopInteractive($pid)
    {
        if (!$pid) {
            return array(false, "No session id given");
        }
        if ($this->namespace === "") {
            return array(false, "OPENVRE_K8S_NAMESPACE is not set");
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
}
