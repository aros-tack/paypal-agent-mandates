# Mandate — spending authority for AI agents on PayPal

**An AI agent proposes. PayPal approval is yours.**

AI agents can now be handed a PayPal integration. The hard question is not *can* the agent pay, it is *who is in charge when it does*: a prompt-injected invoice, a
hallucinated vendor, a runaway loop. Mandate is a small, dependency-free layer that sits between any agent and PayPal and makes every payment pass through
**the owner's rules, an AI critic, a human's PayPal approval, and a tamper-evident audit trail**.

```
 agent (any MCP client / any LLM)
        │  propose_purchase(merchant, amount, category, description, justification)
        ▼
 ┌─────────────────────── Mandate ───────────────────────┐
 │ 1. mandate rules   caps (per purchase / day / month), merchants, categories,   │
 │                    open-order limit, velocity, expiry  → BLOCKED with reasons  │
 │ 2. purchase critic heuristics + any OpenAI-compatible model: prompt injection  │
 │                    in the item text, fake pre-authorisation, "hide it from the  │
 │                    owner", scam payment methods → BLOCKED; loose justification  │
 │                    → warning shown to the human, not a block                    │
 │ 3. PayPal Orders v2: create order (PayPal-Request-Id idempotency)               │
 └──────────────┬─────────────────────────────────────────┘
                ▼
     the HUMAN approves on PayPal's own approval page   ← the agent has no approve tool
                ▼
 capture_purchase: refused unless PayPal itself reports the order APPROVED for exactly
 the amount and currency the ledger recorded, while the mandate is still valid
                ▼
 webhooks (signature-verified through PayPal's verify-webhook-signature) reconcile the ledger;
 every step is appended to a SHA-256 hash-chained audit log
```

## What PayPal and AI are doing here

| PayPal (free sandbox) | AI |
|---|---|
| **Orders v2**: create (`intent=CAPTURE`, `PAY_NOW` experience), get, capture; field names, enums and headers checked against PayPal's published OpenAPI spec | **The agent**: any tool-calling LLM through the bundled **MCP server** (`propose_purchase`, `check_purchase`, `capture_purchase`, `cancel_purchase`, `show_mandate`) |
| **PayPal's approval page is the human-in-the-loop**: the payer approval step is the control, not an afterthought | **The purchase reviewer** (a local or hosted model): reads the human's *task*, the item, the merchant text and the agent's justification, and returns hard flags, a goal-fit score, merchant plausibility, a risk level and a two-sentence **brief for the human approver** shown on the approval card |
| **Webhooks** + `verify-webhook-signature`; replayed events recognised by id | **Mandate drafting**: `python -m mandate draft "let my research agent spend up to $50 per purchase on data and software, never gift cards"` → a validated mandate you confirm with `--save` |
| **Idempotency** (`PayPal-Request-Id`), `ORDER_ALREADY_CAPTURED` recovery so a retry or crash never double-charges | |

**Where the model stops.** The model can only make a purchase *more* restricted or better explained: it can add a block, add a warning, write the brief. The caps, allow/deny lists, the human's PayPal approval and the verified capture are plain deterministic code, so a wrong or manipulated model can never spend money. The item text is passed to the model as *untrusted*. With no model reachable the system says so (`ai_mode: heuristics-only`) and falls back to the deterministic checks and a templated brief. A model is auto-detected (local Ollama) or set with `MANDATE_LLM_URL` / `MANDATE_LLM_MODEL` / `MANDATE_LLM_KEY` (any OpenAI-compatible endpoint).

## Run it (Python 3.10+, no third-party packages)

```bash
python -m unittest discover -t . -s tests     # 50 tests: policy, AI review, mandate drafting, ledger, full PayPal flow, replay/expiry/tamper, MCP, dashboard, webhooks
python -m mandate.live_check --emulator        # six end-to-end scenarios, no credentials
python demo.py                                 # scripted scenario against the bundled sandbox emulator
python agent_demo.py                           # a real local LLM agent (Ollama, qwen2.5:14b) using the tools, incl. an injected invoice
```

### Against the real PayPal sandbox

