<?php

namespace OpenVRE;

/**
 * Builds the scheduler request for an interactive session from a tool
 * definition plus OpenVRE's context (user, project, run folder, mounts).
 *
 * Template and parameters come from the tool's usual fields (container_image,
 * container_port, interactive_access, proxy, cpus, memory, ...); see
 * toolFieldsTemplate(). A tool can instead name a scheduler template directly:
 *   infrastructure.k8s.template  e.g. "openvre-web-session"
 *   infrastructure.k8s.params    sent to the scheduler as they are (image, port, cpu, memory, env, ...)
 *
 * Nothing tool-specific is decided here: a new tool needs a tool definition,
 * not a code change.
 */
class K8sSessionRequest
{
    public const WEB_SESSION_TEMPLATE = 'openvre-web-session';

    /**
     * The mount for one entry of infrastructure.volumes ("<folder in the
     * project>" => "<path in the container>" or {path, read_only}). Folders
     * named "repository" are mounted read-only. Null when the entry is unusable.
     */
    public static function volumeMount(string $projectDir, $volume, $target): ?array
    {
        if (!is_string($volume) || trim($volume, "/") === "") {
            return null;
        }
        $readOnly = false;
        if (is_array($target)) {
            $readOnly = !empty($target['readOnly']) || !empty($target['read_only']);
            $target = (string)($target['path'] ?? ($target['container_path'] ?? ""));
        }
        if (!is_string($target) || $target === "") {
            return null;
        }
        $volume = "/" . trim($volume, "/");
        $mount = array(
            "host_path" => preg_replace('#/+#', '/', rtrim($projectDir, "/") . $volume),
            "container_path" => $target,
        );
        if ($readOnly || preg_match('#(^|/)repository$#', $volume)) {
            $mount["read_only"] = true;
        }
        return $mount;
    }

    /**
     * The POST /v1/interactive-sessions body.
     *
     * $context: user_id, working_dir, project, execution, volume_mounts (from
     * volumeMount), cpus and memory (GB) as resolved for this run.
     * Throws \InvalidArgumentException when the tool or context is incomplete.
     */
    public static function build(array $tool, string $namespace, array $context): array
    {
        $infra = is_array($tool['infrastructure'] ?? null) ? $tool['infrastructure'] : array();
        $userId = (string)($context['user_id'] ?? "");
        $workingDir = trim((string)($context['working_dir'] ?? ""));
        if ($userId === "") {
            throw new \InvalidArgumentException("user id is required");
        }
        if ($workingDir === "") {
            throw new \InvalidArgumentException("Missing working_dir for interactive session");
        }

        [$template, $params] = self::templateAndParams($infra, $context['cpus'] ?? null, $context['memory'] ?? null);
        // An empty PHP array encodes as a JSON list, which breaks mapping
        // parameters such as env: {}. Leave them out; the template default applies.
        $params = array_filter($params, fn($val) => !(is_array($val) && empty($val)));

        $mounts = array_values($context['volume_mounts'] ?? array());
        $mountPath = self::workspaceMountPath($infra, $mounts);
        if (empty($mounts)) {
            $mounts = array(array("host_path" => $workingDir, "container_path" => $mountPath));
        }

        $execution = basename(rtrim($workingDir, "/"));
        if ($execution === "" || $execution === ".") {
            $execution = trim((string)($context['execution'] ?? "run"));
        }

        return array(
            "namespace" => $namespace,
            "template" => $template,
            "context" => array(
                "user_id" => $userId,
                "tool_id" => (string)($tool['_id'] ?? ""),
                "tool_name" => (string)($tool['name'] ?? ""),
                "session_id" => self::sessionId($tool),
                "project" => trim((string)($context['project'] ?? ""), "/"),
                "execution" => $execution,
                "working_dir" => $workingDir,
                "workspace_mount_path" => $mountPath,
                "volume_mounts" => $mounts,
                "external_base" => self::externalBase(),
            ),
            // (object): an empty parameter list stays a JSON object.
            "params" => (object)$params,
        );
    }

    private static function templateAndParams(array $infra, $cpus, $memory): array
    {
        $k8s = is_array($infra['k8s'] ?? null) ? $infra['k8s'] : array();
        if (!empty($k8s['template'])) {
            return array((string)$k8s['template'], is_array($k8s['params'] ?? null) ? $k8s['params'] : array());
        }
        return self::toolFieldsTemplate($infra, $cpus ?? ($infra['cpus'] ?? 1), $memory ?? ($infra['memory'] ?? 2));
    }

