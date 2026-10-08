"""Tiny out-of-process reference for the enterprise adapter protocol.

The records are synthetic metadata, not a connection to a CRM vendor. Replace
the ASSETS lookup with a scoped, read-only vendor metadata API in a real adapter.
"""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound simultaneous clients before an unauthenticated request is parsed."""

    def __init__(self, *args, **kwargs):
        self._slots = BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        request.settimeout(5)
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

ASSETS = {
    "accounts": [
        {
            "name": "CRM Accounts",
            "pointer": "crm/accounts",
            "asset_type": "table",
            "revision": "schema-v1",
            "schema": [
                {"name": "account_id", "data_type": "text", "nullable": False},
                {"name": "account_name", "data_type": "text", "nullable": False},
                {"name": "annual_revenue", "data_type": "decimal", "nullable": True},
            ],
        },
        {
            "name": "CRM Contacts",
            "pointer": "crm/contacts",
            "asset_type": "table",
            "revision": "schema-v1",
            "schema": [
                {"name": "contact_id", "data_type": "text", "nullable": False},
                {"name": "account_id", "data_type": "text", "nullable": False},
                {"name": "email", "data_type": "text", "nullable": True},
            ],
        },
    ],
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        token = os.environ.get("ADAPTER_BEARER_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not token or not hmac.compare_digest(supplied, f"Bearer {token}"):
            self._reply(401, {"error": "unauthorized"})
            return
        if self.path != "/discover":
            self._reply(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096:
                raise ValueError("invalid length")
            request = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "invalid_request"})
            return
        if not isinstance(request, dict):
            self._reply(400, {"error": "invalid_request"})
            return
        scope = request.get("scope")
        if request.get("protocol_version") != "1.0" or scope not in ASSETS:
            self._reply(400, {"error": "unsupported_scope_or_version"})
            return
        assets = ASSETS[scope]
        max_assets = request.get("max_assets")
        max_columns = request.get("max_columns")
        if type(max_assets) is not int or type(max_columns) is not int or max_assets < 1 or max_columns < 1:
            self._reply(400, {"error": "invalid_scan_budget"})
            return
        if len(assets) > max_assets or sum(len(item["schema"]) for item in assets) > max_columns:
            self._reply(413, {"error": "scope_exceeds_scan_budget"})
            return
        self._reply(200, {"protocol_version": "1.0", "assets": assets})

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        pass


if __name__ == "__main__":
    BoundedHTTPServer(("127.0.0.1", 8060), Handler).serve_forever()
