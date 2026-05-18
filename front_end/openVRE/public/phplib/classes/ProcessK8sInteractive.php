<?php

/**
 * Kubernetes interactive sessions (RStudio, etc.) via the scheduler service.
 * Each session gets Deployment + Service + Ingress at /{user-id}/{session-id}/.
 */
class ProcessK8sInteractive
{
    private $namespace = "bsctre-v2";
    private $schedulerUrl = "";
    private $schedulerToken = "";
    private $sharedPvc = "dashboard-frontend-sgecore-shareddata";
    private $pid = "";
    private $accessUrl = "";
    private $stderr = "";

    public function __construct()
    {
        $this->namespace = getenv("OPENVRE_K8S_NAMESPACE") ?: "bsctre-v2";
        $this->schedulerUrl = rtrim(getenv("OPENVRE_K8S_SCHEDULER_URL") ?: "", "/");
        $this->schedulerToken = getenv("OPENVRE_K8S_SCHEDULER_TOKEN") ?: "";
        $this->sharedPvc = getenv("OPENVRE_K8S_SHARED_PVC") ?: "dashboard-frontend-sgecore-shareddata";
    }

    public function createSession($tool, $userId, $sessionId = "")
    {
        if ($this->schedulerUrl === "") {
            $this->stderr = "OPENVRE_K8S_SCHEDULER_URL is not set";
            return false;
        }
        if ($userId === "") {
            $this->stderr = "user id is required";
            return false;
        }

        $image = $tool['infrastructure']['container_image'] ?? "rocker/rstudio:4.4.2";
        $memoryGb = (int)($tool['infrastructure']['memory'] ?? 2);
        $cpus = (int)($tool['infrastructure']['cpus'] ?? 1);
        $memory = max(1, $memoryGb) . "Gi";
        $cpu = max(1, $cpus) * 500 . "m";
        if ($cpus >= 2) {
            $cpu = (string)$cpus;
        }

        $externalBase = "";
        if (!empty($_SERVER["HTTP_HOST"])) {
            $scheme = (!empty($_SERVER["HTTPS"]) && $_SERVER["HTTPS"] !== "off") ? "https" : "http";
            $externalBase = $scheme . "://" . $_SERVER["HTTP_HOST"];
        }

        $payload = array(
            "namespace" => $this->namespace,
            "user_id" => $userId,
            "session_id" => $sessionId !== "" ? $sessionId : ("rstudio-" . substr(md5(uniqid("", true)), 0, 8)),
            "image" => $image,
            "pvc" => $this->sharedPvc,
            "password" => getenv("OPENVRE_INTERACTIVE_PASSWORD") ?: "openvre",
            "cpu" => $cpu,
            "memory" => $memory,
            "run_as_uid" => (int)(getenv("OPENVRE_K8S_RUN_AS_UID") ?: 1000),
            "run_as_gid" => (int)(getenv("OPENVRE_K8S_RUN_AS_GID") ?: 1000),
            "external_base" => $externalBase,
        );

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
        $job = array();
        if (!$pid) {
            return $job;
        }

        $response = $this->schedulerRequest(
            "GET",
            "/interactive-sessions/" . rawurlencode($pid) . "?namespace=" . rawurlencode($this->namespace)
        );
        if ($response["ok"] !== true) {
            return array();
        }
        if (empty($response["data"]["exists"])) {
            return array();
        }

        $state = isset($response["data"]["state"]) ? $response["data"]["state"] : "Pending";
        $job["pid"] = $pid;
        $job["state"] = ($state === "Running") ? "RUNNING" : "PENDING";
        $job["job_name"] = $pid;
        return $job;
    }

    public function stop($pid = null)
    {
        if (!$pid) {
            return array(false, "No session id given");
        }
        $response = $this->schedulerRequest(
            "DELETE",
            "/interactive-sessions/" . rawurlencode($pid) . "?namespace=" . rawurlencode($this->namespace)
        );
        if ($response["ok"] === true) {
            return array(true, isset($response["data"]["stdout"]) ? trim((string)$response["data"]["stdout"]) : "");
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

        $url = $this->schedulerUrl . $path;
        $ch = curl_init($url);
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
            return array("ok" => false, "error" => isset($json["error"]) ? $json["error"] : "Scheduler error");
        }
        return array("ok" => true, "data" => $json);
    }
}
