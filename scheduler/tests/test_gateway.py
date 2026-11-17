"""Session routes for the gateway (Traefik): built from the session Services."""

import http.client
import json
import logging
import os
import threading
import unittest

from helpers import GATEWAY_TOKEN, TOKEN, FakeKube, session_body, settings

from openvre_scheduler import gateway, server, sessions, templates
from openvre_scheduler import manifests as mf

logging.disable(logging.CRITICAL)

TPLS = templates.load_dir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"))
SVC_PATH = "/api/v1/namespaces/openvre/services"


def start(kube, s, body):
    tpl, spec, values = sessions.parse_request(body, s, TPLS)
    sessions.create(kube, s, tpl, spec, values)
    return spec


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.kube = FakeKube()

    def test_no_sessions(self):
        self.assertEqual(gateway.build(self.kube, self.s), {})

    def test_one_session(self):
        spec = start(self.kube, self.s, session_body())
        http = gateway.build(self.kube, self.s)["http"]
        name = spec.name
        self.assertEqual(http["routers"][name], {
            "rule": f"PathPrefix(`{spec.path_prefix}`)",
            "priority": gateway.ROUTER_PRIORITY,
            "entryPoints": ["web"],
            "middlewares": [f"{name}-strip", f"{name}-headers"],
            "service": name,
        })
        self.assertEqual(http["middlewares"][f"{name}-strip"], {"stripPrefix": {"prefixes": [spec.path_prefix]}})
        self.assertEqual(http["middlewares"][f"{name}-headers"]["headers"]["customRequestHeaders"],
                         {"X-Forwarded-Proto": "https"})
        self.assertEqual(http["services"][name]["loadBalancer"]["servers"],
                         [{"url": f"http://{name}.openvre.svc:8787"}])

    def test_header_modes(self):
        params = {"image": "x/y:1", "port": 8888, "proxy_root_mode": "custom_header",
                  "proxy_root_header": "X-RStudio-Root-Path", "proxy_headers": {"X-P": "{{PATH_PREFIX}}/z"}}
        spec = start(self.kube, self.s, session_body(params=params))
        http = gateway.build(self.kube, self.s)["http"]
        headers = http["middlewares"][f"{spec.name}-headers"]["headers"]["customRequestHeaders"]
        self.assertEqual(headers["X-RStudio-Root-Path"], spec.path_prefix)
        self.assertEqual(headers["X-P"], spec.path_prefix + "/z")
        self.assertTrue(http["services"][spec.name]["loadBalancer"]["servers"][0]["url"].endswith(":8888"))

    def test_forward_auth(self):
        s = settings(OPENVRE_INTERACTIVE_AUTH_URL="http://auth.example/check")
        spec = start(self.kube, s, session_body())
        http = gateway.build(self.kube, s)["http"]
        self.assertEqual(http["routers"][spec.name]["middlewares"][0], f"{spec.name}-auth")
        self.assertEqual(http["middlewares"][f"{spec.name}-auth"]["forwardAuth"]["address"], "http://auth.example/check")

    def test_several_sessions_and_delete(self):
        a = start(self.kube, self.s, session_body())
        b = start(self.kube, self.s, session_body(user_id="bob"))
        self.assertEqual(set(gateway.build(self.kube, self.s)["http"]["routers"]), {a.name, b.name})
        sessions.delete(self.kube, self.s, "openvre", a.name)
        self.assertEqual(set(gateway.build(self.kube, self.s)["http"]["routers"]), {b.name})

    def test_other_services_ignored(self):
        # Interactive label but not made by the scheduler, or made by it without a gateway path.
        self.kube.objects[f"{SVC_PATH}/foreign"] = {"metadata": {
            "name": "foreign", "namespace": "openvre", "labels": {mf.INTERACTIVE_LABEL: "true"},
            "annotations": {mf.GATEWAY_PATH_ANNOTATION: "/interactive-tool/foreign"}},
            "spec": {"ports": [{"port": 80}]}}
        self.kube.objects[f"{SVC_PATH}/no-path"] = {"metadata": {
            "name": "no-path", "namespace": "openvre",
            "labels": {mf.INTERACTIVE_LABEL: "true", "app.kubernetes.io/managed-by": mf.MANAGED_BY}},
            "spec": {"ports": [{"port": 80}]}}
        self.assertEqual(gateway.build(self.kube, self.s), {})

    def test_path_must_be_a_session_path(self):
        self.kube.objects[f"{SVC_PATH}/sneaky"] = {"metadata": {
            "name": "sneaky", "namespace": "openvre",
            "labels": {mf.INTERACTIVE_LABEL: "true", "app.kubernetes.io/managed-by": mf.MANAGED_BY},
            "annotations": {mf.GATEWAY_PATH_ANNOTATION: "/auth"}},
            "spec": {"ports": [{"port": 80}]}}
        self.assertEqual(gateway.build(self.kube, self.s), {})


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.s.listen_host, self.s.listen_port = "127.0.0.1", 0
        self.kube = FakeKube()
        self.srv = server.make_server(self.s, self.kube, TPLS)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        gateway.clear_cache()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/gateway/routes", headers=headers)
        resp = conn.getresponse()
        body = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, body

    def test_needs_gateway_token(self):
        self.assertEqual(self.get({})[0], 401)
        self.assertEqual(self.get({"X-OpenVRE-Gateway-Token": "wrong"})[0], 401)
        # The frontend's scheduler token is not the gateway token.
        self.assertEqual(self.get({"X-OpenVRE-Gateway-Token": TOKEN})[0], 401)
        self.assertEqual(self.get({"Authorization": f"Bearer {TOKEN}"})[0], 401)

    def test_routes(self):
        status, body = self.get({"X-OpenVRE-Gateway-Token": GATEWAY_TOKEN})
        self.assertEqual((status, body), (200, {}))
        spec = start(self.kube, self.s, session_body())
        gateway.clear_cache()
        status, body = self.get({"X-OpenVRE-Gateway-Token": GATEWAY_TOKEN})
        self.assertEqual(status, 200)
        self.assertIn(spec.name, body["http"]["routers"])


class ConfigTests(unittest.TestCase):
    def test_gateway_token_checked_when_set(self):
        from openvre_scheduler import config
        with self.assertRaisesRegex(config.ConfigError, "at least 16"):
            settings(OPENVRE_GATEWAY_TOKEN="short")
        with self.assertRaisesRegex(config.ConfigError, "must differ"):
            settings(OPENVRE_GATEWAY_TOKEN=TOKEN)

    def test_no_gateway_is_allowed(self):
        self.assertEqual(settings(OPENVRE_GATEWAY_TOKEN="").gateway_token, "")


class NoGatewayTests(unittest.TestCase):
    """A site without the gateway (OPENVRE_GATEWAY_TOKEN empty): batch Jobs only."""

    def setUp(self):
        self.s = settings(OPENVRE_GATEWAY_TOKEN="")
        self.kube = FakeKube()

    def test_web_session_refused(self):
        with self.assertRaises(sessions.v.ValidationError) as cm:
            sessions.parse_request(session_body(), self.s, TPLS)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("web tools are not enabled", cm.exception.message)
        self.assertEqual(self.kube.created, [])

    def test_routes_endpoint_off(self):
        self.s.listen_host, self.s.listen_port = "127.0.0.1", 0
        srv = server.make_server(self.s, self.kube, TPLS)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            conn.request("GET", "/gateway/routes", headers={"X-OpenVRE-Gateway-Token": GATEWAY_TOKEN})
            self.assertEqual(conn.getresponse().status, 404)
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
