import unittest

from helpers import php_job_manifest, settings

from openvre_scheduler import policy


class JobPolicyTests(unittest.TestCase):
    def setUp(self):
        self.s = settings()

    def check(self, manifest, s=None):
        return policy.check_job(manifest, "openvre", s or self.s)

    def assertRejected(self, manifest, fragment):
        job, errs = self.check(manifest)
        self.assertIsNone(job)
        self.assertTrue(any(fragment in e for e in errs), errs)

    def test_frontend_manifest_passes_and_is_hardened(self):
        job, errs = self.check(php_job_manifest())
        self.assertEqual(errs, [])
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        c = pod["containers"][0]["securityContext"]
        self.assertEqual(c["capabilities"], {"drop": ["ALL"]})
        self.assertFalse(c["allowPrivilegeEscalation"])
        self.assertEqual(job["spec"]["template"]["metadata"]["labels"]["openvre.io/batch"], "true")

    def test_input_not_modified(self):
        m = php_job_manifest()
        self.check(m)
        self.assertNotIn("seccompProfile", m["spec"]["template"]["spec"]["securityContext"])

    def test_batch_label_forced_even_if_missing(self):
        m = php_job_manifest()
        del m["spec"]["template"]["metadata"]
        job, errs = self.check(m)
        self.assertEqual(errs, [])
        self.assertEqual(job["spec"]["template"]["metadata"]["labels"]["openvre.io/batch"], "true")

    def test_secret_volume_rejected(self):
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["volumes"].append({"name": "s", "secret": {"secretName": "scheduler-auth"}})
        self.assertRejected(m, "only persistentVolumeClaim and emptyDir")

    def test_projected_token_rejected(self):
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["volumes"].append(
            {"name": "t", "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}})
        self.assertRejected(m, "only persistentVolumeClaim and emptyDir")

    def test_host_path_rejected(self):
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["volumes"].append({"name": "h", "hostPath": {"path": "/"}})
        self.assertRejected(m, "only persistentVolumeClaim")

    def test_other_pvc_rejected(self):
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "admin-pvc"
        self.assertRejected(m, "is not allowed")

    def test_service_account_rejected(self):
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["serviceAccountName"] = "scheduler"
        self.assertRejected(m, "serviceAccountName")

    def test_host_network_rejected(self):
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["hostNetwork"] = True
        self.assertRejected(m, "hostNetwork")

    def test_privileged_and_caps_rejected(self):
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True, "capabilities": {"add": ["SYS_ADMIN"]}}
        job, errs = self.check(m)
        self.assertIsNone(job)
        self.assertTrue(any("privileged" in e for e in errs) and any("capabilities" in e for e in errs))

    def test_root_rejected(self):
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["securityContext"]["runAsUser"] = 0
        self.assertRejected(m, "non-root")

    def test_secret_env_rejected(self):
        m = php_job_manifest()
        c = m["spec"]["template"]["spec"]["containers"][0]
        c["env"].append({"name": "X", "valueFrom": {"secretKeyRef": {"name": "scheduler-auth", "key": "token"}}})
        self.assertRejected(m, "must not read Secrets")
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [{"secretRef": {"name": "x"}}]
        self.assertRejected(m, "envFrom")

    def test_name_and_namespace(self):
        self.assertRejected(php_job_manifest(name="scheduler"), "must start with")
        self.assertRejected(php_job_manifest(ns="kube-system"), "metadata.namespace")

    def test_kind(self):
        m = php_job_manifest()
        m["kind"] = "Deployment"
        self.assertRejected(m, "kind Job")

    def test_deadline_required(self):
        m = php_job_manifest()
        del m["spec"]["activeDeadlineSeconds"]
        self.assertRejected(m, "activeDeadlineSeconds")

    def test_strict_image(self):
        s = settings(OPENVRE_STRICT="1", OPENVRE_IMAGE_ALLOWLIST="ghcr.io/inab/*")
        job, errs = self.check(php_job_manifest(image="evil/miner"), s)
        self.assertIsNone(job)
        self.assertTrue(any("ALLOWLIST" in e for e in errs))
        job, errs = self.check(php_job_manifest(), s)
        self.assertEqual(errs, [])

    def test_pull_secrets_added_and_checked(self):
        s = settings(OPENVRE_IMAGE_PULL_SECRETS="gitlab-registry")
        job, errs = self.check(php_job_manifest(), s)
        self.assertEqual(job["spec"]["template"]["spec"]["imagePullSecrets"], [{"name": "gitlab-registry"}])
        m = php_job_manifest()
        m["spec"]["template"]["spec"]["imagePullSecrets"] = [{"name": "keycloak-secrets"}]
        job, errs = self.check(m, s)
        self.assertIsNone(job)


if __name__ == "__main__":
    unittest.main()
