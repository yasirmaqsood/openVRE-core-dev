import os
import tempfile
import textwrap
import unittest

from helpers import settings

from openvre_scheduler import jobs, templates
from openvre_scheduler import validation as v

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

GOOD_PARAMS = {
    "job_name": "openvre-bj-alice-run-1a2b3c4d",
    "image": "ghcr.io/inab/tool:1.0",
    "workdir": "/shared_data/userdata/alice/p/run1",
    "submit_script": "/shared_data/userdata/alice/p/run1/.submit",
}

MINIMAL = """
name: t
kind: Job
name_param: job_name
parameters:
  job_name: {type: name, prefix: openvre-bj-, required: true}
{extra_params}
manifest:
  apiVersion: batch/v1
  kind: Job
  metadata:
    name: "{{ job_name }}"
  spec:
    template:
      spec:
        containers:
          - name: c
            image: busybox
{extra_manifest}
"""


def minimal(extra_params="", extra_manifest=""):
    # Plain replace, not str.format: the YAML itself is full of braces.
    return (MINIMAL
            .replace("{extra_params}", textwrap.indent(textwrap.dedent(extra_params), "  "))
            .replace("{extra_manifest}", textwrap.indent(textwrap.dedent(extra_manifest), "            ")))


def make(extra_params="", extra_manifest=""):
    text = minimal(extra_params, extra_manifest)
    return templates.Template(templates.load_yaml(text, "t.yaml"), "t.yaml")


