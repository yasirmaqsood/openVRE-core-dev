"""Minimal Kubernetes API client using only the standard library."""

import json
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request


class K8sError(Exception):
    """The Kubernetes API answered with an error status, or could not be reached."""

    def __init__(self, status, message, reason="", raw=""):
        super().__init__(message)
        self.status = status  # 0 means the API could not be reached
        self.message = message
        self.reason = reason
        self.raw = raw


def q(segment):
    """Quote one path segment. Names are validated before this; quoting is a second guard."""
    return urllib.parse.quote(str(segment), safe="")


def _error_message(raw, status):
    try:
        body = json.loads(raw)
        return body.get("message") or f"HTTP {status}", body.get("reason", "")
    except Exception:
        return (raw or f"HTTP {status}")[:300], ""


class KubeClient:
    def __init__(self, settings):
        self.s = settings
        self._token = ""
        self._token_mtime = None
        self._lock = threading.Lock()
        self._ssl = None

    def _ssl_context(self):
        if self._ssl is None:
            self._ssl = ssl.create_default_context(cafile=self.s.ca_path)
        return self._ssl

    def token(self):
        # Re-read when the file changes: Kubernetes rotates projected tokens.
        try:
            mtime = os.stat(self.s.token_path).st_mtime
        except OSError as e:
            raise K8sError(0, f"cannot read service account token: {e}")
        with self._lock:
            if mtime != self._token_mtime:
                with open(self.s.token_path, "r", encoding="utf-8") as f:
                    self._token = f.read().strip()
                self._token_mtime = mtime
            return self._token

    def request(self, method, path, body=None, content_type="application/json", query=None, accept="application/json"):
        """Return (status, parsed JSON or None). Raises K8sError on errors other than 404."""
        url = f"https://{self.s.api_host}:{self.s.api_port}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        if body is not None:
            data = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Authorization", f"Bearer {self.token()}")
        req.add_header("Accept", accept)
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, context=self._ssl_context(), timeout=self.s.k8s_timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
                return resp.getcode(), (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8") if e.fp else ""
            if e.code == 404:
                return 404, None
            msg, reason = _error_message(raw, e.code)
            raise K8sError(e.code, msg, reason, raw)
        except (urllib.error.URLError, OSError) as e:
            raise K8sError(0, f"Kubernetes API unreachable: {e}")

    # Convenience wrappers ------------------------------------------------

    def get(self, path, query=None, accept="application/json"):
        code, obj = self.request("GET", path, query=query, accept=accept)
        return None if code == 404 else obj

    def create(self, path, obj):
        return self.request("POST", path, body=obj)[1]

    def delete(self, path, body=None):
        """Return True if deleted, False if it was already gone."""
        code, _ = self.request("DELETE", path, body=body)
        return code != 404

    def list(self, path, label_selector=None):
        query = {"labelSelector": label_selector} if label_selector else None
        obj = self.get(path, query=query) or {}
        return obj.get("items", []) or []

    def ping(self):
        self.request("GET", "/version")

    def get_text(self, path, query=None):
        """GET a plain-text resource (pod logs). Returns "" if it does not exist."""
        url = f"https://{self.s.api_host}:{self.s.api_port}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self.token()}")
        try:
            with urllib.request.urlopen(req, context=self._ssl_context(), timeout=self.s.k8s_timeout_sec) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            if e.code in (400, 404):
                # 400: container not started yet (e.g. still pulling its image).
                return ""
            msg, reason = _error_message(raw, e.code)
            raise K8sError(e.code, msg, reason, raw)
        except (urllib.error.URLError, OSError) as e:
            raise K8sError(0, f"Kubernetes API unreachable: {e}")
