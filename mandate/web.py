"""The human side: a small dashboard (pending approvals with PayPal links, ledger, audit-chain health), the PayPal return page and the webhook endpoint."""
from __future__ import annotations

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import MandateService

CSS = ("body{font:15px/1.5 -apple-system,system-ui,sans-serif;margin:0;background:#0b1020;color:#e8ecf6}main{max-width:960px;margin:0 auto;padding:24px 16px}"
       "h1{font-size:22px;margin:0 0 4px}.sub{color:#9aa6c4;margin-bottom:20px}.card{background:#141b33;border:1px solid #243055;border-radius:12px;padding:14px 16px;margin:12px 0}"
       ".row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.pill{border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600}"
       ".PENDING_APPROVAL{background:#3b3210;color:#ffd666}.CAPTURED{background:#0f3a2a;color:#6ee7a8}.BLOCKED{background:#3d1520;color:#ff8ea1}.CANCELLED,.EXPIRED,.FAILED,.APPROVED{background:#1f2745;color:#b6c2e6}"
       "a.btn{display:inline-block;background:#2f6bff;color:#fff;text-decoration:none;border-radius:8px;padding:6px 12px;font-weight:600}.muted{color:#9aa6c4;font-size:13px}code{background:#0b1020;padding:1px 6px;border-radius:6px}")


def page(svc: MandateService) -> str:
    m = svc.mandate
    ok, chain = svc.ledger.verify_chain()
    rows = svc.ledger.list()
    cards = []
    for r in rows[:40]:
        extra = ""
        if r["status"] == "PENDING_APPROVAL":
            extra = f'<p><a class="btn" href="{html.escape(r["approve_url"] or "#")}" target="_blank" rel="noopener">Review and approve on PayPal</a></p>'
        brief = (f'<p>{html.escape(r["brief"])}</p><div class="muted">AI review ({html.escape(r.get("ai_mode") or "")}) · risk <b>{html.escape(r.get("risk") or "?")}</b>'
                 + (f' · fit with the task {float(r["goal_fit"]):.0%}' if r.get("goal_fit") is not None else "") + "</div>") if r.get("brief") else ""
        if r.get("warnings") and r["status"] == "PENDING_APPROVAL":
            extra = f'<p class="muted">⚠ For your attention: {html.escape(r["warnings"])}</p>' + extra
        if r["status"] == "BLOCKED":
            extra = f'<p class="muted">Blocked before PayPal: {html.escape(r["reasons"] or "")}</p>'
        cards.append(f'<div class="card"><div class="row"><b>{html.escape(r["currency"])} {html.escape(r["amount"])} · {html.escape(r["merchant"])}</b>'
                     f'<span class="pill {html.escape(r["status"])}">{html.escape(r["status"])}</span></div><div>{html.escape(r["description"])}</div>'
                     f'<div class="muted">Agent said: “{html.escape(r["justification"] or "")}” · {html.escape(r["id"])}</div>{brief}{extra}</div>')
    t = svc.ledger.totals(m.agent_id)
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Mandate</title><style>{CSS}</style><main>"
            f"<h1>Mandate — spending authority for <code>{html.escape(m.agent_id)}</code></h1><div class=sub>An AI agent proposes. PayPal approval is yours.</div>"
            f'<div class="card"><div class="row"><span>Per purchase ≤ <b>{m.per_purchase_cap}</b> · Daily ≤ <b>{m.daily_cap}</b> · Monthly ≤ <b>{m.monthly_cap}</b> {m.currency}</span>'
            f'<span>Spent/committed today <b>{t.spent_today}</b> · this month <b>{t.spent_month}</b> · waiting <b>{t.open_orders}</b></span></div>'
            f'<div class="muted">Audit chain: {"✔" if ok else "✖ TAMPERED"} {html.escape(chain)}</div></div>'
            + ("".join(cards) or '<div class="card muted">No purchases yet.</div>') + "</main>")


def make_server(svc: MandateService, host: str = "127.0.0.1", port: int = 8808) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8"):
            raw = body.encode()
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, page(svc))
            if u.path == "/api/purchases":
                return self._send(200, json.dumps(svc.ledger.list(), default=str), "application/json")
            if u.path == "/paypal/return":
                pid = parse_qs(u.query).get("purchase", [""])[0]
                res = svc.capture(pid)                       # the human just approved on PayPal: finish the payment (still verified against PayPal)
                return self._send(200, f"<!doctype html><meta charset=utf-8><style>{CSS}</style><main><h1>Payment {html.escape(res.get('status', ''))}</h1>"
                                       f"<p class=muted>{html.escape(', '.join(res.get('reasons', [])))}</p><p><a class=btn href='/'>Back to the mandate</a></p></main>")
            if u.path == "/paypal/cancel":
                pid = parse_qs(u.query).get("purchase", [""])[0]
                svc.cancel(pid)
                return self._send(200, f"<!doctype html><meta charset=utf-8><style>{CSS}</style><main><h1>Cancelled</h1><p><a class=btn href='/'>Back</a></p></main>")
            self._send(404, "not found", "text/plain")

        def do_POST(self):
            if urlparse(self.path).path != "/paypal/webhook":
                return self._send(404, "not found", "text/plain")
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            try:
                event = json.loads(raw)
            except ValueError:
                return self._send(400, "{}", "application/json")
            res = svc.on_webhook(headers=dict(self.headers.items()), event=event, webhook_id=os.environ.get("PAYPAL_WEBHOOK_ID", ""))
            self._send(200 if res.get("accepted") else 400, json.dumps(res), "application/json")

    return ThreadingHTTPServer((host, port), H)
