import unittest

from helpers import BASE_ENV, settings

from openvre_scheduler import config
from openvre_scheduler import validation as v


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        s = settings()
        self.assertEqual(s.allowed_namespaces, ["openvre"])
        self.assertEqual(s.allowed_pvcs, ["shared-pvc"])
        self.assertFalse(s.strict)

    def test_empty_token_fails(self):
        env = dict(BASE_ENV, SCHEDULER_AUTH_TOKEN="")
        with self.assertRaises(config.ConfigError):
            config.load(env)

    def test_short_token_fails(self):
        with self.assertRaises(config.ConfigError):
            config.load(dict(BASE_ENV, SCHEDULER_AUTH_TOKEN="short"))

    def test_token_rotation_list(self):
        s = settings(SCHEDULER_AUTH_TOKEN="old-token-aaaaaaaaaaaa,new-token-bbbbbbbbbbbb")
        self.assertEqual(len(s.auth_tokens), 2)

    def test_audit_needs_image(self):
        with self.assertRaises(config.ConfigError):
            settings(OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED="true")
        s = settings(OPENVRE_INTERACTIVE_AUDIT_SIDECAR_ENABLED="true", OPENVRE_INTERACTIVE_AUDIT_IMAGE="audit:1")
        self.assertTrue(s.audit_enabled)

    def test_strict_needs_allowlist(self):
        with self.assertRaises(config.ConfigError):
            settings(OPENVRE_STRICT="1")

    def test_service_link_env_ignored(self):
        # Kubernetes sets SCHEDULER_PORT=tcp://... for a Service named "scheduler" (found in the dev cluster).
        s = settings(SCHEDULER_PORT="tcp://10.100.64.199:8080", SCHEDULER_LISTEN_PORT="9090")
        self.assertEqual(s.listen_port, 9090)
        self.assertEqual(settings(SCHEDULER_PORT="tcp://10.0.0.1:8080").listen_port, 8080)

    def test_placeholder_token_detected(self):
        self.assertTrue(config.is_placeholder_token(settings(SCHEDULER_AUTH_TOKEN="CHANGEME-scheduler-token")))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.s = settings(OPENVRE_ALLOWED_NAMESPACES="openvre,other")

    def test_namespace(self):
        self.assertEqual(v.namespace("", self.s), "openvre")
        self.assertEqual(v.namespace("other", self.s), "other")
        with self.assertRaises(v.ValidationError) as cm:
            v.namespace("kube-system", self.s)
        self.assertEqual(cm.exception.status, 403)
        for bad in ("openvre/secrets", "../x", "Openvre", "a" * 64):
            with self.assertRaises(v.ValidationError):
                v.namespace(bad, self.s)

    def test_names_need_prefix(self):
        self.assertEqual(v.job_name("openvre-bj-x-1"), "openvre-bj-x-1")
        for bad in ("scheduler", "openvre-ix-x", "openvre-bj-x/../../secrets", "openvre-bj-X"):
            with self.assertRaises(v.ValidationError):
                v.job_name(bad)
        with self.assertRaises(v.ValidationError):
            v.session_name("dashboard-frontend")

    def test_paths(self):
        self.assertEqual(v.path_under("/shared_data/userdata/a//b/", "/shared_data", "p"), "/shared_data/userdata/a/b")
        for bad in ("/shared_data/../etc", "/shared_data/userdata/../../etc", "relative/path", "/etc/passwd", "/shared_dataX/a"):
            with self.assertRaises(v.ValidationError):
                v.path_under(bad, "/shared_data", "p")
        self.assertEqual(v.subpath("/shared_data/userdata/a", "/shared_data"), "userdata/a")

    def test_quantities(self):
        for ok in ("500m", "1", "1.5", "2Gi", "1536Mi"):
            v.quantity(ok, "cpu")
        for bad in ("1 Gi", "-1", "lots", "1GiB"):
            with self.assertRaises(v.ValidationError):
                v.quantity(bad, "cpu")
        v.at_most("2Gi", "4Gi", "memory")
        with self.assertRaises(v.ValidationError):
            v.at_most("8Gi", "4Gi", "memory")
        with self.assertRaises(v.ValidationError):
            v.at_most("2", "1500m", "cpu")

    def test_image_allowlist(self):
        s = settings(OPENVRE_STRICT="1", OPENVRE_IMAGE_ALLOWLIST="ghcr.io/inab/*,docker.io/library/python:3.11")
        v.image("ghcr.io/inab/anything:2", s)
        v.image("docker.io/library/python:3.11", s)
        with self.assertRaises(v.ValidationError) as cm:
            v.image("docker.io/library/python:3.12", s)
        self.assertEqual(cm.exception.status, 403)
        # Not strict: allowed (only logged).
        v.image("evil/miner:latest", settings(OPENVRE_IMAGE_ALLOWLIST="ghcr.io/inab/*"))

    def test_label_value(self):
        self.assertEqual(v.label_value("alice"), "alice")
        hashed = v.label_value("alice@example.org/with spaces")
        self.assertTrue(hashed.startswith("u-") and len(hashed) <= 63)
        self.assertEqual(hashed, v.label_value("alice@example.org/with spaces"))

    def test_headers(self):
        self.assertEqual(v.header_map({"X-Prefix": "{{PATH_PREFIX}}"}, "h"), {"X-Prefix": "{{PATH_PREFIX}}"})
        with self.assertRaises(v.ValidationError):
            v.header_map({"X-Bad": "a\r\nInjected: 1"}, "h")
        with self.assertRaises(v.ValidationError):
            v.header_map({"Bad Name": "x"}, "h")

    def test_pull_secrets(self):
        s = settings(OPENVRE_IMAGE_PULL_SECRETS="gitlab-registry", OPENVRE_ALLOWED_PULL_SECRETS="other-registry")
        self.assertEqual(v.pull_secrets(["other-registry"], s), ["other-registry"])
        with self.assertRaises(v.ValidationError):
            v.pull_secrets(["scheduler-auth"], s)



