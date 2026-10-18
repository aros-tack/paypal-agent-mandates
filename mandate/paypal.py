"""A small PayPal REST client (Orders v2 + webhook verification) using only the standard library.

Mode is chosen by PAYPAL_API_BASE: https://api-m.sandbox.paypal.com (default) is PayPal's free sandbox; the local emulator can be pointed at for offline use.
PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are read from the environment at call time and never logged or stored.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

SANDBOX = "https://api-m.sandbox.paypal.com"


class PayPalError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"PayPal answered {status}: {str(body)[:300]}")
        self.status, self.body = status, body

    @property
    def issues(self) -> list[str]:
        return [str(d.get("issue")) for d in ((self.body or {}).get("details") or []) if isinstance(self.body, dict)]


class PayPal:
    def __init__(self, base: str | None = None, client_id: str | None = None, secret: str | None = None) -> None:
        self.base = (base or os.environ.get("PAYPAL_API_BASE") or SANDBOX).rstrip("/")
        if self.base in ("https://api-m.paypal.com", "https://api.paypal.com") and os.environ.get("MANDATE_ALLOW_LIVE_MONEY") != "1":
            raise PayPalError(0, "refusing to talk to PayPal's LIVE API: Mandate is a sandbox project; real money needs MANDATE_ALLOW_LIVE_MONEY=1 and a deliberate decision")
        self._id, self._secret = client_id, secret
        self._token: tuple[str, float] | None = None

    def _creds(self) -> tuple[str, str]:
        cid = self._id or os.environ.get("PAYPAL_CLIENT_ID", "")
        sec = self._secret or os.environ.get("PAYPAL_CLIENT_SECRET", "")
        if not cid or not sec:
            raise PayPalError(0, "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set (a free PayPal sandbox app provides them)")
        return cid, sec

    def _call(self, method: str, path: str, body: dict | None = None, *, headers: dict | None = None, auth: str = "bearer") -> Any:
        h = {"content-type": "application/json", **(headers or {})}
        if auth == "bearer":
            h["authorization"] = "Bearer " + self.token()
        req = urllib.request.Request(self.base + path, None if body is None else json.dumps(body).encode(), h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode())
            except ValueError:
                data = None
            raise PayPalError(exc.code, data) from exc
        except OSError as exc:
            raise PayPalError(0, f"network: {exc}") from exc

    def token(self) -> str:
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        cid, sec = self._creds()
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        req = urllib.request.Request(self.base + "/v1/oauth2/token", b"grant_type=client_credentials",
                                     {"authorization": "Basic " + basic, "content-type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise PayPalError(exc.code, "token request refused") from exc
        self._token = (d["access_token"], time.time() + int(d.get("expires_in", 300)))
        return self._token[0]

    def create_order(self, *, amount: str, currency: str, description: str, reference_id: str, return_url: str, cancel_url: str, merchant_name: str = "") -> dict[str, Any]:
        body = {"intent": "CAPTURE",
                "purchase_units": [{"reference_id": reference_id, "description": description[:127], "custom_id": reference_id,
                                    "amount": {"currency_code": currency, "value": amount}}],
                "payment_source": {"paypal": {"experience_context": {"user_action": "PAY_NOW", "return_url": return_url, "cancel_url": cancel_url,
                                                                      "brand_name": (merchant_name or "Mandate")[:127], "shipping_preference": "NO_SHIPPING"}}}}
        return self._call("POST", "/v2/checkout/orders", body, headers={"PayPal-Request-Id": reference_id})

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._call("GET", f"/v2/checkout/orders/{order_id}")

    def patch_order(self, order_id: str, operations: list[dict[str, Any]]) -> None:
        """PayPal allows a merchant-side PATCH of a CREATED or APPROVED order. Mandate never calls this; the tests use it to prove an altered order is refused at capture."""
        self._call("PATCH", f"/v2/checkout/orders/{order_id}", operations)

    def capture_order(self, order_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        return self._call("POST", f"/v2/checkout/orders/{order_id}/capture", {}, headers={"PayPal-Request-Id": request_id or str(uuid.uuid4())})

    def verify_webhook(self, *, webhook_id: str, headers: dict[str, str], event: dict[str, Any]) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = {"auth_algo": h.get("paypal-auth-algo", ""), "cert_url": h.get("paypal-cert-url", ""), "transmission_id": h.get("paypal-transmission-id", ""),
                "transmission_sig": h.get("paypal-transmission-sig", ""), "transmission_time": h.get("paypal-transmission-time", ""), "webhook_id": webhook_id, "webhook_event": event}
        return self._call("POST", "/v1/notifications/verify-webhook-signature", body).get("verification_status") == "SUCCESS"
