import json
import unittest
from decimal import Decimal

from mandate.policy import Mandate, Totals
from mandate.x402_hook import before_payment_hook


def mandate(**kw):
    base = {"mandate_id": "m", "agent_id": "a", "per_purchase_cap": "1.00", "daily_cap": "3.00", "monthly_cap": "10.00", "denied_merchants": ["bad.example"]}
    base.update(kw)
    return Mandate.from_dict(base)


def ctx(amount="250000", resource="https://api.good.example/v1/x", pay_to="0xabc"):
    return {"selectedRequirements": {"payTo": pay_to, "amount": amount, "resource": resource}}


class X402HookTests(unittest.TestCase):
    def test_within_mandate_and_approved_is_allowed_and_audited(self):
        log = []
        hook = before_payment_hook(mandate(), approver=lambda p, d: True, audit=log.append)
        self.assertIsNone(hook(ctx()))
        self.assertTrue(json.loads(log[0])["allow"])

    def test_over_the_per_purchase_cap_aborts_with_the_rule_reason(self):
        out = before_payment_hook(mandate(), approver=lambda p, d: True)(ctx(amount="2000000"))
        self.assertTrue(out["abort"])
        self.assertIn("per-purchase cap", out["reason"])

    def test_denied_seller_and_daily_total_are_enforced_from_the_ledger_not_the_agent(self):
        hook = before_payment_hook(mandate(), totals=lambda: Totals(spent_today=Decimal("2.90")), approver=lambda p, d: True)
        self.assertIn("denied", hook(ctx(resource="https://bad.example/p"))["reason"])
        self.assertIn("daily cap", hook(ctx(amount="250000"))["reason"])

    def test_needs_approval_aborts_without_an_approver(self):
        out = before_payment_hook(mandate())(ctx())
        self.assertTrue(out["abort"])
        self.assertIn("human approval", out["reason"])

    def test_python_spelling_and_unreadable_requests_are_handled(self):
        hook = before_payment_hook(mandate(), approver=lambda p, d: True)
        self.assertIsNone(hook({"selected_requirements": {"pay_to": "0xabc", "amount": "100000", "resource": "https://api.good.example/z"}}))
        self.assertTrue(hook({"selectedRequirements": {"payTo": "0xabc", "amount": "oops"}})["abort"])
        self.assertTrue(hook({})["abort"])


if __name__ == "__main__":
    unittest.main()
