<?php

namespace OpenVRE;

/**
 * Kubernetes interactive sessions (RStudio, etc.) via the scheduler service.
 * Creates Deployment + Service + Ingress at /interactive-tool/<session-id>/.
 */
class ProcessK8sInteractive
{
    private $namespace = "";
    private $schedulerUrl = "";
    private $schedulerToken = "";
    private $sharedPvc = "";
    private $userdataPvc = "";
    private $admindataPvc = "";
    private $pid = "";
    private $accessUrl = "";
    private $stderr = "";

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

    public function __construct()
    {
        $this->namespace = getenv("OPENVRE_K8S_NAMESPACE") ?: "";
        $this->schedulerUrl = rtrim(getenv("OPENVRE_K8S_SCHEDULER_URL") ?: "", "/");
        $this->schedulerToken = getenv("OPENVRE_K8S_SCHEDULER_TOKEN") ?: "";
        $this->sharedPvc = getenv("OPENVRE_K8S_SHARED_PVC") ?: "";
        $this->userdataPvc = getenv("OPENVRE_K8S_USERDATA_PVC") ?: "";
        $this->admindataPvc = getenv("OPENVRE_K8S_ADMINDATA_PVC") ?: "";
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

    public function getPid()
    {
        return $this->pid;
    }

    public function getAccessUrl()
    {
        return $this->accessUrl;
    }

    public function getErr()
    {
        return $this->stderr !== "" ? $this->stderr : null;
    }

    public function getRunningJobInfo($pid)
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

    public function stop($pid = null)
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

    private function schedulerRequest($method, $path, $payload = null)
    {
        if ($this->schedulerUrl === "") {
            return array("ok" => false, "error" => "OPENVRE_K8S_SCHEDULER_URL is not set");
        }
        if (!function_exists("curl_init")) {
            return array("ok" => false, "error" => "PHP curl extension is required");
        }

        $ch = curl_init($this->schedulerUrl . $path);
        $headers = array("Content-Type: application/json");
        if ($this->schedulerToken !== "") {
            $headers[] = "Authorization: Bearer " . $this->schedulerToken;
        }
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
        curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
        curl_setopt($ch, CURLOPT_TIMEOUT, 60);
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
        if ($code < 200 || $code >= 300) {
            $msg = is_array($json) && isset($json["error"]) ? $json["error"] : ("HTTP " . $code);
            return array("ok" => false, "error" => $msg);
        }
        if (!is_array($json)) {
            return array("ok" => false, "error" => "Invalid JSON from scheduler");
        }
        if (isset($json["ok"]) && !$json["ok"]) {
            return array("ok" => false, "error" => $json["error"] ?? "Scheduler error");
        }
        return array("ok" => true, "data" => $json);
    }
}