class LoadTests(unittest.TestCase):
    def test_shipped_templates_load(self):
        tpls = templates.load_dir(TEMPLATE_DIR)
        self.assertIn("openvre-batch-job", tpls)
        desc = tpls["openvre-batch-job"].describe()
        self.assertTrue(desc["parameters"]["job_name"]["required"])
        self.assertEqual(desc["parameters"]["cpu"]["default"], "1")

    def test_minimal_is_valid(self):
        make()

    def test_duplicate_keys_rejected(self):
        with self.assertRaisesRegex(templates.TemplateError, "duplicate key"):
            templates.load_yaml("a: 1\na: 2\n")

    def test_unquoted_octal_default_caught(self):
        # YAML 1.1 reads 0640 as the number 416. The string type catches it.
        with self.assertRaisesRegex(templates.TemplateError, "invalid default"):
            make(extra_params="mode: {type: string, default: 0640}", extra_manifest='args: ["{{ mode }}"]')
        make(extra_params='mode: {type: string, default: "0640"}', extra_manifest='args: ["{{ mode }}"]')

    def test_unknown_placeholder(self):
        with self.assertRaisesRegex(templates.TemplateError, "unknown parameter"):
            make(extra_manifest='args: ["{{ nope }}"]')
        with self.assertRaisesRegex(templates.TemplateError, "unknown config"):
            make(extra_manifest='args: ["{{ config.secret_token }}"]')

    def test_unused_parameter(self):
        with self.assertRaisesRegex(templates.TemplateError, "never used"):
            make(extra_params="x: {type: string}")

    def test_list_rules(self):
        with self.assertRaisesRegex(templates.TemplateError, "inside other text"):
            make(extra_params="env: {type: env}", extra_manifest='args: ["a {{ env }}"]')
        with self.assertRaisesRegex(templates.TemplateError, "needs a list"):
            make(extra_params="x: {type: string}", extra_manifest='args: ["{{ ...x }}"]')
        with self.assertRaisesRegex(templates.TemplateError, "whole list item"):
            make(extra_params="env: {type: env}", extra_manifest='env: "{{ ...env }}"')

    def test_bad_structure(self):
        with self.assertRaisesRegex(templates.TemplateError, "unknown keys"):
            templates.Template({"name": "t", "kind": "Job", "name_param": "x", "parameters": {},
                                "manifest": {}, "extra": 1}, "t")
        with self.assertRaisesRegex(templates.TemplateError, "type must be one of"):
            make(extra_params="x: {type: dict}", extra_manifest='args: ["{{ x }}"]')
        with self.assertRaisesRegex(templates.TemplateError, "needs 'under"):
            make(extra_params="p: {type: path}", extra_manifest='args: ["{{ p }}"]')

    def test_metadata_name_must_be_name_param(self):
        data = templates.load_yaml(minimal(), "t")
        data["manifest"]["metadata"]["name"] = "fixed-name"
        with self.assertRaisesRegex(templates.TemplateError, "metadata.name"):
            templates.Template(data, "t")

    def test_file_name_must_match(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "other.yaml"), "w") as f:
                f.write(minimal())
            with self.assertRaisesRegex(templates.TemplateError, "must match the file name"):
                templates.load_dir(d)

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(templates.TemplateError, "no templates"):
                templates.load_dir(d)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.s = settings(OPENVRE_NODE_SELECTOR="openvre.project=demo", OPENVRE_ALLOWED_PULL_SECRETS="gitlab-registry")
        self.tpl = templates.load_dir(TEMPLATE_DIR)["openvre-batch-job"]

    def render(self, **overrides):
        params = dict(GOOD_PARAMS)
        params.update(overrides)
        return self.tpl.render(params, self.s)[0]

    def test_defaults_and_config(self):
        m = self.render()
        pod = m["spec"]["template"]["spec"]
        self.assertEqual(m["spec"]["activeDeadlineSeconds"], 86400)
        self.assertIsInstance(m["spec"]["activeDeadlineSeconds"], int)
        self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
        self.assertEqual(pod["nodeSelector"], {"openvre.project": "demo"})
        self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], "shared-pvc")
        self.assertEqual(m["metadata"]["labels"]["openvre.io/template"], "openvre-batch-job")

    def test_job_uid_from_scheduler_config(self):
        s = settings(OPENVRE_JOB_RUN_AS_UID="2000", OPENVRE_JOB_RUN_AS_GID="2000")
        pod = self.tpl.render(dict(GOOD_PARAMS), s)[0]["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"]["runAsUser"], 2000)

    def test_env_spread_and_fixed_vars_last(self):
        env = self.render(env={"FOO": "bar", "OPENVRE_SUBMIT_SCRIPT": "/evil"})["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual([e["name"] for e in env], ["FOO", "OPENVRE_SUBMIT_SCRIPT", "OPENVRE_WORKDIR", "OPENVRE_SUBMIT_SCRIPT"])
        self.assertEqual(env[-1]["value"], GOOD_PARAMS["submit_script"])

    def test_value_cannot_change_structure(self):
        evil = 'x\n  privileged: true\n"}, {"hostPath": "/"'
        env = self.render(env={"X": evil})["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual(env[0], {"name": "X", "value": evil})

    def test_bad_params(self):
        cases = [
            ({"job_name": "scheduler"}, "must start with"),
            ({"workdir": "/etc"}, "must be under"),
            ({"workdir": "/shared_data/../etc"}, ".."),
            ({"cpu": "64"}, "above the limit"),
            ({"cpu": 2}, "must be a string"),
            ({"deadline_seconds": 10}, "at least"),
            ({"deadline_seconds": "100"}, "must be an integer"),
            ({"env": {"BAD-NAME": "x"}}, "invalid variable name"),
            ({"env": ["A=1"]}, "mapping"),
            ({"image_pull_secrets": ["scheduler-auth"]}, "not allowed"),
            ({"privileged": True}, "unknown parameters"),
        ]
        for overrides, fragment in cases:
            with self.assertRaises(v.ValidationError, msg=str(overrides)) as cm:
                self.render(**overrides)
            self.assertIn(fragment, cm.exception.message, overrides)

    def test_required(self):
        params = dict(GOOD_PARAMS)
        del params["image"]
        with self.assertRaisesRegex(v.ValidationError, "image is required"):
            self.tpl.render(params, self.s)

    def test_strict_image_applies(self):
        s = settings(OPENVRE_STRICT="1", OPENVRE_IMAGE_ALLOWLIST="ghcr.io/inab/*")
        self.tpl.render(dict(GOOD_PARAMS), s)
        with self.assertRaises(v.ValidationError):
            self.tpl.render(dict(GOOD_PARAMS, image="evil/miner"), s)


class JobsBuildTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()
        self.tpls = templates.load_dir(TEMPLATE_DIR)

    def test_template_job_passes_policy(self):
        job, source = jobs.build({"template": "openvre-batch-job", "params": GOOD_PARAMS}, "openvre", self.s, self.tpls)
        self.assertEqual(source, "openvre-batch-job")
        self.assertTrue(job["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"])

    def test_policy_still_checks_template_output(self):
        bad = make(extra_manifest='volumeMounts: [{name: h, mountPath: /host}]')
        bad.manifest["spec"]["template"]["spec"]["volumes"] = [{"name": "h", "hostPath": {"path": "/"}}]
        bad.manifest["spec"]["activeDeadlineSeconds"] = 60
        with self.assertRaises(v.ValidationError) as cm:
            jobs.build({"template": "t", "params": {"job_name": "openvre-bj-x"}}, "openvre", self.s, {"t": bad})
        self.assertEqual(cm.exception.status, 422)

    def test_unknown_template(self):
        with self.assertRaises(v.ValidationError) as cm:
            jobs.build({"template": "nope", "params": {}}, "openvre", self.s, self.tpls)
        self.assertEqual(cm.exception.status, 404)

    def test_raw_manifest_off_by_default(self):
        with self.assertRaisesRegex(v.ValidationError, "raw manifests are disabled"):
            jobs.build({"manifest": {}}, "openvre", self.s, self.tpls)


SESSION = """
name: s
kind: InteractiveSession
access: {type: web}
parameters:
  image: {type: image, required: true}
  port: {type: int, required: true}
objects:
  - apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: "{{ session.name }}"
    spec:
      template:
        spec:
          containers:
            - name: c
              image: "{{ image }}"
              command: "{{ session.command }}"
"""


def session_tpl(mutate=None):
    data = templates.load_yaml(SESSION, "s.yaml")
    if mutate:
        mutate(data)
    return templates.Template(data, "s.yaml")


class SessionTemplateLoadTests(unittest.TestCase):
    def test_minimal(self):
        t = session_tpl()
        self.assertIsInstance(t, templates.SessionTemplate)

    def test_needs_port(self):
        with self.assertRaisesRegex(templates.TemplateError, "port"):
            session_tpl(lambda d: d["parameters"].pop("port"))

    def test_reserved_types(self):
        with self.assertRaisesRegex(templates.TemplateError, "fixed meaning"):
            session_tpl(lambda d: d["parameters"].update(port={"type": "string", "required": True}))

    def test_object_kinds(self):
        def add_secret(d):
            d["objects"].append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "{{ session.name }}-s"}})
        with self.assertRaisesRegex(templates.TemplateError, "kind must be one of"):
            session_tpl(add_secret)

    def test_one_deployment(self):
        with self.assertRaisesRegex(templates.TemplateError, "exactly one Deployment"):
            session_tpl(lambda d: d["objects"].append(dict(d["objects"][0])))

    def test_object_names(self):
        def rename(d):
            d["objects"].append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "dashboard-frontend"}})
        with self.assertRaisesRegex(templates.TemplateError, "metadata.name"):
            session_tpl(rename)

    def test_unknown_session_value(self):
        def bad(d):
            d["objects"][0]["metadata"]["labels"] = "{{ session.secrets }}"
        with self.assertRaisesRegex(templates.TemplateError, "unknown session value"):
            session_tpl(bad)

    def test_session_refs_only_in_session_templates(self):
        with self.assertRaisesRegex(templates.TemplateError, "only for InteractiveSession"):
            make(extra_manifest='args: ["{{ session.name }}"]')

    def test_access(self):
        with self.assertRaisesRegex(templates.TemplateError, "access.type"):
            session_tpl(lambda d: d.update(access={"type": "ssh"}))
        with self.assertRaisesRegex(templates.TemplateError, "protocol is required"):
            session_tpl(lambda d: d.update(access={"type": "guacamole"}))

    def test_none_value_drops_key(self):
        out = templates.fill({"a": "{{ x }}", "b": 1, "l": ["{{ x }}", 2]}, lambda ref: None)
        self.assertEqual(out, {"b": 1, "l": [2]})

    def test_describe(self):
        tpls = templates.load_dir(TEMPLATE_DIR)
        web = tpls["openvre-web-session"].describe()
        self.assertEqual(web["objects"], ["Deployment", "Service"])
        self.assertEqual(web["access"], {"type": "web"})
        self.assertEqual(tpls["openvre-desktop-session"].describe()["access"], {"type": "guacamole"})


if __name__ == "__main__":
    unittest.main()
