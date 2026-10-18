"""Scenario check of the whole payment flow, against the real PayPal sandbox or (for CI / dry runs) the bundled emulator.

  python -m mandate.live_check --emulator           all scenarios, no credentials, no human
  python -m mandate.live_check                      real sandbox; set PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (a free sandbox app)
        approvals are given by YOU as the sandbox buyer (the script prints each PayPal approval link), or automatically with
        PAYPAL_SANDBOX_BUYER_EMAIL / PAYPAL_SANDBOX_BUYER_PASSWORD and Playwright (best effort; PayPal's pages change)

Scenarios: successful approval + capture · refused approval · amount manipulation after approval · approval-window expiry · replayed capture · capture verification.
Sandbox only: the base URL must be api-m.sandbox.paypal.com (or the emulator); the script refuses to run against api-m.paypal.com. Never a real-money transaction.
Writes a JSON evidence file with the PayPal order and capture ids of every scenario.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from typing import Callable

from .ledger import Ledger
from .paypal import SANDBOX, PayPal, PayPalError
from .policy import Mandate
from .service import MandateService

MANDATE = {"mandate_id": "live-check", "agent_id": "live-check-agent", "currency": "USD", "per_purchase_cap": "25.00", "daily_cap": "100.00", "monthly_cap": "200.00",
           "allowed_categories": ["software"], "max_open_orders": 10, "velocity_per_hour": 60, "approval_window": 600}


_PAY = "#payment-submit-btn, button:has-text('Complete Purchase'), button:has-text('Pay Now')"


def buyer_visit(link: str, action: str = "check") -> str:
    """Log in as the sandbox buyer on PayPal's page and, per `action`, stop at the approval page ("check"), approve, or cancel.
    Returns one of: OK, NO_CREDENTIALS, LOGIN_REJECTED, SELLER_ACCOUNT, NO_APPROVAL_PAGE, NO_BROWSER. Never returns or prints credentials."""
    email, pw = os.environ.get("PAYPAL_SANDBOX_BUYER_EMAIL"), os.environ.get("PAYPAL_SANDBOX_BUYER_PASSWORD")
    if not email or not pw:
        return "NO_CREDENTIALS"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "NO_BROWSER"
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        try:
            pg.goto(link, wait_until="domcontentloaded")
            pg.fill("input[name='login_email'], #email", email)
            if pg.locator("#btnNext").count():
                pg.click("#btnNext")
            pg.fill("input[name='login_password'], #password", pw)
            pg.click("#btnLogin")
            try:
                pg.wait_for_selector(_PAY, timeout=45000)
            except Exception:
                pass
            pg.wait_for_timeout(6000)
            body = pg.inner_text("body").lower()
            if "genericError" in pg.url or "seller for this" in body:
                return "SELLER_ACCOUNT"
            if "check your entries" in body or pg.locator(_PAY).count() == 0 and "login" in pg.url:
                return "LOGIN_REJECTED"
            if pg.locator(_PAY).count() == 0:
                return "NO_APPROVAL_PAGE"
            if action == "approve":
                pg.click(_PAY)
                pg.wait_for_url(lambda u: "checkoutnow" not in u and "checkoutweb" not in u, timeout=60000)
            elif action == "cancel":
                pg.click("a:has-text('Cancel'), button:has-text('Cancel'), #cancelLink")
                pg.wait_for_url(lambda u: "checkoutnow" not in u, timeout=60000)
            return "OK"
        except Exception:
            return "NO_APPROVAL_PAGE"
        finally:
            b.close()


def browser_approver(link: str) -> bool:
    return buyer_visit(link, "approve") == "OK"


def browser_canceller(link: str) -> bool:
    return buyer_visit(link, "cancel") == "OK"


def preflight() -> int:
    """Four separate results; creates no order unless application authentication passes, and moves no money."""
    import uuid
    paypal = PayPal()
    if paypal.base != SANDBOX:
        sys.exit("sandbox only")
    res = {"Application authentication": "FAIL", "Buyer identity": "NOT RUN", "Buyer login": "NOT RUN", "Sandbox purchase and capture": "NOT RUN (runs in the scenarios)"}
    try:
        paypal.token()
        res["Application authentication"] = "PASS"
    except Exception as exc:
        res["Application authentication"] = "FAIL (PayPal refused the Client ID / Secret" + (f", HTTP {getattr(exc, 'status', '')}" if getattr(exc, "status", "") else "") + ")"
    if res["Application authentication"] == "PASS":
        o = paypal.create_order(amount="1.00", currency="USD", description="buyer login check", reference_id="chk-" + uuid.uuid4().hex[:8], return_url="http://127.0.0.1:8808/r", cancel_url="http://127.0.0.1:8808/c")
        link = next(l["href"] for l in o["links"] if l["rel"] == "payer-action")
        v = buyer_visit(link, "check")
        res["Buyer login"] = {"OK": "PASS"}.get(v, f"FAIL ({v})")
        res["Buyer identity"] = {"OK": "PASS (a separate buyer, not the app's seller account)", "SELLER_ACCOUNT": "FAIL (this is the seller account that owns the app)"}.get(v, "UNDETERMINED (login did not complete)")
    for k, v in res.items():
        print(f"{k}: {v}")
    return 0 if res["Application authentication"] == "PASS" and res["Buyer login"] == "PASS" and res["Buyer identity"].startswith("PASS") else 1


def check_buyer_login() -> int:
    return preflight()


def human_approver(link: str) -> bool:
    print(f"\n  Open this link and approve as your sandbox BUYER:\n  {link}")
    input("  Press Enter when you have approved… ")
    return True


class Scenarios:
    def __init__(self, svc: MandateService, approve: Callable[[str], bool], cancel: Callable[[str], bool], out: dict) -> None:
        self.svc, self.approve, self.cancel, self.out = svc, approve, cancel, out

    def propose(self, amount="10.00", desc="developer tools licence"):
        return self.svc.propose(merchant="tools.example", amount=amount, category="software", description=desc, justification="the build agent needs the developer tools", task="set up the build")

    def record(self, name: str, ok: bool, **evidence) -> bool:
        self.out["scenarios"][name] = {"pass": ok, **evidence}
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        return ok

    def success(self) -> bool:
        r = self.propose()
        early = self.svc.capture(r["purchase_id"])
        self.approve(r["approve_url"])
        done = self.svc.capture(r["purchase_id"])
        return self.record("successful approval and capture", r["status"] == "PENDING_APPROVAL" and early["status"] == "PENDING_APPROVAL" and done["status"] == "CAPTURED",
                           paypal_order=r.get("paypal_order_id"), capture=done.get("capture_id"), refused_before_approval=early["status"] == "PENDING_APPROVAL")

    def refused(self) -> bool:
        r = self.propose()
        self.cancel(r["approve_url"])
        out = self.svc.capture(r["purchase_id"])
        return self.record("refused approval leaves nothing capturable", out["status"] in ("PENDING_APPROVAL", "EXPIRED", "CANCELLED"), paypal_order=r.get("paypal_order_id"), status=out["status"])

    def amount_manipulation(self) -> bool:
        r = self.propose("10.00")
        self.approve(r["approve_url"])
        try:
            self.svc.paypal.patch_order(r["paypal_order_id"], [{"op": "replace", "path": f"/purchase_units/@reference_id=='{r['purchase_id']}'/amount",
                                                               "value": {"currency_code": "USD", "value": "24.00"}}])
        except PayPalError as exc:
            return self.record("amount manipulation after approval is refused", False, error=str(exc)[:200])
        out = self.svc.capture(r["purchase_id"])
        order = self.svc.paypal.get_order(r["paypal_order_id"])
        return self.record("amount manipulation after approval is refused", out["status"] == "BLOCKED" and order.get("status") != "COMPLETED", paypal_order=r.get("paypal_order_id"), status=out["status"], paypal_status=order.get("status"))

    def expiry(self) -> bool:
        r = self.propose()
        self.approve(r["approve_url"])
        real = self.svc.clock
        self.svc.clock = lambda: real() + self.svc.mandate.approval_window + 5
        out = self.svc.capture(r["purchase_id"])
        self.svc.clock = real
        return self.record("approval window expiry refuses capture", out["status"] == "EXPIRED", paypal_order=r.get("paypal_order_id"), status=out["status"])

    def replay(self) -> bool:
        r = self.propose()
        self.approve(r["approve_url"])
        first = self.svc.capture(r["purchase_id"])
        self.svc.ledger.update(r["purchase_id"], status="APPROVED", capture_id="")       # as if a crash lost the CAPTURED mark
        second = self.svc.capture(r["purchase_id"])
        order = self.svc.paypal.get_order(r["paypal_order_id"])
        caps = ((order.get("purchase_units") or [{}])[0].get("payments", {}).get("captures") or [])
        return self.record("replayed capture never charges twice", first["status"] == "CAPTURED" and second["status"] == "CAPTURED" and len(caps) == 1 and first.get("capture_id") == second.get("capture_id"),
                           paypal_order=r.get("paypal_order_id"), captures_on_paypal=len(caps), capture=first.get("capture_id"))

    def verification(self) -> bool:
        r = self.propose("10.00")
        self.approve(r["approve_url"])
        order = self.svc.paypal.get_order(r["paypal_order_id"])
        amt = (order.get("purchase_units") or [{}])[0].get("amount", {})
        done = self.svc.capture(r["purchase_id"])
        row = self.svc.ledger.get(r["purchase_id"])
        return self.record("capture is verified against PayPal's own record", order.get("status") == "APPROVED" and amt.get("value") == "10.00" and done["status"] == "CAPTURED" and bool(row["capture_id"]),
                           paypal_order=r.get("paypal_order_id"), paypal_status_before_capture=order.get("status"), capture=row["capture_id"], audit_chain=self.svc.ledger.verify_chain()[1])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emulator", action="store_true")
    ap.add_argument("--preflight", dest="check_buyer", action="store_true")
    ap.add_argument("--check-buyer", action="store_true", help="one login attempt as the sandbox buyer; prints ACCEPTED or REJECTED")
    ap.add_argument("--evidence", default="live_evidence.json")
    args = ap.parse_args(argv)
    if args.check_buyer:
        return check_buyer_login()
    emu = None
    if args.emulator:
        from emulator.paypal_sandbox_emulator import Emulator
        emu = Emulator().start()
        paypal = PayPal(emu.url, "emu", "emu")
        approve = lambda link: (emu.human_approves(link), True)[1]
        cancel = lambda link: True
        mode = "emulator"
    else:
        paypal = PayPal()
        if paypal.base != SANDBOX:
            sys.exit(f"refusing to run: PAYPAL_API_BASE is {paypal.base}; this check is sandbox-only ({SANDBOX})")
        mode = "paypal-sandbox"
        approve = lambda link: browser_approver(link) or human_approver(link)
        cancel = lambda link: browser_canceller(link) or print("\n  Open this link and press CANCEL (refuse the payment) as the sandbox buyer:\n  " + link) or input("  Press Enter when done… ") or True
        if preflight() != 0:
            print("stopping: the preflight did not pass, so no scenario order is created")
            return 3
    out = {"mode": mode, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "scenarios": {}}
    svc = MandateService(Mandate.from_dict(MANDATE), Ledger(tempfile.mkdtemp(prefix="mandate-live-")), paypal)
    sc = Scenarios(svc, approve, cancel, out)
    print(f"Mandate scenario check — {mode}")
    results = [sc.success(), sc.refused(), sc.amount_manipulation(), sc.expiry(), sc.replay(), sc.verification()]
    out["all_passed"] = all(results)
    out["audit_chain"] = svc.ledger.verify_chain()[1]
    with open(args.evidence, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"\n{sum(results)}/{len(results)} scenarios passed · evidence written to {args.evidence}")
    if emu:
        emu.stop()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
