"""The audit record must say why a session failed, and keep its last logs."""

import json
import os
import tempfile
import unittest

from helpers import FakeKube, load_templates, session_body, settings

from openvre_scheduler import audit, sessions, state

NS = "openvre"
DEP = "/apis/apps/v1/namespaces/openvre/deployments"
PODS = "/api/v1/namespaces/openvre/pods"
EVENTS = "/api/v1/namespaces/openvre/events"
TPLS = load_templates()


def load(path):
    with open(path) as f:
        return json.load(f)


def read(path):
    with open(path) as f:
        return f.read()


def pod(name, app, containers, phase="Pending", restart=0):
    return {"metadata": {"name": name, "labels": {"app": app}},
            "spec": {"containers": [{"name": c} for c in containers]},
            "status": {"phase": phase, "containerStatuses": [
                {"name": c, "image": f"img/{c}", "ready": False, "restartCount": restart, "state": st}
                for c, st in containers.items()]}}


def event(obj, reason, message, kind="Pod", type_="Warning", ts="2026-11-10T11:56:10Z"):
    return {"involvedObject": {"kind": kind, "name": obj}, "reason": reason, "message": message,
            "type": type_, "lastTimestamp": ts}


class EvaluateTests(unittest.TestCase):
    def test_running(self):
        self.assertEqual(state.evaluate({"status": {"readyReplicas": 1}}, [])["state"], "Running")

    def test_image_pull(self):
        p = pod("p", "a", {"interactive": {"waiting": {"reason": "ImagePullBackOff", "message": "denied"}}})
        self.assertEqual(state.evaluate({"status": {}}, [p]), {"state": "Failed", "reason": "ImagePullBackOff", "message": "denied"})

    def test_quota_rejection_without_pod(self):
        dep = {"status": {"conditions": [{"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate",
                                          "message": "exceeded quota: project-quota"}]}}
        self.assertEqual(state.evaluate(dep, [])["reason"], "FailedCreate")

    def test_pending_is_not_failure(self):
        p = pod("p", "a", {"interactive": {"waiting": {"reason": "ContainerCreating"}}})
        self.assertEqual(state.evaluate({"status": {}}, [p]), {"state": "Pending", "reason": "ContainerCreating", "message": ""})

    def test_container_states(self):
        p = pod("p", "a", {"interactive": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}, restart=2)
        cs = state.container_states([p])[0]
        self.assertEqual((cs["state"], cs["reason"], cs["exit_code"], cs["restart_count"]), ("terminated", "OOMKilled", 137, 2))


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = settings(OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED="1", OPENVRE_INTERACTIVE_AUDIT_IMAGE="audit:1",
                          OPENVRE_INTERACTIVE_AUDIT_BASE=f"{self.tmp.name}/admindata")
        self.kube = FakeKube()
        self.dir = os.path.join(self.tmp.name, "admindata", "stata", "alice", "openvre-ix-stata-1")
        audit.update_meta(self.dir, status="starting", last_phase="create")
        self.name = "openvre-ix-stata-1"

    def tearDown(self):
        self.tmp.cleanup()

    def meta(self):
        return load(os.path.join(self.dir, "session-meta.json"))

    def add(self, dep_status, pods, events=()):
        self.kube.objects[f"{DEP}/{self.name}"] = {"metadata": {"name": self.name}, "status": dep_status}
        for p in pods:
            self.kube.objects[f"{PODS}/{p['metadata']['name']}"] = p
        for i, e in enumerate(events):
            self.kube.objects[f"{EVENTS}/e{i}"] = {**e, "metadata": {"name": f"e{i}"}}

    def test_failed_image_pull_is_recorded(self):
        p = pod(f"{self.name}-abc", self.name, {
            "interactive": {"waiting": {"reason": "ErrImagePull", "message": "pull access denied for registry.gitlab.bsc.es/x"}},
            "audit-sidecar": {"waiting": {"reason": "ImagePullBackOff", "message": "401 from ghcr.io"}}})
        self.add({}, [p], [event(p["metadata"]["name"], "Failed", "Failed to pull image: 401 Unauthorized"),
                           event("someone-else", "Failed", "not ours")])
        self.kube.logs[f"{p['metadata']['name']}/audit-sidecar"] = ""
        self.assertEqual(audit.watch_session(self.kube, self.s, self.dir, NS, self.name, timeout=5, interval=0), "failed")
        m = self.meta()
        self.assertEqual((m["status"], m["reason"], m["last_phase"]), ("failed", "ErrImagePull", "failed"))
        self.assertIn("pull access denied", m["message"])
        ev = load(os.path.join(self.dir, "k8s-events-failed.json"))["items"]
        self.assertEqual([e["message"] for e in ev], ["Failed to pull image: 401 Unauthorized"])
        cs = load(os.path.join(self.dir, "containers-failed.json"))
        self.assertEqual({c["container"]: c["reason"] for c in cs}, {"interactive": "ErrImagePull", "audit-sidecar": "ImagePullBackOff"})

    def test_crash_keeps_previous_logs(self):
        p = pod(f"{self.name}-abc", self.name, {"interactive": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}}}, restart=3)
        self.add({}, [p])
        self.kube.logs[f"{p['metadata']['name']}/interactive/previous"] = "Error: license file missing\n"
        audit.watch_session(self.kube, self.s, self.dir, NS, self.name, timeout=5, interval=0)
        log_file = os.path.join(self.dir, "logs", "failed", f"{p['metadata']['name']}__interactive__previous.log")
        self.assertEqual(read(log_file), "Error: license file missing\n")
        self.assertEqual(self.meta()["reason"], "CrashLoopBackOff")

    def test_quota_rejection_recorded_from_replicaset_events(self):
        cond = {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate", "message": "pods forbidden: exceeded quota"}
        self.add({"conditions": [cond]}, [], [event(f"{self.name}-6c9545f879", "FailedCreate", "exceeded quota", kind="ReplicaSet")])
        audit.watch_session(self.kube, self.s, self.dir, NS, self.name, timeout=5, interval=0)
        m = self.meta()
        self.assertEqual((m["status"], m["reason"]), ("failed", "FailedCreate"))
        ev = load(os.path.join(self.dir, "k8s-events-failed.json"))["items"]
        self.assertEqual(ev[0]["involvedObject"]["kind"], "ReplicaSet")

    def test_ready(self):
        p = pod(f"{self.name}-abc", self.name, {"interactive": {"running": {}}}, phase="Running")
        self.add({"readyReplicas": 1}, [p])
        self.kube.logs[f"{p['metadata']['name']}/interactive"] = "server started\n"
        self.assertEqual(audit.watch_session(self.kube, self.s, self.dir, NS, self.name, timeout=5, interval=0), "running")
        m = self.meta()
        self.assertEqual(m["status"], "running")
        self.assertIn("ready_at", m)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "logs", "running", f"{p['metadata']['name']}__interactive.log")))

    def test_timeout(self):
        p = pod(f"{self.name}-abc", self.name, {"interactive": {"waiting": {"reason": "ContainerCreating"}}})
        self.add({}, [p])
        self.assertEqual(audit.watch_session(self.kube, self.s, self.dir, NS, self.name, timeout=0, interval=0), "failed")
        m = self.meta()
        self.assertEqual((m["reason"], m["last_phase"]), ("NotReadyInTime", "timeout"))
        self.assertIn("ContainerCreating", m["message"])

    def test_deleted_before_ready(self):
        self.assertEqual(audit.watch_session(self.kube, self.s, self.dir, NS, self.name, timeout=5, interval=0), "gone")


