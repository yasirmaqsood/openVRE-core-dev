import copy
import tempfile
import json
import unittest
from datetime import datetime, timedelta, timezone

from helpers import FakeKube, desktop_body, load_templates, session_body, settings

from openvre_scheduler import manifests as mf
from openvre_scheduler import policy, sessions
from openvre_scheduler import validation as v
from openvre_scheduler.k8s import K8sError

DEP_PATH = "/apis/apps/v1/namespaces/openvre/deployments"
TPLS = load_templates()


def parse(body, s):
    return sessions.parse_request(body, s, TPLS)


def render(body, s):
    tpl, spec, values = parse(body, s)
    objs, access = sessions.build(tpl, spec, values, s)
    return spec, {o["kind"]: o for o in objs}, access


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()

    def test_valid_request(self):
        tpl, spec, values = parse(session_body(), self.s)
        self.assertTrue(spec.name.startswith("openvre-ix-rstudio-"))
        self.assertLessEqual(len(spec.name), 63)
        self.assertEqual(spec.path_prefix, "/interactive-tool/" + spec.name[len("openvre-ix-"):])
        # The scheduler's own host wins over the Host header the frontend forwards.
        self.assertEqual(spec.external_base, "https://vre.example.org")
        self.assertEqual((spec.session_id, spec.template, spec.port), ("rstudio-abc12345", "openvre-web-session", 8787))
        self.assertEqual(values["cpu"], "1")

    def test_names_are_unique(self):
        self.assertNotEqual(parse(session_body(), self.s)[1].name, parse(session_body(), self.s)[1].name)

    def test_template_errors(self):
        with self.assertRaises(v.ValidationError) as cm:
            parse(session_body(template="nope"), self.s)
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaisesRegex(v.ValidationError, "not an InteractiveSession"):
            parse(session_body(template="openvre-batch-job"), self.s)
        with self.assertRaisesRegex(v.ValidationError, "unknown parameters"):
            parse(session_body(params={"image": "x/y:1", "port": 1, "privileged": True}), self.s)
        with self.assertRaisesRegex(v.ValidationError, "port is required"):
            parse(session_body(params={"image": "x/y:1"}), self.s)
        with self.assertRaisesRegex(v.ValidationError, "unknown context"):
            parse(session_body(context={"userdata_pvc": "admin-pvc"}), self.s)

    def test_rejects_path_escape(self):
        for wd in ("/shared_data/../etc", "/etc", "/shared_data"):
            with self.assertRaises(v.ValidationError, msg=wd):
                parse(session_body(working_dir=wd), self.s)
        body = session_body()
        body["context"]["volume_mounts"][0]["host_path"] = "/shared_data/userdata/bob/../../admindata"
        with self.assertRaises(v.ValidationError):
            parse(body, self.s)

    def test_rejects_admin_tree_paths(self):
        for wd in ("/shared_data/admindata", "/shared_data/admindata/rstudio/alice/x"):
            with self.assertRaises(v.ValidationError) as cm:
                parse(session_body(working_dir=wd), self.s)
            self.assertEqual(cm.exception.status, 403)

    def test_rejects_other_namespace(self):
        body = session_body()
        body["namespace"] = "kube-system"
        with self.assertRaises(v.ValidationError):
            parse(body, self.s)

    def test_resource_caps(self):
        s = settings(OPENVRE_MAX_SESSION_MEMORY="4Gi")
        with self.assertRaises(v.ValidationError):
            parse(session_body(params={"image": "x/y:1", "port": 8787, "memory": "8Gi"}), s)
        with self.assertRaisesRegex(v.ValidationError, "above the limit"):
            parse(session_body(params={"image": "x/y:1", "port": 8787, "memory": "64Gi"}), self.s)


