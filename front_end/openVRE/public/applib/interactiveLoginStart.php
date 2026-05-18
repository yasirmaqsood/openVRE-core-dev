<?php
/**
 * Start OpenVRE login and return to an interactive session URL after OAuth.
 * Used as ingress-nginx auth-signin target (rd=$escaped_request_uri).
 */
require __DIR__ . "/../../config/bootstrap.php";

$returnPath = $_GET["rd"] ?? $_GET["redirect"] ?? "";
$returnPath = is_string($returnPath) ? $returnPath : "";

if ($returnPath !== "" && preg_match("#^/[a-z0-9_-]+/[^/]+#", $returnPath)) {
	$scheme = (!empty($_SERVER["HTTPS"]) && $_SERVER["HTTPS"] !== "off") ? "https" : "http";
	$host = $_SERVER["HTTP_HOST"] ?? "";
	if ($host !== "") {
		$_SESSION["interactive_return_url"] = $scheme . "://" . $host . $returnPath;
	}
}

redirect($GLOBALS["BASEURL"] . "login.php");
