"""Caller (shared token) and user (Keycloak token) checks, with real RSA signatures."""

import http.client
import json
import logging
import threading
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from helpers import TOKEN, FakeKube, load_templates, php_job_manifest, session_body, settings

from openvre_scheduler import auth, server
from openvre_scheduler.auth import AuthError

logging.disable(logging.CRITICAL)

KC_ISSUER = "http://auth.vre.local/auth/realms/demoproject"


def keypair(kid):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    jwk.update(kid=kid, use="sig", alg="RS256")
    return priv, jwk


KC_KEY, KC_JWK = keypair("kc-1")
OTHER_KEY, _ = keypair("kc-1")  # same kid, different key: a forgery


def user_token(sub="user-a", username="alice", roles=(), azp="open-vre", iss=KC_ISSUER, exp_in=300, typ="Bearer",
               key=KC_KEY):
    now = int(time.time())
    return jwt.encode({"iss": iss, "sub": sub, "preferred_username": username, "email": f"{username}@example.org",
                       "azp": azp, "typ": typ, "aud": "account", "realm_access": {"roles": list(roles)},
                       "iat": now, "exp": now + exp_in}, key, algorithm="RS256", headers={"kid": "kc-1"})


SECURE = dict(OPENVRE_USER_TOKEN="required",
              OPENVRE_OIDC_ISSUER=KC_ISSUER, OPENVRE_OIDC_CLIENT_ID="open-vre", OPENVRE_OIDC_ADMIN_ROLES="openvre-admin")


def authenticator(**env):
    a = auth.Authenticator(settings(**env))
    if a.oidc:
        a.oidc.keys = auth._KeySet(lambda: {"keys": [KC_JWK]})
    return a


class CallerTests(unittest.TestCase):
    def test_shared_token(self):
        a = authenticator(**SECURE)
        self.assertEqual(a.caller("Bearer " + TOKEN).name, "frontend")
        for bad in ("Bearer wrong-token-xxxxxxxxxxxx", "Bearer ", "Basic " + TOKEN, ""):
            with self.assertRaises(AuthError):
                a.caller(bad)

    def test_rotation_list(self):
        a = authenticator(SCHEDULER_AUTH_TOKEN="old-token-aaaaaaaaaaaa,new-token-bbbbbbbbbbbb")
        a.caller("Bearer old-token-aaaaaaaaaaaa")
        a.caller("Bearer new-token-bbbbbbbbbbbb")


class UserTests(unittest.TestCase):
    def setUp(self):
        self.a = authenticator(**SECURE)

    def test_valid(self):
        u = self.a.user(user_token(roles=["openvre-admin"]))
        self.assertEqual((u.sub, u.username, u.is_admin), ("user-a", "alice", True))
        self.assertEqual(u.owner, self.a.user(user_token()).owner)  # stable per user
        self.assertNotEqual(u.owner, self.a.user(user_token(sub="user-b")).owner)
        self.assertNotIn("alice", u.owner)  # no personal data in the label

    def test_rejected(self):
        cases = {
            "expired": user_token(exp_in=-120),
            "other client": user_token(azp="some-other-app"),
            "other realm": user_token(iss="http://auth.vre.local/auth/realms/other"),
            "id token": user_token(typ="ID"),
            "forged": user_token(key=OTHER_KEY),
            "garbage": "not-a-token",
        }
        for what, tok in cases.items():
            with self.assertRaises(AuthError, msg=what):
                self.a.user(tok)

    def test_none_algorithm_rejected(self):
        tok = jwt.encode({"iss": KC_ISSUER, "sub": "x", "iat": 1, "exp": 9999999999}, None, algorithm="none")
        with self.assertRaises(AuthError):
            self.a.user(tok)

    def test_modes(self):
        with self.assertRaisesRegex(AuthError, "user token required"):
            self.a.user("")
        self.assertIsNone(authenticator(**{**SECURE, "OPENVRE_USER_TOKEN": "optional"}).user(""))
        self.assertIsNone(authenticator().user(user_token()))  # off: ignored

    def test_config_needs_issuer(self):
        from openvre_scheduler import config
        with self.assertRaises(config.ConfigError):
            settings(OPENVRE_USER_TOKEN="required")


