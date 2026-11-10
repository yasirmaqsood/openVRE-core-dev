"""End-to-end tests: the real HTTP server on a random port, with FakeKube behind it."""

import http.client
import json
import logging
import os
import threading
import unittest

from helpers import TOKEN, FakeKube, desktop_body, php_job_manifest, session_body, settings

from openvre_scheduler import server, templates

logging.disable(logging.CRITICAL)

TPLS = templates.load_dir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"))
BATCH_PARAMS = {
    "job_name": "openvre-bj-alice-run-1a2b3c4d",
    "image": "ghcr.io/inab/tool:1.0",
    "workdir": "/shared_data/userdata/alice/p/run1",
    "submit_script": "/shared_data/userdata/alice/p/run1/.submit",
}


def batch(**overrides):
    return {"namespace": "openvre", "template": "openvre-batch-job", "params": dict(BATCH_PARAMS, **overrides)}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.s = settings(SCHEDULER_MAX_BODY_BYTES="200000", OPENVRE_ALLOW_RAW_JOB_MANIFESTS="1")
        self.s.listen_host, self.s.listen_port = "127.0.0.1", 0
        self.kube = FakeKube()
        self.srv = server.make_server(self.s, self.kube, TPLS)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        server.Handler._ready_cache.update(at=0.0, ok=False, error="")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None, token=TOKEN, raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, payload, resp

    def test_health_no_auth(self):
        status, body, _ = self.call("GET", "/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_readyz(self):
        self.assertEqual(self.call("GET", "/readyz", token=None)[0], 200)
        self.kube.unreachable = True
        server.Handler._ready_cache["at"] = 0.0
        self.assertEqual(self.call("GET", "/readyz", token=None)[0], 503)

    def test_auth(self):
        self.assertEqual(self.call("GET", "/jobs/openvre-bj-x", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/jobs/openvre-bj-x", token="wrong-token-xxxxxxxxxxxx")[0], 401)
        self.assertEqual(self.call("GET", "/jobs/openvre-bj-x?namespace=openvre")[0], 200)

    def test_request_id_echoed(self):
        _, _, resp = self.call("GET", "/healthz", token=None, headers={"X-Request-ID": "abc123"})
        self.assertEqual(resp.getheader("X-Request-ID"), "abc123")

    def test_job_lifecycle(self):
        status, body, _ = self.call("POST", "/v1/jobs", batch())
        self.assertEqual(status, 201, body)
        self.assertEqual(body["template"], "openvre-batch-job")
        name = body["name"]
        status, body, _ = self.call("GET", f"/jobs/{name}?namespace=openvre")
        self.assertTrue(body["exists"])
        self.assertEqual(body["status"]["state"], "Pending")
        self.assertIsInstance(body["job"], str)  # kept for older frontends
        path = f"/apis/batch/v1/namespaces/openvre/jobs/{name}"
        self.kube.objects[path]["status"] = {"failed": 1, "conditions": [
            {"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}
        _, body, _ = self.call("GET", f"/jobs/{name}?namespace=openvre")
        self.assertEqual(body["status"], {"state": "Failed", "reason": "DeadlineExceeded", "message": ""})
        self.assertEqual(self.call("DELETE", f"/v1/jobs/{name}?namespace=openvre")[0], 200)
        _, body, _ = self.call("DELETE", f"/jobs/{name}?namespace=openvre")
        self.assertIn("already deleted", body["stdout"])

    def test_job_policy_rejection(self):
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["volumes"].append({"name": "s", "secret": {"secretName": "x"}})
        status, body, _ = self.call("POST", "/jobs", {"namespace": "openvre", "manifest": m})
        self.assertEqual(status, 422)
        self.assertIn("policy", body["error"])
        self.assertEqual(self.kube.objects, {})

    def test_yaml_manifest_rejected(self):
        status, body, _ = self.call("POST", "/jobs", {"namespace": "openvre", "manifest": "apiVersion: batch/v1\nkind: Job\n"})
        self.assertEqual(status, 400)
        self.assertIn("JSON", body["error"])

    def test_namespace_forbidden(self):
        self.assertEqual(self.call("GET", "/jobs/openvre-bj-x?namespace=kube-system")[0], 403)
        status, _, _ = self.call("POST", "/jobs", {"namespace": "kube-system", "manifest": php_job_manifest(ns="kube-system")})
        self.assertEqual(status, 403)

    def test_path_tricks_rejected(self):
        for path in ("/jobs/..%2F..%2F..%2Fapi%2Fv1%2Fsecrets?namespace=openvre",
                     "/jobs/scheduler?namespace=openvre",
                     "/interactive-sessions/scheduler?namespace=openvre",
                     "/jobs/openvre-bj-x?namespace=openvre%2Fsecrets"):
            self.assertIn(self.call("GET", path)[0], (400, 403), path)
        self.assertEqual(self.call("DELETE", "/interactive-sessions/dashboard-frontend?namespace=openvre")[0], 400)

    def test_body_limits(self):
        status, _, _ = self.call("POST", "/jobs", raw=b"{not json")
        self.assertEqual(status, 400)
        status, _, _ = self.call("POST", "/jobs", raw=b"x" * 300000)
        self.assertEqual(status, 413)
        status, _, _ = self.call("POST", "/jobs", raw=b"[1,2]")
        self.assertEqual(status, 400)

    def test_session_lifecycle(self):
        status, body, _ = self.call("POST", "/interactive-sessions", session_body())
        self.assertEqual(status, 201, body)
        name = body["name"]
        self.assertEqual(body["access_url"], "/interactive-tool/" + name[len("openvre-ix-"):] + "/")
        self.assertEqual(body["access"], {"type": "web", "url": body["access_url"]})
        self.assertEqual(body["template"], "openvre-web-session")
        _, body, _ = self.call("GET", f"/interactive-sessions/{name}?namespace=openvre")
        self.assertEqual((body["exists"], body["state"]), (True, "Pending"))
        status, body, _ = self.call("DELETE", f"/interactive-sessions/{name}?namespace=openvre")
        self.assertEqual(status, 200)
        _, body, _ = self.call("DELETE", f"/interactive-sessions/{name}?namespace=openvre")
        self.assertIn("already deleted", body["stdout"])
        _, body, _ = self.call("GET", f"/interactive-sessions/{name}?namespace=openvre")
        self.assertFalse(body["exists"])

    def test_session_validation_error(self):
        status, body, _ = self.call("POST", "/interactive-sessions", session_body(working_dir="/etc"))
        self.assertEqual(status, 400)
        self.assertIn("working_dir", body["error"])

    def test_desktop_session(self):
        status, body, _ = self.call("POST", "/v1/interactive-sessions", desktop_body())
        self.assertEqual(status, 201, body)
        self.assertEqual(body["access"], {"type": "guacamole", "host": body["name"], "port": 5901, "protocol": "vnc"})
        self.assertEqual(body["access_url"], "")

    def test_k8s_unreachable_is_503(self):
        self.kube.unreachable = True
        status, body, _ = self.call("GET", "/jobs/openvre-bj-x?namespace=openvre")
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])

    def test_k8s_conflict_is_409(self):
        self.assertEqual(self.call("POST", "/jobs", batch())[0], 201)
        self.assertEqual(self.call("POST", "/jobs", batch())[0], 409)

    def test_template_errors(self):
        status, body, _ = self.call("POST", "/jobs", batch(workdir="/etc"))
        self.assertEqual(status, 400)
        self.assertIn("workdir", body["error"])
        status, _, _ = self.call("POST", "/jobs", {"namespace": "openvre", "template": "nope", "params": {}})
        self.assertEqual(status, 404)
        status, body, _ = self.call("POST", "/jobs", {"namespace": "openvre"})
        self.assertEqual(status, 400)

    def test_raw_manifest_can_be_disabled(self):
        self.s.allow_raw_job_manifests = False
        status, body, _ = self.call("POST", "/jobs", {"namespace": "openvre", "manifest": php_job_manifest()})
        self.assertEqual(status, 400)
        self.assertIn("raw manifests are disabled", body["error"])
        self.s.allow_raw_job_manifests = True

    def test_list_templates(self):
        self.assertEqual(self.call("GET", "/v1/templates", token=None)[0], 401)
        status, body, _ = self.call("GET", "/v1/templates")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(t["name"] for t in body["templates"]),
                         ["openvre-batch-job", "openvre-desktop-session", "openvre-web-session"])
        status, body, _ = self.call("GET", "/v1/templates/openvre-batch-job")
        self.assertIn("workdir", body["template"]["parameters"])
        self.assertEqual(self.call("GET", "/v1/templates/nope")[0], 404)

    def test_unknown_route(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("POST", "/jobs/openvre-bj-x?namespace=openvre", {})[0], 405)

    def test_parallel_requests(self):
        """The old server handled one request at a time. Make sure several run at once."""
        results = []

        def worker():
            results.append(self.call("GET", "/jobs/openvre-bj-x?namespace=openvre")[0])

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results, [200] * 20)


if __name__ == "__main__":
    unittest.main()