class DeleteSnapshotTests(unittest.TestCase):
    def test_failure_survives_delete_and_logs_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED="1", OPENVRE_INTERACTIVE_AUDIT_IMAGE="audit:1",
                         OPENVRE_INTERACTIVE_AUDIT_BASE=f"{tmp}/admindata")
            kube = FakeKube()
            tpl, spec, values = sessions.parse_request(session_body(), s, TPLS)
            sessions.create(kube, s, tpl, spec, values)
            d = spec.audit_host_path
            p = pod(f"{spec.name}-abc", spec.name, {"interactive": {"running": {}}}, phase="Running")
            kube.objects[f"{PODS}/{p['metadata']['name']}"] = p
            kube.logs[f"{p['metadata']['name']}/interactive"] = "last words\n"
            audit.update_meta(d, status="failed", reason="ErrImagePull")
            sessions.delete(kube, s, NS, spec.name, reason="delete")
            m = load(os.path.join(d, "session-meta.json"))
            self.assertEqual((m["status"], m["reason"], m["last_phase"]), ("failed", "ErrImagePull", "delete"))
            self.assertEqual(read(os.path.join(d, "logs", "delete", f"{p['metadata']['name']}__interactive.log")), "last words\n")
            self.assertTrue(os.path.isfile(os.path.join(d, "containers-delete.json")))

    def test_normal_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED="1", OPENVRE_INTERACTIVE_AUDIT_IMAGE="audit:1",
                         OPENVRE_INTERACTIVE_AUDIT_BASE=f"{tmp}/admindata")
            kube = FakeKube()
            tpl, spec, values = sessions.parse_request(session_body(), s, TPLS)
            sessions.create(kube, s, tpl, spec, values)
            audit.update_meta(spec.audit_host_path, status="running")
            sessions.delete(kube, s, NS, spec.name, reason="expired")
            m = load(os.path.join(spec.audit_host_path, "session-meta.json"))
            self.assertEqual((m["status"], m["delete_reason"]), ("stopped", "expired"))


if __name__ == "__main__":
    unittest.main()
