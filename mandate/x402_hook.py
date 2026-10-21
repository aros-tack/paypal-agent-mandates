"""A Mandate policy as an x402 `before payment` hook.

The x402 clients expose a hook that runs right before a payment is signed (`onBeforePaymentCreation` in TypeScript, `on_before_payment_creation` in Python), and a
payment provider can pass a user-supplied hook through its config (see coinbase/agentkit issue 1576).  This adapter turns a Mandate (caps, merchants, categories,
velocity) plus the ledger's totals into such a hook.  It is stdlib-only, and it decides with the same pure `evaluate` function the rest of Mandate uses.

    hook = before_payment_hook(mandate, totals=lambda: ledger_totals(), approver=ask_a_human, audit=log_line)
    hook({"selectedRequirements": {"payTo": "0x...", "amount": "250000", "resource": "https://api.seller.example/v1/x"}})
    # -> None (allow) or {"abort": True, "reason": "..."}

The context may use the TypeScript spelling (`selectedRequirements`, `payTo`) or the Python one (`selected_requirements`, `pay_to`).  `amount` is in atomic USDC
(6 decimals) unless `decimals` is given.  The model never decides: a mandate that needs a human approval aborts unless an `approver` callback returns True.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Callable

from .policy import Mandate, Proposal, Totals, evaluate

Hook = Callable[[dict[str, Any]], "dict[str, Any] | None"]


def _pick(d: dict[str, Any], *names: str, default: Any = None) -> Any:
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def before_payment_hook(m: Mandate, *, totals: Callable[[], Totals] = Totals, approver: Callable[[Proposal, Any], bool] | None = None,
                        audit: Callable[[str], None] | None = None, decimals: int = 6, category: str = "x402") -> Hook:
    def hook(context: dict[str, Any]) -> dict[str, Any] | None:
        req = _pick(context, "selectedRequirements", "selected_requirements", default={}) or {}
        pay_to = str(_pick(req, "payTo", "pay_to", default=""))
        resource = str(_pick(req, "resource", default="") or _pick(context.get("paymentRequired") or context.get("payment_required") or {}, "resource", default=""))
        try:
            atomic = Decimal(str(_pick(req, "amount", "maxAmountRequired", "max_amount_required", default="0")))
        except Exception:                                                     # an amount that cannot be read is never allowed
            return _decide(audit, False, "amount could not be read", pay_to, resource, None)
        merchant = resource or pay_to
        if not merchant or atomic <= 0:
            return _decide(audit, False, "no seller or no positive amount in the payment request", pay_to, resource, atomic)
        p = Proposal(merchant, atomic / (Decimal(10) ** decimals), m.currency, str(_pick(context, "category", default=category)),
                     f"x402 payment to {pay_to or merchant}", str(_pick(context, "justification", default="")))
        d = evaluate(m, p, totals())
        if d.verdict == "BLOCKED":
            return _decide(audit, False, "; ".join(d.reasons), pay_to, resource, p.amount)
        if approver is not None and approver(p, d):
            return _decide(audit, True, "within the mandate and approved", pay_to, resource, p.amount)
        return _decide(audit, False, "within the mandate but a human approval is required and was not given", pay_to, resource, p.amount)
    return hook


def _decide(audit: Callable[[str], None] | None, allow: bool, reason: str, pay_to: str, resource: str, amount: Any) -> dict[str, Any] | None:
    if audit:
        audit(json.dumps({"at": round(time.time(), 3), "allow": allow, "reason": reason, "payTo": pay_to, "resource": resource, "amount": str(amount)}, sort_keys=True))
    return None if allow else {"abort": True, "reason": reason}
