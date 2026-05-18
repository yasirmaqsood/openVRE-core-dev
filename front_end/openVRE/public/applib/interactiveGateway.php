<?php
/**
 * OpenVRE-authenticated entry to an interactive session.
 * Performs RStudio sign-in server-side and redirects with session cookies set.
 */
require __DIR__ . "/../../config/bootstrap.php";

$path = $_GET["path"] ?? "";
if ($path === "" || !preg_match("#^/([a-z0-9_-]+)/([^/]+)#", $path, $matches)) {
	http_response_code(400);
	exit("Bad request");
}

$pathUser = $matches[1];
$sessionSeg = $matches[2];

if (!checkLoggedIn()) {
	redirect($GLOBALS["BASEURL"] . "login.php");
}

$userId = $_SESSION["User"]["id"] ?? "";
if ($userId === "" || preg_match('/^BSC_TREANON/i', $userId)) {
	http_response_code(403);
	exit("Forbidden");
}

if (!checkAdmin() && sanitizeInteractiveUserPath($userId) !== $pathUser) {
	http_response_code(403);
	exit("Forbidden");
}

$namespace = getenv("OPENVRE_K8S_NAMESPACE") ?: "bsctre-v2";
$sessionSegClean = preg_replace("/[^a-z0-9_-]/", "", strtolower($sessionSeg));
$k8sName = "openvre-ix-" . $sessionSegClean;
$rootPath = "/" . $pathUser . "/" . $sessionSeg;
$internalBase = "http://" . $k8sName . "." . $namespace . ".svc.cluster.local:8787";
$password = getenv("OPENVRE_INTERACTIVE_PASSWORD") ?: "openvre";
$publicUrl = rtrim($GLOBALS["URL"], "/") . $rootPath . "/";

$cookieFile = tempnam(sys_get_temp_dir(), "openvre_rs_");
if ($cookieFile === false) {
	http_response_code(500);
	exit("Internal error");
}

$ua = "Mozilla/5.0 (compatible; OpenVRE-Interactive/1.0)";

function interactive_gateway_curl($url, $rootPath, $cookieFile, $ua, $extra = array())
{
	$headers = array(
		"User-Agent: " . $ua,
		"X-RStudio-Root-Path: " . $rootPath,
	);
	if (!empty($extra["headers"])) {
		$headers = array_merge($headers, $extra["headers"]);
	}
	$ch = curl_init($url);
	$opts = array(
		CURLOPT_RETURNTRANSFER => true,
		CURLOPT_FOLLOWLOCATION => false,
		CURLOPT_HEADER => true,
		CURLOPT_COOKIEJAR => $cookieFile,
		CURLOPT_COOKIEFILE => $cookieFile,
		CURLOPT_HTTPHEADER => $headers,
		CURLOPT_TIMEOUT => 30,
	);
	if (!empty($extra["post"])) {
		$opts[CURLOPT_POST] = true;
		$opts[CURLOPT_POSTFIELDS] = $extra["post"];
	}
	curl_setopt_array($ch, $opts);
	$raw = curl_exec($ch);
	$err = curl_error($ch);
	$code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
	curl_close($ch);
	return array($code, $raw, $err);
}

list($code, $raw, $err) = interactive_gateway_curl(
	$internalBase . "/auth-sign-in",
	$rootPath,
	$cookieFile,
	$ua
);
if ($err !== "" || $code >= 500) {
	@unlink($cookieFile);
	http_response_code(502);
	exit("Cannot reach interactive session");
}

$postFields = http_build_query(array(
	"username" => "rstudio",
	"password" => $password,
	"appUri" => "/",
	"staySignedIn" => "1",
	"persist" => "0",
	"clientPath" => "",
	"v" => "",
));

list($code, $raw, $err) = interactive_gateway_curl(
	$internalBase . "/auth-do-sign-in",
	$rootPath,
	$cookieFile,
	$ua,
	array(
		"post" => $postFields,
		"headers" => array("Content-Type: application/x-www-form-urlencoded"),
	)
);

if ($code < 200 || $code >= 400) {
	@unlink($cookieFile);
	http_response_code(502);
	exit("RStudio sign-in failed (HTTP $code)");
}

if (is_readable($cookieFile)) {
	$lines = file($cookieFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
	foreach ($lines as $line) {
		if ($line === "" || $line[0] === "#") {
			continue;
		}
		$parts = explode("\t", $line);
		if (count($parts) < 7) {
			continue;
		}
		$cookiePath = $parts[2];
		$cookieName = $parts[5];
		$cookieValue = $parts[6];
		if ($cookiePath !== $rootPath && strpos($cookiePath, $rootPath . "/") !== 0) {
			continue;
		}
		$expires = (int)$parts[4];
		$options = array(
			"path" => $cookiePath,
			"httponly" => true,
			"samesite" => "Lax",
		);
		if ($expires > 0) {
			$options["expires"] = $expires;
		}
		setcookie($cookieName, $cookieValue, $options);
	}
}
@unlink($cookieFile);

header("Location: " . $publicUrl);
exit;