class ImageRuleTests(unittest.TestCase):
    PROD = dict(OPENVRE_STRICT="1",
                OPENVRE_ALLOWED_REGISTRIES="registry.gitlab.bsc.es/impact-cohorte,docker.io/ymaqsoodbsc,ghcr.io/inab")

    def allowed(self, image, **env):
        return v.image_allowed(image, settings(**(env or self.PROD)))

    def test_registry_namespace_covers_new_images(self):
        for img in ("registry.gitlab.bsc.es/impact-cohorte/impd-openvre:stata19web",
                    "registry.gitlab.bsc.es/impact-cohorte/some-new-tool:2.0",
                    "registry.gitlab.bsc.es/impact-cohorte/team/nested:1",
                    "ymaqsoodbsc/fem-tool",
                    "docker.io/ymaqsoodbsc/openvre-kubernetes:rstudio-init-wrapper-2",
                    "index.docker.io/ymaqsoodbsc/fem-tool:3",
                    "ghcr.io/inab/k8-openvre-audit-sidecar:1.0",
                    "ghcr.io/inab/x@sha256:" + "a" * 64):
            self.assertTrue(self.allowed(img), img)

    def test_lookalikes_rejected(self):
        for img in ("registry.gitlab.bsc.es.evil.com/impact-cohorte/x:1",   # different host
                    "registry.gitlab.bsc.es/impact-cohorte-evil/x:1",       # namespace prefix, not a sub-path
                    "registry.gitlab.bsc.es/other-group/x:1",
                    "ymaqsoodbsc-evil/x",
                    "evil.io/ymaqsoodbsc/fem-tool",
                    "ghcr.io/inabx/tool:1",
                    "python:3.11",                                         # docker.io/library/python
                    "not a valid ref"):
            self.assertFalse(self.allowed(img), img)

    def test_whole_registry(self):
        s = dict(OPENVRE_STRICT="1", OPENVRE_ALLOWED_REGISTRIES="registry.gitlab.bsc.es")
        self.assertTrue(self.allowed("registry.gitlab.bsc.es/anyone/anything:1", **s))
        self.assertFalse(self.allowed("registry.gitlab.bsc.es.evil.com/x:1", **s))

    def test_whole_docker_hub_refused(self):
        for bad in ("docker.io", "docker.io/*", "index.docker.io"):
            with self.assertRaisesRegex(config.ConfigError, "every public Docker Hub"):
                settings(OPENVRE_ALLOWED_REGISTRIES=bad)
        with self.assertRaisesRegex(config.ConfigError, "every public Docker Hub"):
            settings(OPENVRE_IMAGE_ALLOWLIST="docker.io/*")

    def test_allowlist_entry_forms(self):
        s = dict(OPENVRE_STRICT="1", OPENVRE_IMAGE_ALLOWLIST=
                 "ghcr.io/inab/tool,ghcr.io/inab/pinned:1.0,stata19web:local,python@sha256:" + "b" * 64)
        self.assertTrue(self.allowed("ghcr.io/inab/tool:any-tag", **s))      # repository: any tag
        self.assertTrue(self.allowed("ghcr.io/inab/pinned:1.0", **s))
        self.assertFalse(self.allowed("ghcr.io/inab/pinned:2.0", **s))       # exact tag only
        self.assertFalse(self.allowed("ghcr.io/inab/other:1", **s))
        self.assertTrue(self.allowed("docker.io/library/stata19web:local", **s))
        self.assertTrue(self.allowed("python@sha256:" + "b" * 64, **s))
        self.assertFalse(self.allowed("python:3.11", **s))

    def test_normalization(self):
        self.assertEqual(v.parse_image("python"), ("docker.io", "library/python", "latest", ""))
        self.assertEqual(v.parse_image("ymaqsoodbsc/fem-tool"), ("docker.io", "ymaqsoodbsc/fem-tool", "latest", ""))
        self.assertEqual(v.parse_image("localhost:5000/a/b:1"), ("localhost:5000", "a/b", "1", ""))
        for bad in ("UPPER/case", "a//b", "a:b:c", "x@sha256:short"):
            with self.assertRaises(v.ValidationError, msg=bad):
                v.parse_image(bad)

    def test_invalid_entry_fails_startup(self):
        with self.assertRaisesRegex(config.ConfigError, "image allowlist"):
            settings(OPENVRE_ALLOWED_REGISTRIES="not a registry")

    def test_strict_needs_some_rule(self):
        settings(OPENVRE_STRICT="1", OPENVRE_ALLOWED_REGISTRIES="ghcr.io/inab")
        with self.assertRaises(config.ConfigError):
            settings(OPENVRE_STRICT="1")

    def test_invalid_reference_rejected_even_when_not_strict(self):
        with self.assertRaises(v.ValidationError):
            v.image("Bad Image", settings())


if __name__ == "__main__":
    unittest.main()