1. Create a free sandbox REST app at developer.paypal.com and use a sandbox *buyer* account.
2. `export PAYPAL_CLIENT_ID=… PAYPAL_CLIENT_SECRET=…` (read at call time; never logged or stored). `PAYPAL_API_BASE` defaults to `https://api-m.sandbox.paypal.com`.
3. `python -m mandate.live_check` — runs six scenarios against the real sandbox (successful approval, refused approval, amount manipulation after approval, approval-window expiry, replayed capture, verified capture) and writes `live_evidence.json` with the PayPal order and capture ids. You approve each order as your sandbox *buyer* (the script prints the links), or automatically with `PAYPAL_SANDBOX_BUYER_EMAIL` / `PAYPAL_SANDBOX_BUYER_PASSWORD` and Playwright (best effort). It refuses to run against `api-m.paypal.com`.
4. `python demo.py --live` — the narrated scenario against the sandbox.
5. `python -m mandate dashboard` serves the approver's view on `http://127.0.0.1:8808` (pending approvals with the PayPal button, blocked requests with reasons, spend vs. limits, audit-chain health). Set `PAYPAL_WEBHOOK_ID` and point a sandbox webhook at `/paypal/webhook`.

### As an MCP server (Claude Desktop, any MCP host)

```json
{"mcpServers": {"paypal-mandates": {"command": "python", "args": ["-m", "mandate", "mcp", "--mandate", "examples/mandate.json"], "env": {"PAYPAL_CLIENT_ID": "…", "PAYPAL_CLIENT_SECRET": "…"}}}}
```

## Verified against the real PayPal sandbox

`evidence/sandbox_orders_partial.json` is produced by calls to `https://api-m.sandbox.paypal.com` (PayPal-confirmed facts are kept apart from local assertions in that file):

| PayPal-confirmed (real sandbox) | Result |
|---|---|
| OAuth + `POST /v2/checkout/orders` with the `PAY_NOW` experience context | order created, status `PAYER_ACTION_REQUIRED`, a `payer-action` link returned |
| `POST …/capture` on an order the buyer has not approved | HTTP 422 `ORDER_NOT_APPROVED` (so an agent cannot self-capture) |
| `PATCH` of the amount on a `CREATED` order | accepted, and reflected by `GET` (the merchant-side alteration Mandate's capture check exists to catch) |
| Re-sending order creation with the same `PayPal-Request-Id` | the same order id (idempotency) |
| `GET` of an unknown order | HTTP 404 |

The buyer-approval scenarios (successful approval + capture, refused approval, amount change after approval, expiry, replayed capture, verified capture) run with `python -m mandate.live_check` once a sandbox buyer logs in; they have **not** been completed against the real sandbox in this repository's evidence. Everything that shows buyer approval and capture here (the demo, the tests, the video) runs on the bundled emulator, which imitates PayPal and is not PayPal.

## The mandate

```json
{"agent_id": "research-assistant", "currency": "USD", "per_purchase_cap": "75.00", "daily_cap": "120.00", "monthly_cap": "400.00",
 "allowed_categories": ["software", "data", "cloud", "books"], "denied_merchants": ["giftcards.example"], "max_open_orders": 2, "velocity_per_hour": 6}
```

Spend totals come from Mandate's own ledger (pending + approved + captured money counts), never from anything the agent says.

## Design decisions worth knowing

* **There is no approve tool.** Approval is a human act on PayPal; an agent that is compromised still cannot spend.
* **Capture is verified, not trusted.** The order is re-read from PayPal and must be `APPROVED` with the ledger's exact amount and currency before capture; a tampered order is refused.
* **Policy is pure.** `policy.evaluate` does no I/O, so the same request and totals always give the same decision and every rule is unit-tested, including the exact-cap boundaries.
* **The audit log is hash-chained.** Editing or deleting any past record is detected by `Ledger.verify_chain()` (the demo shows it).
* **Zero dependencies.** Standard library only: it runs anywhere judges run Python.

## Honest limits

* The bundled `emulator/` imitates only the PayPal endpoints Mandate calls so that the tests and the offline demo are deterministic. It is **not** PayPal: real behaviour is exercised with `--live` against your own sandbox app.
* Sandbox only. No vaulting / unattended capture: by design every payment needs the human's PayPal approval. Refunds, disputes and subscriptions are out of scope.
* The critic is a first line of defence, not a guarantee; the hard caps and the human approval are what make the system safe even when the critic is wrong.

## Layout

`mandate/policy.py` rules · `critic.py` AI/heuristic review · `service.py` the flow · `paypal.py` Orders v2 client · `ledger.py` SQLite + hash chain · `mcp_server.py` MCP tools · `web.py` approver dashboard + webhook · `emulator/` offline PayPal stand-in · `tests/` · `demo.py`, `agent_demo.py`

*Built with AI assistance (design, implementation and tests were produced by an AI coding agent working under human direction).* MIT licensed.

## Support

Mandate is free and MIT-licensed. It is an ARIA/AIOS project: AI-assisted design, code and tests under human direction. If it is useful to you, ARIA's small-business tools and tips are on Ko-fi: https://ko-fi.com/P7A426ZPCE . Bug reports and pull requests are welcome through issues here.
