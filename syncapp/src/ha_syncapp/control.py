"""Admin ingress control surface; workers own state and serialize every action."""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Full, Queue
from typing import Any
from uuid import uuid4

UI = Path(__file__).parent / "ui"
ACTIONS = frozenset({"generate", "refresh", "test", "activate", "initialize", "retry"})


class Control:
    def __init__(self) -> None:
        self.csrf = secrets.token_urlsafe(32)
        self.actions: Queue[dict[str, str]] = Queue(maxsize=8)
        self.lock = threading.Lock()
        self._status: dict[str, Any] = {}

    def publish(self, value: dict[str, Any]) -> None:
        with self.lock:
            self._status = json.loads(json.dumps(value))

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {**self._status, "csrf": self.csrf}


def make_server(
    control: Control, address: tuple[str, int], *, allowed_peer: str = "172.30.32.2"
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format: str, *args: Any) -> None:
            # Request paths and headers are not safe operational log fields.
            return

        def send(self, code: int, data: bytes, content_type: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; object-src 'none'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'self'",
            )
            self.end_headers()
            self.wfile.write(data)

        def allowed(self) -> bool:
            if self.client_address[0] != allowed_peer:
                self.send(403, b'{"error":"ingress_required"}')
                return False
            return True

        def do_GET(self) -> None:
            if not self.allowed():
                return
            if self.path == "/status":
                self.send(200, json.dumps(control.status()).encode())
                return
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
            }
            asset = assets.get(self.path)
            if asset is None:
                self.send(404, b"{}")
                return
            self.send(200, (UI / asset[0]).read_bytes(), asset[1])

        def do_POST(self) -> None:
            if not self.allowed():
                return
            if not secrets.compare_digest(
                self.headers.get("X-SyncApp-CSRF", "").encode(), control.csrf.encode()
            ):
                self.send(403, b'{"error":"csrf_required"}')
                return
            if self.path != "/action":
                self.send(404, b"{}")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096 or self.headers.get_content_type() != "application/json":
                    raise ValueError
                body = json.loads(self.rfile.read(size))
                if (
                    not isinstance(body, dict)
                    or body.keys() - {"action", "job"}
                    or body.get("action") not in ACTIONS
                    or not isinstance(body.get("job", ""), str)
                ):
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                self.send(400, b'{"error":"invalid_action"}')
                return
            action = {"id": str(uuid4()), **body}
            try:
                control.actions.put_nowait(action)
            except Full:
                self.send(429, b'{"error":"actions_busy"}')
                return
            self.send(202, json.dumps({"id": action["id"]}).encode())

    http = ThreadingHTTPServer(address, Handler)
    http.daemon_threads = True
    return http
