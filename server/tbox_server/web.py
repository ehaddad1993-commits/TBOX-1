"""Small threaded HTTP server with routing, JSON helpers, static files and security headers."""
import json
import logging
import mimetypes
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("tbox.http")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


class HttpError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


class Response:
    def __init__(self, body=b"", status=200, content_type="application/json", headers=None):
        self.body, self.status, self.content_type, self.headers = body, status, content_type, headers or {}


class Request:
    def __init__(self, method, path, query, headers, body, ip):
        self.method, self.path, self.headers, self.body, self.ip = method, path, headers, body, ip
        self.query = {k: v[0] for k, v in query.items()}
        self.params = {}
        self.user = None
        self.gateway = None

    def json(self):
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "Request body is not valid JSON")
        return data

    def int_q(self, name, default=None):
        v = self.query.get(name)
        if v in (None, ""):
            return default
        try:
            return int(v)
        except ValueError:
            raise HttpError(400, "Query parameter '%s' must be an integer" % name)

    def float_q(self, name, default=None):
        v = self.query.get(name)
        if v in (None, ""):
            return default
        try:
            return float(v)
        except ValueError:
            raise HttpError(400, "Query parameter '%s' must be a number" % name)


class Router:
    def __init__(self):
        self.routes = []

    def add(self, method, pattern, handler, auth="user", roles=None):
        rx = re.sub(r"<int:(\w+)>", r"(?P<\1>\\d+)", pattern)
        rx = re.sub(r"<str:(\w+)>", r"(?P<\1>[^/]+)", rx)
        self.routes.append((method, re.compile("^" + rx + "$"), handler, auth, roles))

    def route(self, method, pattern, auth="user", roles=None):
        def deco(fn):
            self.add(method, pattern, fn, auth, roles)
            return fn
        return deco

    def match(self, method, path):
        allowed = False
        for m, rx, handler, auth, roles in self.routes:
            mt = rx.match(path)
            if mt:
                if m == method:
                    return handler, auth, roles, {k: int(v) if v.isdigit() else v for k, v in mt.groupdict().items()}
                allowed = True
        if allowed:
            raise HttpError(405, "Method not allowed")
        raise HttpError(404, "Not found")


def json_response(data, status=200, headers=None):
    return Response(json.dumps(data, separators=(",", ":"), default=str).encode(), status, "application/json", headers)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30
        server_version = "TBox"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _client_ip(self):
            if app.cfg.trust_proxy:
                xff = self.headers.get("X-Forwarded-For")
                if xff:
                    return xff.split(",")[0].strip()
            return self.client_address[0]

        def _send(self, resp):
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            if resp.content_type == "application/json" or resp.status >= 400:
                self.send_header("Cache-Control", "no-store")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp.body)

        def _handle(self):
            try:
                parts = urlsplit(self.path)
                path = parts.path
                length = int(self.headers.get("Content-Length") or 0)
                if length > app.cfg.max_body:
                    raise HttpError(413, "Request body too large")
                body = self.rfile.read(length) if length else b""
                if path.startswith("/api/"):
                    req = Request(self.command, path, parse_qs(parts.query), self.headers, body, self._client_ip())
                    resp = app.dispatch(req)
                else:
                    if self.command not in ("GET", "HEAD"):
                        raise HttpError(405, "Method not allowed")
                    resp = serve_static(app.cfg.static_dir, path)
            except HttpError as e:
                payload = {"error": e.message}
                if e.code:
                    payload["code"] = e.code
                resp = json_response(payload, e.status)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                return
            except Exception:
                log.exception("unhandled error for %s %s", self.command, self.path)
                resp = json_response({"error": "Internal server error"}, 500)
            try:
                self._send(resp)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle

    return Handler


def serve_static(root, path):
    root = Path(root).resolve()
    rel = path.lstrip("/") or "index.html"
    target = (root / rel).resolve()
    if root not in target.parents and target != root:
        raise HttpError(404, "Not found")
    if not target.is_file():
        if "." in Path(rel).name:
            raise HttpError(404, "Not found")
        target = root / "index.html"  # single-page-app fallback
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if target.suffix == ".webmanifest":
        ctype = "application/manifest+json"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    return Response(target.read_bytes(), 200, ctype, {"Cache-Control": "no-cache"})


class TBoxHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128
