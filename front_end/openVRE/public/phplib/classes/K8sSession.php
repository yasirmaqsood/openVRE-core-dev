<?php

namespace OpenVRE;

use Monolog\Logger;

/**
 * Kubernetes interactive sessions (launcher "kubernetes_interactive"):
 * start, status and stop through the scheduler.
 *
 * Sessions are web tools: the scheduler answers with their URL under
 * /interactive-tool/, served through the gateway.
 */
class K8sSession
{
    private Logger $logger;
    private SchedulerClient $scheduler;
    private string $pid = "";
    private string $accessUrl = "";
    private string $error = "";

    public function __construct()
    {
        $this->logger = LoggerFactory::getLogger("K8s sessions");
        $this->scheduler = new SchedulerClient();
    }

    /**
     * Starts a session for the tool. $context: see K8sSessionRequest::build.
     * Returns false on failure; getErr() says why.
     */
    public function create(array $tool, array $context): bool
    {
        $err = $this->scheduler->configError();
        if ($err !== "") {
            return $this->failed($err);
        }
        try {
            $payload = K8sSessionRequest::build($tool, $this->scheduler->namespace(), $context);
        } catch (\InvalidArgumentException $e) {
            return $this->failed($e->getMessage());
        }
        $workingDir = $payload["context"]["working_dir"];
        if (!is_dir($workingDir) && !@mkdir($workingDir, 0775, true)) {
            return $this->failed("Cannot create run directory: " . $workingDir);
        }

        $response = $this->scheduler->request("POST", "/v1/interactive-sessions", $payload);
        if ($response["ok"] !== true) {
            return $this->failed($response["error"]);
        }
        $data = $response["data"];
        $this->pid = (string)($data["name"] ?? "");
        if ($this->pid === "") {
            return $this->failed("scheduler returned empty session name");
        }

        $access = is_array($data["access"] ?? null) ? $data["access"] : array();
        if (($access["type"] ?? "") !== "web" || empty($access["url"])) {
            $this->stop($this->pid);
            return $this->failed("scheduler returned no web address for the session");
        }
        $this->accessUrl = (string)$access["url"];
        return true;
    }

    /**
     * Session state for OpenVRE's job list. Empty array = gone. When the
     * scheduler cannot be reached the session stays listed (its pods keep running).
     */
    public function getRunningJobInfo(string $pid): array
    {
        if ($pid === "") {
            return array();
        }
        $response = $this->scheduler->request("GET", $this->scheduler->objectPath("interactive-sessions", $pid));
        if ($response["ok"] !== true) {
            $this->logger->warning("K8s session $pid: status unknown, scheduler error: " . $response["error"]);
            return SchedulerClient::unknownStateInfo($pid);
        }
        if (empty($response["data"]["exists"])) {
            return array();
        }
        $state = $response["data"]["state"] ?? "Pending";
        $info = array(
            "pid" => $pid,
            // Failed = image pull error or crash loop: show ERROR instead of waiting forever.
            "state" => $state === "Running" ? "RUNNING" : ($state === "Failed" ? "ERROR" : "PENDING"),
            "job_name" => $pid,
        );
        if (!empty($response["data"]["reason"])) {
            $info["reason"] = (string)$response["data"]["reason"];
        }
        return $info;
    }

    /** Stops the session. */
    public function stop(string $pid): array
    {
        if ($pid === "") {
            return array(false, "No session id given");
        }
        $response = $this->scheduler->request("DELETE", $this->scheduler->objectPath("interactive-sessions", $pid));
        if ($response["ok"] === true) {
            return array(true, trim((string)($response["data"]["stdout"] ?? "")));
        }
        return array(false, $response["error"] ?: "Failed to delete interactive session");
    }

    public function getPid(): string
    {
        return $this->pid;
    }

    public function getAccessUrl(): string
    {
        return $this->accessUrl;
    }

    public function getErr(): string
    {
        return $this->error;
    }

    private function failed(string $error): bool
    {
        $this->error = $error;
        return false;
    }
}
