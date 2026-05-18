<?php
/**
 * Ingress-nginx external auth for interactive sessions.
 * Validates OpenVRE login and that the URL user segment matches the session user.
 */
require __DIR__ . "/../../config/bootstrap.php";

function interactive_auth_deny($code, $message)
{
	header("Content-Type: text/plain; charset=utf-8", true);
	header("HTTP/1.1 " . $code . " " . $message, true, $code);
	exit($message);
}

if (!checkLoggedIn()) {
	interactive_auth_deny(401, "Unauthorized");
}

$userId = $_SESSION["User"]["id"] ?? "";
if ($userId === "" || preg_match('/^BSC_TREANON/i', $userId)) {
	interactive_auth_deny(403, "Forbidden");
}

$uri = $_SERVER["HTTP_X_ORIGINAL_URI"] ?? "";
if ($uri === "" && !empty($_SERVER["HTTP_X_ORIGINAL_URL"])) {
	$uri = parse_url($_SERVER["HTTP_X_ORIGINAL_URL"], PHP_URL_PATH) ?: "";
}
if ($uri === "" && !empty($_SERVER["REQUEST_URI"])) {
	$uri = parse_url($_SERVER["REQUEST_URI"], PHP_URL_PATH) ?: "";
}

if (!preg_match("#^/([^/]+)/#", $uri, $matches)) {
	interactive_auth_deny(403, "Forbidden");
}

$pathUser = $matches[1];
$expected = sanitizeInteractiveUserPath($userId);

if (!checkAdmin() && $pathUser !== $expected) {
	interactive_auth_deny(403, "Forbidden");
}

header("Content-Type: text/plain; charset=utf-8", true);
header("HTTP/1.1 200 OK", true, 200);
exit("OK");