class WebTemplateTests(unittest.TestCase):
    """The web template must build what the scheduler used to build in code."""

    def setUp(self):
        self.s = settings(OPENVRE_IMAGE_PULL_SECRETS="gitlab-registry")

    def test_objects(self):
        spec, objs, access = render(session_body(), self.s)
        self.assertEqual(list(objs), ["Deployment", "Service"])
        self.assertEqual(access, {"type": "web", "url": spec.path_prefix + "/"})
        for o in objs.values():
            self.assertEqual(o["metadata"]["namespace"], "openvre")
            self.assertEqual(o["metadata"]["labels"]["openvre.io/session"], spec.name)
            self.assertEqual(o["metadata"]["labels"]["openvre.io/interactive"], "true")

    def test_deployment(self):
        spec, objs, _ = render(session_body(), self.s)
        dep = objs["Deployment"]
        pod = dep["spec"]["template"]["spec"]
        self.assertEqual(dep["spec"]["selector"], {"matchLabels": {"app": spec.name}})
        self.assertEqual(dep["spec"]["strategy"], {"type": "Recreate"})
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["securityContext"], {"fsGroup": 1000})
        self.assertEqual(pod["imagePullSecrets"], [{"name": "gitlab-registry"}])
        tool = pod["containers"][0]
        self.assertNotIn("command", tool)  # no executable -> key left out
        self.assertEqual(tool["readinessProbe"]["tcpSocket"]["port"], 8787)
        mounts = {m["mountPath"]: m for m in tool["volumeMounts"]}
        self.assertEqual(mounts["/home/rstudio/repository"]["subPath"], "userdata/alice/proj/repository")
        self.assertTrue(mounts["/home/rstudio/repository"]["readOnly"])
        self.assertEqual(dep["metadata"]["labels"][mf.USER_LABEL], "alice")
        self.assertEqual(len(pod["containers"]), 1)  # audit off

    def test_optional_values(self):
        params = {"image": "x/y:1", "port": 8888, "executable": "start.sh --x", "readiness_path": "/api",
                  "env": {"A": "1"}, "proxy_root_mode": "x_forwarded_prefix", "proxy_headers": {"X-P": "{{PATH_PREFIX}}/z"}}
        spec, objs, _ = render(session_body(params=params), self.s)
        tool = objs["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(tool["command"], ["sh", "-lc", "start.sh --x"])
        self.assertEqual(tool["readinessProbe"]["httpGet"], {"path": "/api", "port": 8888})
        self.assertEqual(tool["env"], [{"name": "A", "value": "1"}])
        headers = json.loads(objs["Service"]["metadata"]["annotations"][mf.GATEWAY_HEADERS_ANNOTATION])
        self.assertEqual(headers["X-Forwarded-Prefix"], spec.path_prefix)
        self.assertEqual(headers["X-P"], spec.path_prefix + "/z")

    def test_gateway_annotations(self):
        spec, objs, _ = render(session_body(), self.s)
        ann = objs["Service"]["metadata"]["annotations"]
        self.assertEqual(ann[mf.GATEWAY_PATH_ANNOTATION], spec.path_prefix)
        self.assertEqual(json.loads(ann[mf.GATEWAY_HEADERS_ANNOTATION]), {"X-Forwarded-Proto": "https"})

    def test_http_external_base(self):
        _, objs, _ = render(session_body(), settings(OPENVRE_EXTERNAL_BASE="http://vre.local"))
        headers = json.loads(objs["Service"]["metadata"]["annotations"][mf.GATEWAY_HEADERS_ANNOTATION])
        self.assertEqual(headers["X-Forwarded-Proto"], "http")


class DesktopTemplateTests(unittest.TestCase):
    def test_objects_and_access(self):
        spec, objs, access = render(desktop_body(), settings())
        self.assertEqual(list(objs), ["Deployment", "Service"])
        self.assertEqual(access, {"type": "guacamole", "host": spec.name, "port": 5901, "protocol": "vnc"})
        self.assertEqual(objs["Service"]["spec"]["ports"][0]["port"], 5901)

    def test_protocol_checked(self):
        body = desktop_body()
        body["params"]["protocol"] = "ssh"
        with self.assertRaises(v.ValidationError):
            parse(body, settings())

    def test_no_gateway_route(self):
        _, objs, _ = render(desktop_body(), settings())
        self.assertNotIn(mf.GATEWAY_PATH_ANNOTATION, objs["Service"]["metadata"].get("annotations") or {})


class SessionPolicyTests(unittest.TestCase):
    """Whatever a session template produces, the policy keeps it in bounds."""

    def setUp(self):
        self.s = settings()
        tpl, self.spec, values = parse(session_body(), self.s)
        self.blocks = mf.building_blocks(self.spec, self.s, values)
        objs, _ = tpl.render(values, self.s, self.blocks)
        self.objs = objs

    def check(self, mutate, settings_=None):
        objs = copy.deepcopy(self.objs)
        mutate({o["kind"]: o for o in objs})
        return policy.check_session_objects(objs, self.spec, self.blocks, settings_ or self.s)

    def assertRejected(self, mutate, fragment, settings_=None):
        out, errs = self.check(mutate, settings_)
        self.assertIsNone(out)
        self.assertTrue(any(fragment in e for e in errs), errs)

    def pod(self, o):
        return o["Deployment"]["spec"]["template"]["spec"]

    def test_clean_passes(self):
        out, errs = self.check(lambda o: None)
        self.assertEqual(errs, [])

    def test_volume_types(self):
        self.assertRejected(lambda o: self.pod(o)["volumes"].append({"name": "h", "hostPath": {"path": "/"}}), "only persistentVolumeClaim")
        self.assertRejected(lambda o: self.pod(o)["volumes"].append({"name": "s", "secret": {"secretName": "x"}}), "only persistentVolumeClaim")
        self.assertRejected(lambda o: self.pod(o)["volumes"].append({"name": "c", "configMap": {"name": "kube-root-ca.crt"}}), "own ConfigMaps")
        self.assertRejected(lambda o: self.pod(o)["volumes"].append({"name": "p", "persistentVolumeClaim": {"claimName": "other"}}), "not allowed")

    def test_admin_volume_only_for_sidecar(self):
        def add_admin(o):
            self.pod(o)["volumes"].append({"name": "admindata", "persistentVolumeClaim": {"claimName": "admin-pvc"}})
            self.pod(o)["containers"][0]["volumeMounts"].append({"name": "admindata", "mountPath": "/a"})
        self.assertRejected(add_admin, "admin volume")

    def test_shared_pvc_root_and_audit_folder(self):
        s = settings(DEFAULT_ADMINDATA_PVC="shared-pvc")
        self.assertRejected(lambda o: self.pod(o)["containers"][0]["volumeMounts"].append({"name": "userdata", "mountPath": "/all"}),
                            "subPath outside the audit folder", s)
        self.assertRejected(lambda o: self.pod(o)["containers"][0]["volumeMounts"].append(
            {"name": "userdata", "mountPath": "/a", "subPath": "admindata/x"}), "subPath outside the audit folder", s)

    def test_pod_fields(self):
        self.assertRejected(lambda o: self.pod(o).update(hostNetwork=True), "hostNetwork")
        self.assertRejected(lambda o: self.pod(o).update(serviceAccountName="scheduler"), "serviceAccountName")
        self.assertRejected(lambda o: self.pod(o)["containers"][0].update(securityContext={"privileged": True}), "privileged")
        self.assertRejected(lambda o: self.pod(o)["containers"][0]["ports"].append({"containerPort": 1, "hostPort": 80}), "hostPort")
        self.assertRejected(lambda o: self.pod(o)["containers"][0].update(envFrom=[{"secretRef": {"name": "x"}}]), "envFrom")

    def test_init_containers_checked(self):
        self.assertRejected(lambda o: self.pod(o).update(initContainers=[{"name": "i", "image": "x/y:1",
                                                                          "securityContext": {"privileged": True}}]), "privileged")

    def test_forced_settings(self):
        def tamper(o):
            self.pod(o)["automountServiceAccountToken"] = True
            o["Deployment"]["spec"]["replicas"] = 5
            o["Deployment"]["spec"]["selector"] = {"matchLabels": {"app": "scheduler"}}
            o["Service"]["spec"]["selector"] = {"app": "dashboard-frontend"}
        out, errs = self.check(tamper)
        self.assertEqual(errs, [])
        k = {x["kind"]: x for x in out}
        self.assertFalse(k["Deployment"]["spec"]["template"]["spec"]["automountServiceAccountToken"])
        self.assertEqual(k["Deployment"]["spec"]["replicas"], 1)
        self.assertEqual(k["Service"]["spec"]["selector"], {"app": self.spec.name})

    def test_service_type(self):
        self.assertRejected(lambda o: o["Service"]["spec"].update(type="NodePort"), "ClusterIP")
        self.assertRejected(lambda o: o["Service"]["spec"].update(type="LoadBalancer"), "ClusterIP")

    def check_list(self, objs):
        return policy.check_session_objects(objs, self.spec, self.blocks, self.s)

    def test_no_ingress(self):
        objs = copy.deepcopy(self.objs) + [{"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                                            "metadata": {"name": self.spec.name}, "spec": {}}]
        out, errs = self.check_list(objs)
        self.assertIsNone(out)
        self.assertTrue(any("kind 'Ingress' is not allowed" in e for e in errs), errs)

    def test_gateway_annotations_forced(self):
        out, errs = self.check(lambda o: o["Service"]["metadata"].update(
            annotations={mf.GATEWAY_PATH_ANNOTATION: "/interactive-tool/someone-else"}))
        self.assertEqual(errs, [])
        svc = [x for x in out if x["kind"] == "Service"][0]
        self.assertEqual(svc["metadata"]["annotations"][mf.GATEWAY_PATH_ANNOTATION], self.spec.path_prefix)

    def test_web_session_needs_service(self):
        out, errs = self.check_list([o for o in copy.deepcopy(self.objs) if o["kind"] != "Service"])
        self.assertIsNone(out)
        self.assertTrue(any("needs a Service" in e for e in errs), errs)

    def test_names(self):
        self.assertRejected(lambda o: o["Service"]["metadata"].update(name="dashboard-frontend"), "start with")

    def test_strict_image_in_template(self):
        s = settings(OPENVRE_STRICT="1", OPENVRE_IMAGE_ALLOWLIST="ghcr.io/inab/*")
        self.assertRejected(lambda o: self.pod(o)["containers"].append({"name": "x", "image": "evil/miner"}), "ALLOWLIST", s)


class CreateDeleteTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.kube = FakeKube()

    def create(self, body=None, s=None):
        s = s or self.s
        tpl, spec, values = parse(body or session_body(), s)
        sessions.create(self.kube, s, tpl, spec, values)
        return spec

    def test_create_and_status(self):
        spec = self.create()
        self.assertEqual(len(self.kube.created), 2)
        self.assertEqual(sessions.status(self.kube, "openvre", spec.name)["state"], "Pending")
        self.kube.objects[f"{DEP_PATH}/{spec.name}"]["status"] = {"readyReplicas": 1}
        self.assertEqual(sessions.status(self.kube, "openvre", spec.name)["state"], "Running")

    def test_failed_state_from_pod(self):
        spec = self.create()
        self.kube.objects[f"/api/v1/namespaces/openvre/pods/{spec.name}-x"] = {
            "metadata": {"name": f"{spec.name}-x", "labels": {"app": spec.name}},
            "status": {"containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff", "message": "denied"}}}]},
        }
        st = sessions.status(self.kube, "openvre", spec.name)
        self.assertEqual((st["state"], st["reason"]), ("Failed", "ImagePullBackOff"))

    def test_rollback_only_own_objects(self):
        self.kube.fail_create_kind = "Service"
        with self.assertRaises(K8sError):
            self.create()
        self.assertEqual(self.kube.objects, {})
        self.assertEqual(len(self.kube.deleted), 1)

    def test_per_user_limit(self):
        s = settings(OPENVRE_MAX_SESSIONS_PER_USER="1")
        self.create(s=s)
        with self.assertRaises(v.ValidationError) as cm:
            self.create(s=s)
        self.assertEqual(cm.exception.status, 429)
        self.create(session_body(user_id="bob"), s=s)

    def test_delete_by_label(self):
        spec = self.create()
        found, errors = sessions.delete(self.kube, self.s, "openvre", spec.name)
        self.assertEqual((found, errors), (True, []))
        self.assertEqual(self.kube.objects, {})
        self.assertEqual(sessions.delete(self.kube, self.s, "openvre", spec.name), (False, []))

    def test_delete_by_name_without_session_label(self):
        """The Deployment and Service are also found by name (always the session name)."""
        name = "openvre-ix-rstudio-abc12345"
        lbl = {"openvre.io/interactive": "true", "app": name}
        for kind, path, oname in (("Deployment", DEP_PATH, name),
                                  ("Service", "/api/v1/namespaces/openvre/services", name)):
            self.kube.objects[f"{path}/{oname}"] = {"kind": kind, "metadata": {"name": oname, "labels": lbl}}
        found, errors = sessions.delete(self.kube, self.s, "openvre", name)
        self.assertEqual((found, errors), (True, []))
        self.assertEqual(self.kube.objects, {})

    def test_delete_refuses_unmanaged(self):
        name = "openvre-ix-not-ours"
        self.kube.objects[f"{DEP_PATH}/{name}"] = {"metadata": {"name": name, "labels": {"app": name}}}
        found, _ = sessions.delete(self.kube, self.s, "openvre", name)
        self.assertFalse(found)
        self.assertIn(f"{DEP_PATH}/{name}", self.kube.objects)

    def test_reaper(self):
        s = settings(OPENVRE_SESSION_TTL_HOURS="1")
        spec = self.create(s=s)
        self.assertEqual(sessions.reap_once(self.kube, s), [])
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.kube.objects[f"{DEP_PATH}/{spec.name}"]["metadata"]["annotations"][mf.EXPIRES_ANNOTATION] = past
        self.assertEqual(sessions.reap_once(self.kube, s), [spec.name])
        self.assertEqual(self.kube.objects, {})

    def test_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED="1", OPENVRE_INTERACTIVE_AUDIT_IMAGE="audit:1",
                         OPENVRE_INTERACTIVE_AUDIT_BASE=f"{tmp}/admindata", OPENVRE_STRICT="1",
                         OPENVRE_IMAGE_ALLOWLIST="ghcr.io/inab/*")
            spec = self.create(session_body(tool_id="my_tool", user_id="Alice@x"), s=s)
            self.assertTrue(spec.audit_host_path.startswith(f"{tmp}/admindata/my_tool/alice-x/openvre-ix-"))
            pod = self.kube.objects[f"{DEP_PATH}/{spec.name}"]["spec"]["template"]["spec"]
            sidecar = pod["containers"][1]
            # The sidecar image is exempt from the tool image allowlist.
            self.assertEqual((sidecar["name"], sidecar["image"]), ("audit-sidecar", "audit:1"))
            self.assertEqual(sidecar["volumeMounts"][-1]["subPath"], spec.audit_host_path[len(f"{tmp}/admindata/"):])
            self.assertEqual(pod["volumes"][1], {"name": "admindata", "persistentVolumeClaim": {"claimName": "admin-pvc"}})
            self.assertTrue(all(m["readOnly"] for m in sidecar["volumeMounts"][:-1]))


if __name__ == "__main__":
    unittest.main()
