"""A local stand-in for the PayPal sandbox endpoints Mandate uses (OAuth, Orders v2 create/get/capture, webhook verification).

It exists so tests and the demo run offline and deterministically.  It is NOT a claim about PayPal's behaviour beyond the documented request/response shapes it
copies; the real integration is exercised by running Mandate against https://api-m.sandbox.paypal.com with your own sandbox app (see README).
The 'payer' approves by visiting the approve link, exactly as a human would on PayPal's page.
"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Emulator:
    def __init__(self, port: int = 0) -> None:
        self.orders: dict[str, dict] = {}
        self.captures = 0
        self.webhook_ok = True
        emu = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silent
                pass

            def _send(self, code: int, body: dict):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("content-length") or 0))

            def do_POST(self):
                path = urlparse(self.path).path
                raw = self._body()
                if path == "/v1/oauth2/token":
                    return self._send(200, {"access_token": "EMU-" + uuid.uuid4().hex, "expires_in": 3000, "token_type": "Bearer"})
                if not (self.headers.get("authorization") or "").startswith("Bearer EMU-"):
                    return self._send(401, {"name": "AUTHENTICATION_FAILURE"})
                if path == "/v2/checkout/orders":
                    b = json.loads(raw)
                    oid = "EMU" + uuid.uuid4().hex[:14].upper()
                    unit = b["purchase_units"][0]
                    ctx = b["payment_source"]["paypal"]["experience_context"]
                    emu.orders[oid] = {"id": oid, "status": "PAYER_ACTION_REQUIRED", "intent": b["intent"], "purchase_units": [unit], "return_url": ctx["return_url"]}
                    base = f"http://127.0.0.1:{emu.port}"
                    return self._send(201, {"id": oid, "status": "PAYER_ACTION_REQUIRED",
                                            "links": [{"href": f"{base}/checkout/approve?token={oid}", "rel": "payer-action", "method": "GET"},
                                                      {"href": f"{base}/v2/checkout/orders/{oid}", "rel": "self", "method": "GET"}]})
                if path.startswith("/v2/checkout/orders/") and path.endswith("/capture"):
                    oid = path.split("/")[4]
                    o = emu.orders.get(oid)
                    if not o:
                        return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
                    if o["status"] == "COMPLETED":
                        return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
                    if o["status"] != "APPROVED":
                        return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
                    o["status"] = "COMPLETED"
                    emu.captures += 1
                    amt = dict(o["purchase_units"][0]["amount"])
                    o["purchase_units"][0]["payments"] = {"captures": [{"id": "CAP" + uuid.uuid4().hex[:12].upper(), "status": "COMPLETED", "amount": amt}]}
                    return self._send(201, o)
                if path == "/v1/notifications/verify-webhook-signature":
                    return self._send(200, {"verification_status": "SUCCESS" if emu.webhook_ok else "FAILURE"})
                return self._send(404, {"name": "NOT_FOUND"})

            def do_PATCH(self):
                path = urlparse(self.path).path
                ops = json.loads(self._body() or b"[]")
                if not (self.headers.get("authorization") or "").startswith("Bearer EMU-"):
                    return self._send(401, {"name": "AUTHENTICATION_FAILURE"})
                o = emu.orders.get(path.split("/")[4]) if path.startswith("/v2/checkout/orders/") else None
                if not o:
                    return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
                if o["status"] == "COMPLETED":
                    return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_COMPLETED"}]})
                for op in ops:                                          # the documented path form for a single purchase unit
                    if op.get("op") == "replace" and str(op.get("path", "")).endswith("/amount"):
                        o["purchase_units"][0]["amount"] = op["value"]
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/checkout/approve":                      # the human clicks "Pay Now" on PayPal's page
                    oid = parse_qs(u.query).get("token", [""])[0]
                    if oid in emu.orders:
                        emu.orders[oid]["status"] = "APPROVED"
                        return self._send(200, {"approved": oid})
                    return self._send(404, {})
                if u.path.startswith("/v2/checkout/orders/"):
                    o = emu.orders.get(u.path.split("/")[4])
                    return self._send(200, o) if o else self._send(404, {"name": "RESOURCE_NOT_FOUND"})
                self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "Emulator":
        self._t.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def human_approves(self, approve_url: str) -> None:
        import urllib.request
        urllib.request.urlopen(approve_url, timeout=10).read()
