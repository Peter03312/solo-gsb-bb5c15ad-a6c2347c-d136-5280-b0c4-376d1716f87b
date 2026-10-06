"""HTTP entry point.

Standard-library HTTP server exposing the scheduling audit API:

    GET  /                        service info
    GET  /health                  liveness probe
    POST /api/v1/schedule/audit   audit one observation plan

Configuration is via environment variables (see README.md):
    HOST        bind address          (default 0.0.0.0)
    PORT        listen port           (default 8000)
    LOG_LEVEL   logging level         (default INFO)
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from . import __version__
from .scheduler import run_audit

LOG = logging.getLogger("obs-scheduler-audit")

AUDIT_PATH = "/api/v1/schedule/audit"
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB is far beyond any realistic plan

SERVICE_INFO = {
    "service": "observatory-night-scheduler-audit",
    "version": __version__,
    "endpoints": {
        "health": "GET /health",
        "audit": f"POST {AUDIT_PATH}",
    },
    "docs": "see README.md",
}


class AuditHandler(BaseHTTPRequestHandler):
    server_version = f"ObsSchedulerAudit/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- helpers ------------------------------------------------------------
    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:  # route access logs
        LOG.info("%s - %s", self.address_string(), fmt % args)

    # -- routes ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/":
            self._send_json(200, SERVICE_INFO)
        else:
            self._send_json(404, {"status": "error",
                                  "message": f"not found: {self.path}"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path != AUDIT_PATH:
            self._send_json(404, {"status": "error",
                                  "message": f"not found: {self.path}"})
            return

        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self._send_json(411, {"status": "error",
                                  "message": "missing Content-Length header"})
            return
        try:
            length = int(content_length)
        except ValueError:
            self._send_json(400, {"status": "error",
                                  "message": "invalid Content-Length header"})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"status": "error",
                                  "message": "request body too large"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {
                "status": "rejected",
                "errors": [{
                    "position": "$",
                    "code": "INVALID_JSON",
                    "message": f"request body is not valid JSON: {exc}",
                }],
            })
            return

        status, body = run_audit(payload)
        self._send_json(status, body)


def _self_check() -> None:
    """Startup initialisation: verify the IANA timezone database is usable."""
    for name in ("UTC", "America/Santiago", "Asia/Shanghai"):
        try:
            ZoneInfo(name)
        except Exception as exc:  # pragma: no cover - environment dependent
            LOG.warning("timezone database check failed for %s: %s", name, exc)
            return
    LOG.info("timezone database check passed")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    _self_check()

    server = ThreadingHTTPServer((host, port), AuditHandler)
    LOG.info("observatory night scheduler audit v%s listening on "
             "http://%s:%d (audit endpoint: POST %s)",
             __version__, host, port, AUDIT_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        LOG.info("shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