class OwnershipApiTests(unittest.TestCase):
    """End to end through HTTP, with both layers required."""

    def setUp(self):
        self.s = settings(**SECURE)
        self.s.listen_host, self.s.listen_port = "127.0.0.1", 0
        self.kube = FakeKube()
        self.srv = server.make_server(self.s, self.kube, load_templates())
        self.srv.RequestHandlerClass.auth.oidc.keys = auth._KeySet(lambda: {"keys": [KC_JWK]})
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None, user=None, caller=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Authorization": "Bearer " + (caller or TOKEN), "Content-Type": "application/json"}
        if user:
            h["X-OpenVRE-User-Token"] = user
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        out = json.loads(r.read() or b"{}")
        conn.close()
        return r.status, out

    def test_session_owner_only(self):
        alice, bob = user_token(sub="a", username="alice"), user_token(sub="b", username="bob")
        admin = user_token(sub="c", username="carol", roles=["openvre-admin"])
        status, body = self.call("POST", "/v1/interactive-sessions", session_body(), user=alice)
        self.assertEqual(status, 201, body)
        name = body["name"]
        dep = self.kube.objects[f"/apis/apps/v1/namespaces/openvre/deployments/{name}"]
        self.assertTrue(dep["metadata"]["labels"]["openvre.io/owner"].startswith("o-"))
        path = f"/v1/interactive-sessions/{name}?namespace=openvre"
        self.assertTrue(self.call("GET", path, user=alice)[1]["exists"])
        self.assertFalse(self.call("GET", path, user=bob)[1]["exists"])
        self.assertEqual(self.call("DELETE", path, user=bob)[0], 404)
        self.assertEqual(self.call("DELETE", path, user=admin)[0], 200)

    def test_job_owner_only(self):
        alice, bob = user_token(sub="a"), user_token(sub="b")
        body = {"namespace": "openvre", "template": "openvre-batch-job", "params": {
            "job_name": "openvre-bj-alice-1", "image": "ghcr.io/inab/tool:1.0",
            "workdir": "/shared_data/userdata/a/p/r", "submit_script": "/shared_data/userdata/a/p/r/.s"}}
        self.assertEqual(self.call("POST", "/v1/jobs", body, user=alice)[0], 201)
        job = self.kube.objects["/apis/batch/v1/namespaces/openvre/jobs/openvre-bj-alice-1"]
        self.assertEqual(job["metadata"]["labels"]["openvre.io/owner"],
                         job["spec"]["template"]["metadata"]["labels"]["openvre.io/owner"])
        path = "/v1/jobs/openvre-bj-alice-1?namespace=openvre"
        self.assertTrue(self.call("GET", path, user=alice)[1]["exists"])
        self.assertFalse(self.call("GET", path, user=bob)[1]["exists"])
        self.assertEqual(self.call("DELETE", path, user=bob)[0], 404)
        self.assertEqual(self.call("DELETE", path, user=alice)[0], 200)

    def test_missing_or_bad_tokens(self):
        # Shared token alone: no access to jobs or sessions when user tokens are required.
        self.assertEqual(self.call("GET", "/v1/jobs/openvre-bj-x?namespace=openvre")[0], 401)
        self.assertEqual(self.call("POST", "/v1/interactive-sessions", session_body())[0], 401)
        # Valid user token but wrong shared token: still refused.
        self.assertEqual(self.call("GET", "/v1/jobs/openvre-bj-x?namespace=openvre", user=user_token(),
                                   caller="wrong-token-xxxxxxxxxxxx")[0], 401)
        self.assertEqual(self.call("GET", "/v1/templates")[0], 200)  # no user needed

    def test_limit_counts_verified_owner(self):
        self.s.max_sessions_per_user = 1
        alice = user_token(sub="a")
        self.assertEqual(self.call("POST", "/v1/interactive-sessions", session_body(), user=alice)[0], 201)
        # Same person, different user_id in the body: still the same owner.
        self.assertEqual(self.call("POST", "/v1/interactive-sessions", session_body(user_id="someone-else"), user=alice)[0], 429)
        self.assertEqual(self.call("POST", "/v1/interactive-sessions", session_body(), user=user_token(sub="b"))[0], 201)


if __name__ == "__main__":
    unittest.main()
