"""Outbound-only HTTPS client for the TBox cloud."""
import json
import ssl
import urllib.error
import urllib.request


class CloudError(Exception):
    def __init__(self, message, kind="network", status=None):
        super().__init__(message)
        self.kind, self.status = kind, status


class CloudClient:
    def __init__(self, cfg):
        self.base = cfg.server_url.rstrip("/")
        self.token = cfg.token
        self.timeout = cfg.request_timeout_s
        self.ctx = None
        if self.base.startswith("https://"):
            self.ctx = ssl.create_default_context(cafile=cfg.ca_file or None)
            if not cfg.verify_tls:
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE

    def _call(self, method, path, body=None):
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json", "User-Agent": "TBox-Edge-Gateway"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            kind = "auth" if e.code in (401, 403) else ("server" if e.code >= 500 else "client")
            raise CloudError("HTTP %d from server" % e.code, kind, e.code)
        except (urllib.error.URLError, OSError, ssl.SSLError, ValueError) as e:
            raise CloudError("cannot reach server (%s)" % getattr(e, "reason", e), "network")

    def get_config(self, version):
        return self._call("GET", "/api/gateway/config?version=%d" % version)

    def ingest(self, payload):
        return self._call("POST", "/api/gateway/ingest", payload)
