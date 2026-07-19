#!/usr/bin/env python3
"""Minimal signed capability server for the 15-minute developer quickstart."""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Allow running from repo root or this folder
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aimarket_hub.signing import Signer  # noqa: E402


_KEY = Path(__file__).resolve().parent / "provider_key"
_SIGNER = Signer(_KEY)
_BIND = os.environ.get("CAPABILITY_BIND", "127.0.0.1").strip() or "127.0.0.1"
_PORT = int(os.environ.get("CAPABILITY_PORT", "3456"))


def public_invoke_url() -> str:
    """URL for manifest / supply register (may differ from bind address)."""
    host = os.environ.get("CAPABILITY_PUBLIC_HOST", _BIND).strip() or _BIND
    return f"http://{host}:{_PORT}/invoke"


def _canonical_result(result: dict) -> str:
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[hello-capability] {self.address_string()} - {fmt % args}")

    def do_POST(self) -> None:
        if self.path != "/invoke":
            self.send_error(404, "POST /invoke only")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        inp = body.get("input") or {}
        name = str(inp.get("name") or "world")
        result = {"greeting": f"Hello, {name}!", "from": "hello-capability"}
        sig = _SIGNER.sign_canonical(_canonical_result(result))
        payload = json.dumps({"success": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Provider-Signature", sig)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    print(f"hello-capability listening on http://{_BIND}:{_PORT}/invoke")
    print(f"invoke_url for publish: {public_invoke_url()}")
    print(f"provider_pubkey (put in capability.json): {_SIGNER.public_key_b64}")
    if _BIND in ("127.0.0.1", "localhost") and not os.environ.get("CAPABILITY_PUBLIC_HOST"):
        print("Hub in Docker: set CAPABILITY_PUBLIC_HOST=127.0.0.1 and AIMARKET_INVOKE_HOST_GATEWAY=host.docker.internal on hub")
    HTTPServer((_BIND, _PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
