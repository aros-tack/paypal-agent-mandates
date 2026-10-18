import io
import json
import tempfile
import threading
import unittest
import urllib.request

from emulator.paypal_sandbox_emulator import Emulator
from mandate.ledger import Ledger
from mandate.mcp_server import TOOLS, McpServer
from mandate.paypal import PayPal
from mandate.policy import Mandate
from mandate.service import MandateService
from mandate.web import make_server

M = {"mandate_id": "m1", "agent_id": "agent", "per_purchase_cap": "50.00", "daily_cap": "80.00", "monthly_cap": "200.00", "allowed_categories": ["software"]}


class McpAndWeb(unittest.TestCase):
    def setUp(self):
        self.emu = Emulator().start()
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(self.tmp.name)
        self.svc = MandateService(Mandate.from_dict(M), self.ledger, PayPal(self.emu.url, "i", "s"), base_url="http://127.0.0.1:0")
        self.mcp = McpServer(self.svc)

    def tearDown(self):
        self.emu.stop()
        self.ledger.close()
        self.tmp.cleanup()

    def rpc(self, method, params=None, mid=1):
        return self.mcp.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        return json.loads(r["result"]["content"][0]["text"])

    def test_protocol_basics_and_there_is_no_approve_tool(self):
        self.assertEqual(self.rpc("initialize")["result"]["serverInfo"]["name"], "paypal-agent-mandates")
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, [t["name"] for t in TOOLS])
        self.assertFalse(any("approve" in n for n in names))
        self.assertIsNone(self.mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})["error"]["code"], -32602)

    def test_agent_session_over_stdio(self):
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "propose_purchase", "arguments": {"merchant": "shop.example", "amount": "12.50", "category": "software",
                                                                                                                   "description": "pandas course licence", "justification": "the analyst agent needs the pandas course"}}}]
        out = io.StringIO()
        self.mcp.serve(io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\nnot json\n"), out)
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[1]["result"]["content"][0]["text"])["status"], "PENDING_APPROVAL")
        self.assertEqual(lines[2]["error"]["code"], -32700)

    def test_agent_cannot_capture_until_the_human_has_approved(self):
        r = self.call("propose_purchase", merchant="shop.example", amount="12.50", category="software", description="pandas course licence", justification="the analyst agent needs the pandas course")
        self.assertEqual(self.call("capture_purchase", purchase_id=r["purchase_id"])["status"], "PENDING_APPROVAL")
        self.emu.human_approves(r["approve_url"])
        self.assertEqual(self.call("capture_purchase", purchase_id=r["purchase_id"])["status"], "CAPTURED")
        self.assertEqual(self.call("check_purchase", purchase_id=r["purchase_id"])["status"], "CAPTURED")
        self.assertEqual(self.call("show_mandate")["agent_id"], "agent")

    def test_dashboard_return_page_and_webhook(self):
        srv = make_server(self.svc, port=0)
        port = srv.server_address[1]
        self.svc.base_url = f"http://127.0.0.1:{port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = self.call("propose_purchase", merchant="shop.example", amount="9.00", category="software", description="linter pro licence", justification="the build agent needs the linter licence")
            page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
            self.assertIn("Review and approve on PayPal", page)
            self.assertIn("✔", page)
            self.emu.human_approves(r["approve_url"])
            ret = urllib.request.urlopen(f"http://127.0.0.1:{port}/paypal/return?purchase={r['purchase_id']}", timeout=10).read().decode()
            self.assertIn("Payment CAPTURED", ret)
            blocked = self.call("propose_purchase", merchant="shop.example", amount="500.00", category="software", description="enterprise suite", justification="the build needs the enterprise suite")
            page2 = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
            self.assertIn("Blocked before PayPal", page2)
            body = json.dumps({"id": "WH-2", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "CAP1", "supplementary_data": {"related_ids": {"order_id": r["paypal_order_id"]}}}}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/paypal/webhook", body, {"content-type": "application/json"}, method="POST")
            self.assertEqual(urllib.request.urlopen(req, timeout=10).status, 200)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