    /** Where the run folder appears in the container. */
    private static function workspaceMountPath(array $infra, array $mounts): string
    {
        $path = trim((string)($infra['workspace_mount_path'] ?? ""));
        if (empty($mounts) && is_array($infra['volumes'] ?? null)) {
            // Only the container paths matter here.
            foreach ($infra['volumes'] as $volume => $target) {
                $mounts[] = self::volumeMount("/", $volume, $target);
            }
            $mounts = array_filter($mounts);
        }
        foreach ($mounts as $mount) {
            if ($path === "" && empty($mount['read_only'])) {
                $path = (string)$mount['container_path'];
            }
        }
        if ($path === "") {
            $path = trim(SchedulerClient::optionalEnv("OPENVRE_INTERACTIVE_DEFAULT_MOUNT"));
        }
        if ($path === "") {
            throw new \InvalidArgumentException("Missing workspace mount path");
        }
        return $path;
    }

    /**
     * Template and parameters from the tool's usual fields (web template).
     */
    private static function toolFieldsTemplate(array $infra, $cpus, $memory): array
    {
        $access = is_array($infra['interactive_access'] ?? null) ? $infra['interactive_access'] : array();
        if (strtolower(trim((string)($access['mode'] ?? ''))) === 'guacamole') {
            throw new \InvalidArgumentException("Desktop (Guacamole) tools are not supported on Kubernetes");
        }

        $image = (string)($infra['container_image'] ?? SchedulerClient::optionalEnv("OPENVRE_INTERACTIVE_DEFAULT_IMAGE"));
        if ($image === "") {
            throw new \InvalidArgumentException("Missing infrastructure.container_image for interactive tool");
        }
        $port = (int)($infra['container_port'] ?? SchedulerClient::optionalEnv("OPENVRE_INTERACTIVE_DEFAULT_PORT", "0"));
        if ($port <= 0) {
            throw new \InvalidArgumentException("Missing infrastructure.container_port (and OPENVRE_INTERACTIVE_DEFAULT_PORT unset)");
        }

        $params = array(
            "image" => $image,
            "port" => $port,
            "cpu" => self::cpuQuantity($cpus),
            "memory" => max(1, (int)$memory) . "Gi",
        );
        foreach (array("executable", "readiness_path") as $key) {
            if (trim((string)($infra[$key] ?? "")) !== "") {
                $params[$key] = trim((string)$infra[$key]);
            }
        }
        if (!empty($infra['image_pull_secrets']) && is_array($infra['image_pull_secrets'])) {
            $params["image_pull_secrets"] = array_values(array_map("strval", $infra['image_pull_secrets']));
        }

        $proxy = is_array($infra['proxy'] ?? null) ? $infra['proxy'] : array();
        if (!empty($proxy['root_path_mode'])) {
            $params["proxy_root_mode"] = strtolower(trim((string)$proxy['root_path_mode']));
        }
        if (!empty($proxy['root_path_header'])) {
            $params["proxy_root_header"] = trim((string)$proxy['root_path_header']);
        }
        if (!empty($proxy['extra_headers']) && is_array($proxy['extra_headers'])) {
            $params["proxy_headers"] = array_map("strval", $proxy['extra_headers']);
        }
        return array(self::WEB_SESSION_TEMPLATE, $params);
    }

    /** CPUs as a Kubernetes quantity: 2 -> "2", 0.5 -> "500m". */
    private static function cpuQuantity($cpus): string
    {
        $cpus = (float)$cpus;
        if ($cpus <= 0) {
            $cpus = 1;
        }
        return $cpus == floor($cpus) ? (string)(int)$cpus : ((int)round($cpus * 1000)) . "m";
    }

    /** "<tool>-<8 hex>": the scheduler builds the Kubernetes names from it. */
    private static function sessionId(array $tool): string
    {
        $prefix = strtolower((string)(($tool['_id'] ?? "") ?: ($tool['name'] ?? "")));
        $prefix = substr(trim(preg_replace('/[^a-z0-9_-]+/', '-', $prefix), '-'), 0, 24) ?: "interactive";
        return $prefix . "-" . substr(md5(uniqid("", true)), 0, 8);
    }

    /**
     * The portal's scheme and host as the browser sees it: OPENVRE_PUBLIC_URL
     * when set (behind an Ingress that ends TLS, the request itself looks like
     * plain http), else the request's own host.
     */
    private static function externalBase(): string
    {
        $public = parse_url(SchedulerClient::optionalEnv("OPENVRE_PUBLIC_URL"));
        if (!empty($public['scheme']) && !empty($public['host'])) {
            return $public['scheme'] . "://" . $public['host'] . (isset($public['port']) ? ":" . $public['port'] : "");
        }
        if (empty($_SERVER["HTTP_HOST"])) {
            return "";
        }
        $https = !empty($_SERVER["HTTPS"]) && $_SERVER["HTTPS"] !== "off";
        return ($https ? "https" : "http") . "://" . $_SERVER["HTTP_HOST"];
    }
}
