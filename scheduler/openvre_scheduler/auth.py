"""Who is calling the scheduler, and for which user.

Two checks:

1. The caller: the shared bearer token (SCHEDULER_AUTH_TOKEN), compared in
   constant time. It only proves the request comes from the frontend.

2. The user (on whose behalf): the user's own Keycloak access token, sent by
   the frontend in X-OpenVRE-User-Token and checked against Keycloak's public
   keys. The scheduler takes the user from the token, never from the request
   body, and only lets a user see or stop their own sessions and Jobs. With
   OPENVRE_USER_TOKEN=required, the shared token alone cannot read or change
   any session or Job.
"""

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.request

import jwt


log = logging.getLogger("openvre_scheduler.auth")

OWNER_LABEL = "openvre.io/owner"
JWT_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]
KEY_TTL_SEC = 3600
KEY_REFRESH_MIN_SEC = 30
LEEWAY_SEC = 30


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.message = message
        self.status = status


class Caller:
    def __init__(self, name):
        self.name = name


class User:
    """A verified Keycloak user."""

    def __init__(self, claims, issuer, admin_roles):
        self.sub = str(claims.get("sub", ""))
        self.email = str(claims.get("email", ""))
        self.username = str(claims.get("preferred_username", ""))
        roles = set(claims.get("roles") or []) | set((claims.get("realm_access") or {}).get("roles") or [])
        for client in (claims.get("resource_access") or {}).values():
            roles |= set((client or {}).get("roles") or [])
        self.roles = sorted(roles)
        self.is_admin = bool(set(admin_roles) & roles)
        # Label value: stable per user, no personal data in Kubernetes labels.
        self.owner = "o-" + hashlib.sha256(f"{issuer}|{self.sub}".encode()).hexdigest()[:24]

    def log_name(self):
        return self.username or self.sub


class _KeySet:
    """Public signing keys from a JWKS document, cached and refreshed on unknown key ids."""

    def __init__(self, fetch):
        self._fetch = fetch      # () -> JWKS dict
        self._keys = {}
        self._loaded = 0.0
        self._lock = threading.Lock()

    def _load(self):
        jwks = self._fetch()
        keys = {}
        for k in jwks.get("keys", []):
            if k.get("use", "sig") != "sig":
                continue
            try:
                keys[k.get("kid", "")] = jwt.PyJWK(k)
            except jwt.PyJWTError as e:
                log.warning("ignoring unusable signing key", extra={"kid": k.get("kid"), "error": str(e)})
        self._keys = keys
        self._loaded = time.monotonic()

    def get(self, kid):
        with self._lock:
            age = time.monotonic() - self._loaded
            if not self._keys or age > KEY_TTL_SEC or (kid not in self._keys and age > KEY_REFRESH_MIN_SEC):
                self._load()
            key = self._keys.get(kid)
            if key is None and len(self._keys) == 1 and not kid:
                key = next(iter(self._keys.values()))
            if key is None:
                raise AuthError("token signed with an unknown key")
            return key


def _decode(token, keyset, options=None, **kwargs):
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise AuthError("malformed token")
    if header.get("alg") not in JWT_ALGORITHMS:
        raise AuthError("token algorithm not allowed")
    key = keyset.get(header.get("kid", ""))
    try:
        return jwt.decode(token, key.key, algorithms=[header["alg"]], leeway=LEEWAY_SEC,
                          options={"require": ["exp", "iat", "iss", "sub"], **(options or {})}, **kwargs)
    except jwt.ExpiredSignatureError:
        raise AuthError("token expired")
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid token: {e}")


class OIDCVerifier:
    """Checks the user's Keycloak access token."""

    def __init__(self, settings, fetch=None):
        self.s = settings
        self.keys = _KeySet(fetch or self._fetch_jwks)

    def _fetch_jwks(self):
        try:
            with urllib.request.urlopen(self.s.oidc_jwks_url, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - any network or parse error
            raise AuthError(f"cannot read Keycloak signing keys: {e}", 503)

    def verify(self, token):
        claims = _decode(token, self.keys, issuer=self.s.oidc_issuer, options={"verify_aud": False})
        if claims.get("typ", "Bearer") not in ("Bearer", "JWT", "at+jwt"):
            raise AuthError("not an access token")
        if self.s.oidc_client_id:
            aud = claims.get("aud") or []
            aud = [aud] if isinstance(aud, str) else aud
            if claims.get("azp") != self.s.oidc_client_id and self.s.oidc_client_id not in aud:
                raise AuthError("user token was not issued for this application")
        return User(claims, self.s.oidc_issuer, self.s.oidc_admin_roles)


class Authenticator:
    def __init__(self, settings, kube=None):
        self.s = settings
        self.oidc = OIDCVerifier(settings) if settings.user_token != "off" else None

    def caller(self, authorization_header):
        if not authorization_header.startswith("Bearer "):
            raise AuthError("unauthorized")
        token = authorization_header[len("Bearer "):].strip().encode("utf-8")
        ok = False
        for t in self.s.auth_tokens:
            # compare_digest takes the same time whether or not the token matches.
            ok |= hmac.compare_digest(token, t.encode("utf-8"))
        if not ok:
            raise AuthError("unauthorized")
        return Caller("frontend")

    def user(self, user_token_header):
        """Return a verified User, or None when no user token is sent and none is required."""
        token = (user_token_header or "").strip()
        if self.s.user_token == "off":
            return None
        if not token:
            if self.s.user_token == "required":
                raise AuthError("user token required (X-OpenVRE-User-Token); log in again")
            return None
        return self.oidc.verify(token)


def may_access(user, obj, settings):
    """Owner check for reading or deleting a session or Job.

    Objects without an owner label were created before user tokens were in use;
    they stay reachable unless user tokens are required.
    """
    if user is None:
        return settings.user_token != "required"
    owner = ((obj or {}).get("metadata", {}).get("labels") or {}).get(OWNER_LABEL)
    if owner is None:
        return settings.user_token != "required" or user.is_admin
    return owner == user.owner or user.is_admin
