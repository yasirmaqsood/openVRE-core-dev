<?php

require dirname(__FILE__) . "/../../config/globals.inc.php";


function checkStatus($pid)
{
    $interactiveToolprefix = "/interactive-tool/";

    $login = $_SESSION['User']['_id'];
    $jobs  = getUserJobPid($login, $pid);

    if (!isset($jobs[$pid])) {
        return [
            "ready"   => false,
            "reload"  => false,
            "title"   => "Job not found",
            "message" => "The requested interactive session could not be found."
        ];
    }

    $job = $jobs[$pid];

    if (($job['launcher'] ?? '') === "kubernetes_interactive") {
        return checkStatusK8s($pid, $job);
    }

    /*----------------------------------------------------
     * Job state
     *---------------------------------------------------*/

    if ($job['state'] == "PENDING") {
        return [
            "ready"   => false,
            "reload"  => true,
            "title"   => "Waiting for scheduler",
            "message" => "The interactive session is waiting for compute resources."
        ];
    }

    if ($job['state'] != "RUNNING") {
        return [
            "ready"   => false,
            "reload"  => false,
            "title"   => "Session finished",
            "message" => "The interactive session is no longer running."
        ];
    }

    /*----------------------------------------------------
     * Wait until stdout exists
     *---------------------------------------------------*/

    if (!is_file($job['stdout_file'])) {
        return [
            "ready"   => false,
            "reload"  => true,
            "title"   => "Starting container",
            "message" => "Waiting for launcher output..."
        ];
    }

    $stdout = file_get_contents($job['stdout_file']);

    /*----------------------------------------------------
     * Save metadata in the session (KEEP THIS)
     *---------------------------------------------------*/

    /*
    if (preg_match('/ExposedPort: (\d+)/', $stdout, $matches)) {
        $_SESSION['User']['lastjobs'][$pid]['interactive_tool']['port'] = $matches[1];
    }

    if (preg_match('/ContainerID: (\w+)/', $stdout, $matches)) {
        $_SESSION['User']['lastjobs'][$pid]['interactive_tool']['container_id'] = $matches[1];
    }

    if (preg_match('/ContainerName: (\S+)/', $stdout, $matches)) {
        $_SESSION['User']['lastjobs'][$pid]['interactive_tool']['containerName'] = $matches[1];
    }
    */

    /*----------------------------------------------------
     * Has the service started?
     *---------------------------------------------------*/

    if (strpos($stdout, "Service UP") === false) {

        return [
            "ready"   => false,
            "reload"  => true,
            "title"   => "Preparing interactive session",
            "message" => "The container is running. Waiting for the application to become available..."
        ];
    }

    /*----------------------------------------------------
     * Mark service ready
     *---------------------------------------------------*/

    $url = $GLOBALS['SERVER']
        . $interactiveToolprefix
        . $job['containerName']
        . "/";

    return [
        "ready"   => true,
        "reload"  => false,
        "title"   => "Interactive session ready",
        "message" => "Your session is ready.",
        "url"     => $url,
        "job"     => $job
    ];
}


/**
 * Status of a Kubernetes session, read live from the scheduler. Its URL was
 * saved when it started: its path under /interactive-tool/ (gateway).
 */
function checkStatusK8s($pid, $job)
{
    $info = getRunningJobInfo($pid, "kubernetes_interactive");
    $state = $info['state'] ?? "";
    $reason = $info['reason'] ?? "";

    if (empty($info)) {
        return ["ready" => false, "reload" => false, "title" => "Session finished",
            "message" => "The interactive session is no longer running."];
    }
    if (!empty($info['scheduler_unreachable'])) {
        return ["ready" => false, "reload" => true, "title" => "Waiting for scheduler",
            "message" => "The session status cannot be read right now. Retrying..."];
    }
    if ($state == "ERROR") {
        return ["ready" => false, "reload" => false, "title" => "Session failed",
            "message" => "The interactive session could not start" . ($reason !== "" ? " ($reason)" : "") . ". Stop it and try again, or contact the administrators."];
    }
    if ($state != "RUNNING") {
        $messages = [
            "Unschedulable" => "Waiting for free resources in the cluster.",
            "ContainerCreating" => "Downloading the tool image. The first start on a node can take several minutes.",
            "PodInitializing" => "Preparing the session.",
        ];
        return ["ready" => false, "reload" => true, "title" => "Starting interactive session",
            "message" => $messages[$reason] ?? "The session is starting. This page refreshes on its own."];
    }

    $url = (string)($job['access_path'] ?? "");
    if ($url === "") {
        return ["ready" => false, "reload" => false, "title" => "Session URL missing",
            "message" => "The session is running, but its address was not saved. Stop it and start it again."];
    }
    if ($url[0] === "/") {
        $url = rtrim($GLOBALS['URL'], "/") . $url;
    }
    return ["ready" => true, "reload" => false, "title" => "Interactive session ready",
        "message" => "Your session is ready.", "url" => $url];
}
