"""An MCP server (JSON-RPC 2.0 over stdio) that gives any MCP-capable agent a PayPal spending tool that is already governed by its mandate.

Tools
  propose_purchase  ask to buy something; returns BLOCKED with the reasons, or PENDING_APPROVAL with the PayPal link the HUMAN must open
  check_purchase    ask PayPal-backed status of a purchase
  capture_purchase  finish a purchase the human has approved (refused until PayPal itself reports it approved)
  cancel_purchase   withdraw a pending purchase
  show_mandate      the limits the agent operates under (so it can plan within them)
There is deliberately no approve tool: approval is the human's act, on PayPal.
"""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .service import MandateService

PROTOCOL = "2024-11-05"
TOOLS = [
    {"name": "propose_purchase", "description": "Ask to make a purchase through PayPal. The request is checked against the spending mandate and an AI critic; if allowed, a PayPal order is created and a human must approve it on PayPal. Returns status BLOCKED (with reasons) or PENDING_APPROVAL (with approve_url for the human).",
     "inputSchema": {"type": "object", "required": ["merchant", "amount", "category", "description", "justification"], "properties": {
         "merchant": {"type": "string", "description": "merchant domain, e.g. shop.example"}, "amount": {"type": "string", "description": "decimal amount, e.g. \"19.99\""},
         "category": {"type": "string"}, "description": {"type": "string", "description": "what is being bought"}, "justification": {"type": "string", "description": "why the task needs it"},
         "currency": {"type": "string"}, "task": {"type": "string", "description": "the task the human gave you, verbatim; lets the AI reviewer check this purchase serves it"}}}},
    {"name": "check_purchase", "description": "Current status of a purchase (PENDING_APPROVAL, APPROVED, CAPTURED, BLOCKED, CANCELLED...).",
     "inputSchema": {"type": "object", "required": ["purchase_id"], "properties": {"purchase_id": {"type": "string"}}}},
    {"name": "capture_purchase", "description": "Complete a purchase the human has already approved on PayPal. Refused while PayPal reports the order as not approved.",
     "inputSchema": {"type": "object", "required": ["purchase_id"], "properties": {"purchase_id": {"type": "string"}}}},
    {"name": "cancel_purchase", "description": "Withdraw a purchase that is still waiting for approval.",
     "inputSchema": {"type": "object", "required": ["purchase_id"], "properties": {"purchase_id": {"type": "string"}}}},
    {"name": "show_mandate", "description": "The spending limits and rules this agent operates under.", "inputSchema": {"type": "object", "properties": {}}},
]


class McpServer:
    def __init__(self, service: MandateService) -> None:
        self.svc = service

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        s = self.svc
        if name == "propose_purchase":
            return s.propose(merchant=str(args.get("merchant", "")), amount=args.get("amount"), category=str(args.get("category", "")), description=str(args.get("description", "")),
                             justification=str(args.get("justification", "")), currency=args.get("currency"), task=str(args.get("task", "")))
        if name == "check_purchase":
            return s.status(str(args.get("purchase_id", "")))
        if name == "capture_purchase":
            return s.capture(str(args.get("purchase_id", "")))
        if name == "cancel_purchase":
            return s.cancel(str(args.get("purchase_id", "")))
        if name == "show_mandate":
            return s.mandate.to_dict()
        raise KeyError(name)

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        mid, method = msg.get("id"), msg.get("method", "")
        if mid is None:                                                      # notification
            return None
        def ok(result: Any) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        def err(code: int, text: str) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
        if method == "initialize":
            return ok({"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": {"name": "paypal-agent-mandates", "version": "1.0.0"}})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": TOOLS})
        if method == "tools/call":
            p = msg.get("params") or {}
            try:
                out = self.call_tool(p.get("name", ""), p.get("arguments") or {})
            except KeyError:
                return err(-32602, f"unknown tool {p.get('name')!r}")
            except Exception as exc:                                         # a tool failure is reported to the agent, never crashes the server
                return ok({"content": [{"type": "text", "text": json.dumps({"error": type(exc).__name__ + ": " + str(exc)})}], "isError": True})
            return ok({"content": [{"type": "text", "text": json.dumps(out, default=str)}], "isError": out.get("status") in ("FAILED", "UNKNOWN")})
        return err(-32601, f"method not found: {method}")

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
                stdout.flush()
                continue
            reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()
