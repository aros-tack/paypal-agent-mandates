"""The mandate service: propose -> decide -> (PayPal order) -> HUMAN approves on PayPal -> verified capture -> reconcile.

An agent can only ask.  It can never approve (that is the human on PayPal's approval page) and it can capture only an order PayPal itself reports as
APPROVED, for exactly the amount and currency the ledger recorded, while the mandate is still valid.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Callable

from . import ai as ai_mod
from . import critic as critic_mod
from .ledger import Ledger
from .paypal import PayPal, PayPalError
from .policy import Mandate, MandateError, Proposal, evaluate, money


class MandateService:
    def __init__(self, mandate: Mandate, ledger: Ledger, paypal: PayPal, *, base_url: str = "http://127.0.0.1:8808",
                 assess: Callable[..., ai_mod.Assessment] = ai_mod.assess, clock: Callable[[], float] | None = None) -> None:
        self.mandate, self.ledger, self.paypal, self.base_url, self._assess = mandate, ledger, paypal, base_url.rstrip("/"), assess
        import time
        self.clock = clock or time.time

    # ------------------------------------------------------------------ propose
    def propose(self, *, merchant: str, amount: Any, category: str, description: str, justification: str = "", currency: str | None = None, task: str = "") -> dict[str, Any]:
        m = self.mandate
        cur = (currency or m.currency).upper()
        try:
            amt = money(amount, cur)
        except MandateError as exc:
            self.ledger.audit("proposal_rejected_malformed", agent=m.agent_id, error=str(exc))
            return {"status": "REJECTED", "reasons": [str(exc)]}
        p = Proposal(merchant=merchant.strip(), amount=amt, currency=cur, category=category.strip(), description=description.strip(), justification=justification.strip())
        a = self._assess(task=task, merchant=p.merchant, amount=str(amt), currency=cur, description=p.description, justification=p.justification)
        hard, soft = a.hard, a.soft
        decision = evaluate(m, p, self.ledger.totals(m.agent_id, now=self.clock()), now=self.clock(), critic_flags=tuple(hard))
        pid = "pur_" + uuid.uuid4().hex[:12]
        row = {"id": pid, "mandate_id": m.mandate_id, "agent_id": m.agent_id, "merchant": p.merchant, "category": p.category, "description": p.description,
               "justification": p.justification, "amount": str(amt), "currency": cur, "decision": decision.verdict, "reasons": "; ".join(decision.reasons), "warnings": "; ".join(soft), "brief": a.brief, "risk": a.risk, "goal_fit": a.goal_fit, "ai_mode": a.mode, "task": task[:300]}
        if not decision.allowed:
            self.ledger.add({**row, "status": "BLOCKED"})
            self.ledger.audit("proposal_blocked", purchase=pid, amount=str(amt), currency=cur, merchant=p.merchant, reasons=decision.reasons, rules=decision.rules_checked)
            return {"purchase_id": pid, "status": "BLOCKED", "reasons": decision.reasons, "risk": a.risk, "ai_mode": a.mode}
        try:
            order = self.paypal.create_order(amount=str(amt), currency=cur, description=p.description, reference_id=pid, merchant_name=p.merchant,
                                             return_url=f"{self.base_url}/paypal/return?purchase={pid}", cancel_url=f"{self.base_url}/paypal/cancel?purchase={pid}")
        except PayPalError as exc:
            self.ledger.add({**row, "status": "FAILED"})
            self.ledger.audit("order_create_failed", purchase=pid, error=str(exc))
            return {"purchase_id": pid, "status": "FAILED", "reasons": [str(exc)]}
        approve = next((l["href"] for l in order.get("links", []) if l.get("rel") in ("payer-action", "approve")), "")
        self.ledger.add({**row, "status": "PENDING_APPROVAL", "paypal_order_id": order["id"], "approve_url": approve})
        self.ledger.audit("order_created", purchase=pid, order=order["id"], amount=str(amt), currency=cur, merchant=p.merchant, rules=decision.rules_checked)
        return {"purchase_id": pid, "status": "PENDING_APPROVAL", "paypal_order_id": order["id"], "approve_url": approve,
                "warnings": soft, "risk": a.risk, "goal_fit": a.goal_fit, "brief": a.brief, "ai_mode": a.mode, "message": "A human must approve this on PayPal; the agent cannot capture it before that."}

    # ------------------------------------------------------------------ capture
    def capture(self, purchase_id: str) -> dict[str, Any]:
        row = self.ledger.get(purchase_id)
        if not row:
            return {"status": "UNKNOWN", "reasons": ["no such purchase"]}
        if row["status"] == "CAPTURED":
            return {"purchase_id": purchase_id, "status": "CAPTURED", "capture_id": row["capture_id"], "message": "already captured"}
        if row["status"] not in ("PENDING_APPROVAL", "APPROVED"):
            return {"purchase_id": purchase_id, "status": row["status"], "reasons": ["this purchase cannot be captured in its current state"]}
        m = self.mandate
        if m.expires_at and self.clock() >= m.expires_at:
            self.ledger.update(purchase_id, status="EXPIRED")
            self.ledger.audit("capture_refused", purchase=purchase_id, reason="mandate expired")
            return {"purchase_id": purchase_id, "status": "EXPIRED", "reasons": ["the mandate has expired"]}
        if m.approval_window and self.clock() - float(row["created"]) > m.approval_window:
            self.ledger.update(purchase_id, status="EXPIRED")
            self.ledger.audit("capture_refused", purchase=purchase_id, reason="approval window elapsed", window_s=m.approval_window)
            return {"purchase_id": purchase_id, "status": "EXPIRED", "reasons": [f"the approval window of {m.approval_window}s has elapsed"]}
        try:
            order = self.paypal.get_order(row["paypal_order_id"])
        except PayPalError as exc:
            if exc.status == 404:
                self.ledger.update(purchase_id, status="EXPIRED")
                self.ledger.audit("capture_refused", purchase=purchase_id, reason="PayPal no longer has the order")
                return {"purchase_id": purchase_id, "status": "EXPIRED", "reasons": ["PayPal no longer has this order"]}
            return {"purchase_id": purchase_id, "status": row["status"], "reasons": [str(exc)]}
        if order.get("status") == "VOIDED":
            self.ledger.update(purchase_id, status="EXPIRED")
            self.ledger.audit("capture_refused", purchase=purchase_id, reason="PayPal voided the order")
            return {"purchase_id": purchase_id, "status": "EXPIRED", "reasons": ["PayPal voided this order"]}
        unit = (order.get("purchase_units") or [{}])[0]
        amt = unit.get("amount", {})
        if order.get("status") == "COMPLETED":                              # PayPal already captured it (e.g. we crashed before recording): record, never charge twice
            cap = (unit.get("payments", {}).get("captures") or [{}])[0]
            self.ledger.update(purchase_id, status="CAPTURED", capture_id=cap.get("id", ""))
            self.ledger.audit("capture_replay_recognised", purchase=purchase_id, capture=cap.get("id"))
            return {"purchase_id": purchase_id, "status": "CAPTURED", "capture_id": cap.get("id"), "message": "PayPal had already captured this order"}
        if order.get("status") != "APPROVED":
            return {"purchase_id": purchase_id, "status": "PENDING_APPROVAL", "reasons": [f"PayPal reports the order as {order.get('status')}: the human has not approved it"]}
        if Decimal(str(amt.get("value", "-1"))) != Decimal(row["amount"]) or amt.get("currency_code") != row["currency"]:
            self.ledger.update(purchase_id, status="BLOCKED")
            self.ledger.audit("capture_refused", purchase=purchase_id, reason="amount or currency on PayPal differs from the ledger", paypal=amt, ledger=[row["amount"], row["currency"]])
            return {"purchase_id": purchase_id, "status": "BLOCKED", "reasons": ["the order on PayPal does not match what the mandate approved"]}
        self.ledger.update(purchase_id, status="APPROVED")
        try:
            done = self.paypal.capture_order(row["paypal_order_id"], request_id=purchase_id)
        except PayPalError as exc:
            if "ORDER_ALREADY_CAPTURED" in exc.issues:                      # replay: PayPal already completed it; record that, never a second charge
                done = self.paypal.get_order(row["paypal_order_id"])
                cap = ((done.get("purchase_units") or [{}])[0].get("payments", {}).get("captures") or [{}])[0]
                self.ledger.update(purchase_id, status="CAPTURED", capture_id=cap.get("id", ""))
                self.ledger.audit("capture_replay_recognised", purchase=purchase_id, capture=cap.get("id"))
                return {"purchase_id": purchase_id, "status": "CAPTURED", "capture_id": cap.get("id"), "message": "PayPal had already captured this order"}
            self.ledger.audit("capture_failed", purchase=purchase_id, error=str(exc))
            return {"purchase_id": purchase_id, "status": "APPROVED", "reasons": [str(exc)]}
        cap = ((done.get("purchase_units") or [{}])[0].get("payments", {}).get("captures") or [{}])[0]
        if done.get("status") == "COMPLETED" and cap.get("status") in ("COMPLETED", "PENDING"):
            self.ledger.update(purchase_id, status="CAPTURED", capture_id=cap.get("id", ""))
            self.ledger.audit("captured", purchase=purchase_id, order=row["paypal_order_id"], capture=cap.get("id"), amount=row["amount"], currency=row["currency"])
            return {"purchase_id": purchase_id, "status": "CAPTURED", "capture_id": cap.get("id")}
        self.ledger.audit("capture_unexpected", purchase=purchase_id, paypal_status=done.get("status"))
        return {"purchase_id": purchase_id, "status": "APPROVED", "reasons": [f"PayPal answered {done.get('status')}"]}

    # ------------------------------------------------------------------ the rest
    def sweep_expired(self) -> list[str]:
        """Lapse every pending purchase whose approval window has elapsed (run periodically; capture also enforces it)."""
        out = []
        for r in self.ledger.list("PENDING_APPROVAL"):
            if self.mandate.approval_window and self.clock() - float(r["created"]) > self.mandate.approval_window:
                self.ledger.update(r["id"], status="EXPIRED")
                self.ledger.audit("expired", purchase=r["id"], window_s=self.mandate.approval_window)
                out.append(r["id"])
        return out

    def status(self, purchase_id: str) -> dict[str, Any]:
        row = self.ledger.get(purchase_id)
        return {k: row[k] for k in ("id", "status", "amount", "currency", "merchant", "description", "paypal_order_id", "approve_url", "capture_id", "reasons", "warnings", "brief", "risk")} if row else {"status": "UNKNOWN"}

    def cancel(self, purchase_id: str) -> dict[str, Any]:
        row = self.ledger.get(purchase_id)
        if not row or row["status"] != "PENDING_APPROVAL":
            return {"status": row["status"] if row else "UNKNOWN", "reasons": ["only a pending purchase can be cancelled"]}
        self.ledger.update(purchase_id, status="CANCELLED")
        self.ledger.audit("cancelled", purchase=purchase_id)
        return {"purchase_id": purchase_id, "status": "CANCELLED"}

    def on_webhook(self, *, headers: dict[str, str], event: dict[str, Any], webhook_id: str) -> dict[str, Any]:
        """PayPal tells us what happened; the ledger follows only a signature-verified event."""
        try:
            ok = self.paypal.verify_webhook(webhook_id=webhook_id, headers=headers, event=event)
        except PayPalError as exc:
            self.ledger.audit("webhook_unverifiable", error=str(exc))
            return {"accepted": False, "reason": "signature could not be verified"}
        if not ok:
            self.ledger.audit("webhook_rejected", event_id=event.get("id"))
            return {"accepted": False, "reason": "signature invalid"}
        if event.get("id") and not self.ledger.first_time(str(event["id"])):
            self.ledger.audit("webhook_replay_ignored", event_id=event.get("id"))
            return {"accepted": True, "replay": True}
        res = event.get("resource", {})
        order_id = (res.get("supplementary_data", {}).get("related_ids", {}) or {}).get("order_id") or res.get("id")
        row = self.ledger.by_order(order_id) if order_id else None
        self.ledger.audit("webhook", event_id=event.get("id"), type=event.get("event_type"), purchase=row and row["id"])
        if row and event.get("event_type") == "PAYMENT.CAPTURE.COMPLETED" and row["status"] != "CAPTURED":
            self.ledger.update(row["id"], status="CAPTURED", capture_id=res.get("id", ""))
        if row and event.get("event_type") == "CHECKOUT.ORDER.APPROVED" and row["status"] == "PENDING_APPROVAL":
            self.ledger.update(row["id"], status="APPROVED")
        return {"accepted": True, "purchase": row and row["id"]}
