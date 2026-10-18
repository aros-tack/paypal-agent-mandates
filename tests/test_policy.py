import unittest
from decimal import Decimal

from mandate.policy import Mandate, MandateError, Proposal, Totals, evaluate, money


def mandate(**kw):
    base = {"mandate_id": "m", "agent_id": "a", "per_purchase_cap": "50.00", "daily_cap": "80.00", "monthly_cap": "200.00", "allowed_categories": ["software"], "denied_merchants": ["bad.example"], "max_open_orders": 2, "velocity_per_hour": 3}
    base.update(kw)
    return Mandate.from_dict(base)


def prop(amount="20.00", merchant="shop.example", category="software", currency="USD"):
    return Proposal(merchant, Decimal(amount), currency, category, "a licence", "needed for the build")


class PolicyTests(unittest.TestCase):
    def test_within_limits_needs_human_approval_not_auto(self):
        d = evaluate(mandate(), prop(), Totals())
        self.assertEqual(d.verdict, "NEEDS_APPROVAL")
        self.assertEqual(d.reasons, [])
        self.assertIn("per_purchase_cap", d.rules_checked)

    def test_each_hard_rule_blocks_with_its_own_reason(self):
        cases = [
            (prop("60.00"), Totals(), "per-purchase cap"),
            (prop("30.00"), Totals(spent_today=Decimal("60.00")), "daily cap"),
            (prop("30.00"), Totals(spent_month=Decimal("190.00")), "monthly cap"),
            (prop(merchant="bad.example"), Totals(), "denied list"),
            (prop(merchant="x.bad.example"), Totals(), "denied list"),
            (prop(category="weapons"), Totals(), "not allowed"),
            (prop(currency="EUR"), Totals(), "mandate is in USD"),
            (prop(), Totals(open_orders=2), "waiting for approval"),
            (prop(), Totals(proposals_last_hour=3), "last hour"),
        ]
        for p, t, text in cases:
            d = evaluate(mandate(), p, t)
            self.assertEqual(d.verdict, "BLOCKED", text)
            self.assertTrue(any(text in r for r in d.reasons), (text, d.reasons))

    def test_exactly_at_the_cap_is_allowed(self):
        self.assertEqual(evaluate(mandate(), prop("50.00"), Totals()).verdict, "NEEDS_APPROVAL")
        self.assertEqual(evaluate(mandate(), prop("20.00"), Totals(spent_today=Decimal("60.00"))).verdict, "NEEDS_APPROVAL")

    def test_expiry_blocks(self):
        d = evaluate(mandate(expires_at=100.0), prop(), Totals(), now=101.0)
        self.assertEqual(d.verdict, "BLOCKED")

    def test_critic_flags_block(self):
        d = evaluate(mandate(), prop(), Totals(), critic_flags=("text tries to override the agent's instructions",))
        self.assertEqual(d.verdict, "BLOCKED")

    def test_malformed_money_is_refused(self):
        for bad in ("0", "-5", "abc", "10.999", "1e2x"):
            with self.assertRaises(MandateError):
                money(bad)
        self.assertEqual(money("10.50"), Decimal("10.50"))
        with self.assertRaises(MandateError):
            money("10.5", "JPY")

    def test_mandate_round_trip_and_missing_fields(self):
        m = mandate()
        self.assertEqual(Mandate.from_dict(m.to_dict()), m)
        with self.assertRaises(MandateError):
            Mandate.from_dict({"agent_id": "a"})


if __name__ == "__main__":
    unittest.main()
