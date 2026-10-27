<?php

require_once __DIR__ . "/../../config/bootstrap.php";

$_SESSION = [];
if (ini_get("session.use_cookies")) {
    $params = session_get_cookie_params();
    setcookie(
        session_name(),
        "",
        time() - 42000,
        $params["path"] ?: "/",
        $params["domain"],
        $params["secure"],
        $params["httponly"]
    );
}
session_destroy();

$returnTo = $GLOBALS["URL"];
$location = $GLOBALS["urlLogout"]
    . "?client_id=" . rawurlencode(getenv("KEYCLOAK_CLIENT"))
    . "&post_logout_redirect_uri=" . rawurlencode($returnTo);

header("Location: " . $location);
exit;
