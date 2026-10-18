"""Mandates: the owner's machine-readable spending authority for an AI agent.

A mandate says what an agent may ask PayPal to charge, how much, where, and when a human must approve.  The engine is pure (no I/O): the same
request and the same ledger totals always give the same decision, which is what makes an agent's spending auditable and testable.

Decisions
  BLOCKED        the request breaks a hard rule; no PayPal order is ever created
  NEEDS_APPROVAL a PayPal order is created and the HUMAN approves it on PayPal's own approval page; the agent can never capture it by itself
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

CURRENCY_EXP = {"USD": 2, "EUR": 2, "GBP": 2, "ZAR": 2, "JPY": 0}


class MandateError(ValueError):
    pass


def host(value: str) -> str:
    """'https://Shop.Example/gift-cards' -> 'shop.example': merchant rules are about hosts."""
    v = str(value).strip().lower()
    v = v.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    return v


def money(value: Any, currency: str = "USD") -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MandateError(f"not an amount: {value!r}") from exc
    exp = CURRENCY_EXP.get(currency, 2)
    if d <= 0 or d != d.quantize(Decimal(1).scaleb(-exp)):
        raise MandateError(f"{value!r} is not a positive {currency} amount with at most {exp} decimals")
    return d


@dataclass(frozen=True)
class Mandate:
    mandate_id: str
    agent_id: str
    currency: str = "USD"
    per_purchase_cap: Decimal = Decimal("100.00")
    daily_cap: Decimal = Decimal("200.00")
    monthly_cap: Decimal = Decimal("500.00")
    allowed_merchants: tuple[str, ...] = ()          # empty = any merchant not denied
    denied_merchants: tuple[str, ...] = ()
    allowed_categories: tuple[str, ...] = ()
    max_open_orders: int = 3                           # unapproved orders an agent may leave hanging
    velocity_per_hour: int = 5                         # proposals per hour, approved or not
    expires_at: float = 0.0                            # 0 = never
    approval_window: int = 3600                        # seconds a human has to approve before the request lapses
    notes: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Mandate":
        cur = str(d.get("currency", "USD")).upper()
        try:
            return Mandate(
                mandate_id=str(d["mandate_id"]), agent_id=str(d["agent_id"]), currency=cur,
                per_purchase_cap=money(d.get("per_purchase_cap", "100.00"), cur), daily_cap=money(d.get("daily_cap", "200.00"), cur),
                monthly_cap=money(d.get("monthly_cap", "500.00"), cur),
                allowed_merchants=tuple(host(x) for x in d.get("allowed_merchants", ())),
                denied_merchants=tuple(host(x) for x in d.get("denied_merchants", ())),
                allowed_categories=tuple(str(x).lower() for x in d.get("allowed_categories", ())),
                max_open_orders=int(d.get("max_open_orders", 3)), velocity_per_hour=int(d.get("velocity_per_hour", 5)),
                expires_at=float(d.get("expires_at", 0.0)), approval_window=int(d.get("approval_window", 3600)), notes=str(d.get("notes", "")))
        except KeyError as exc:
            raise MandateError(f"mandate is missing {exc.args[0]}") from exc

    @staticmethod
    def load(path: str) -> "Mandate":
        with open(path, encoding="utf-8") as fh:
            return Mandate.from_dict(json.load(fh))

    def to_dict(self) -> dict[str, Any]:
        return {"mandate_id": self.mandate_id, "agent_id": self.agent_id, "currency": self.currency, "per_purchase_cap": str(self.per_purchase_cap),
                "daily_cap": str(self.daily_cap), "monthly_cap": str(self.monthly_cap), "allowed_merchants": list(self.allowed_merchants),
                "denied_merchants": list(self.denied_merchants), "allowed_categories": list(self.allowed_categories), "max_open_orders": self.max_open_orders,
                "velocity_per_hour": self.velocity_per_hour, "expires_at": self.expires_at, "approval_window": self.approval_window, "notes": self.notes}


@dataclass(frozen=True)
class Proposal:
    merchant: str
    amount: Decimal
    currency: str
    category: str
    description: str
    justification: str = ""


@dataclass
class Totals:
    """What the agent has already spent or committed, from the ledger (never from the agent)."""
    spent_today: Decimal = Decimal("0")
    spent_month: Decimal = Decimal("0")
    open_orders: int = 0
    proposals_last_hour: int = 0


@dataclass
class Decision:
    verdict: str                                    # BLOCKED | NEEDS_APPROVAL
    reasons: list[str] = field(default_factory=list)
    rules_checked: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.verdict != "BLOCKED"


def evaluate(m: Mandate, p: Proposal, totals: Totals, *, now: float | None = None, critic_flags: tuple[str, ...] = ()) -> Decision:
    now = time.time() if now is None else now
    why: list[str] = []
    checked: list[str] = []

    def rule(name: str, ok: bool, reason: str) -> None:
        checked.append(name)
        if not ok:
            why.append(reason)

    rule("mandate_active", not (m.expires_at and now >= m.expires_at), "the mandate has expired")
    rule("currency", p.currency == m.currency, f"the mandate is in {m.currency}, the request is in {p.currency}")
    h = host(p.merchant)
    rule("merchant_not_denied", not any(h == d or h.endswith("." + d) for d in m.denied_merchants), f"{p.merchant} is on the denied list")
    rule("merchant_allowed", not m.allowed_merchants or any(h == a or h.endswith("." + a) for a in m.allowed_merchants), f"{p.merchant} is not on the allowed list")
    rule("category_allowed", not m.allowed_categories or p.category.lower() in m.allowed_categories, f"category '{p.category}' is not allowed")
    rule("per_purchase_cap", p.amount <= m.per_purchase_cap, f"{p.amount} exceeds the per-purchase cap {m.per_purchase_cap}")
    rule("daily_cap", totals.spent_today + p.amount <= m.daily_cap, f"{totals.spent_today} spent today + {p.amount} exceeds the daily cap {m.daily_cap}")
    rule("monthly_cap", totals.spent_month + p.amount <= m.monthly_cap, f"{totals.spent_month} spent this month + {p.amount} exceeds the monthly cap {m.monthly_cap}")
    rule("open_orders", totals.open_orders < m.max_open_orders, f"{totals.open_orders} orders are already waiting for approval (limit {m.max_open_orders})")
    rule("velocity", totals.proposals_last_hour < m.velocity_per_hour, f"{totals.proposals_last_hour} proposals in the last hour (limit {m.velocity_per_hour})")
    rule("critic", not critic_flags, "the purchase critic flagged: " + "; ".join(critic_flags))
    return Decision("BLOCKED" if why else "NEEDS_APPROVAL", why, checked)
