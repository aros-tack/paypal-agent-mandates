"""Ledger: purchases (SQLite) and a hash-chained audit log.

Every decision, order, approval, capture and webhook is appended to a JSONL file in which each record carries the SHA-256 of the previous one,
so deleting or editing any past record is detectable (`verify_chain`).  Spend totals come from the purchases table, never from the agent.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .policy import Totals

SCHEMA = """CREATE TABLE IF NOT EXISTS purchases(
  id TEXT PRIMARY KEY, mandate_id TEXT, agent_id TEXT, merchant TEXT, category TEXT, description TEXT, justification TEXT,
  amount TEXT, currency TEXT, status TEXT, paypal_order_id TEXT, approve_url TEXT, capture_id TEXT, decision TEXT, reasons TEXT,
  warnings TEXT, brief TEXT, risk TEXT, goal_fit REAL, ai_mode TEXT, task TEXT, created REAL, updated REAL)"""
OPEN = ("PENDING_APPROVAL",)
COUNTED = ("PENDING_APPROVAL", "APPROVED", "CAPTURED")     # money committed or spent; blocked, declined and expired requests do not count


class Ledger:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.dir / "audit.jsonl"
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.dir / "ledger.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.execute("CREATE TABLE IF NOT EXISTS webhook_events(id TEXT PRIMARY KEY, seen REAL)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---- audit chain
    def audit(self, kind: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            prev = "0" * 64
            if self.audit_path.exists():
                last = None
                with self.audit_path.open("rb") as fh:
                    for line in fh:
                        if line.strip():
                            last = line
                if last:
                    prev = json.loads(last)["hash"]
            rec = {"at": round(time.time(), 3), "kind": kind, "data": data, "prev": prev}
            rec["hash"] = hashlib.sha256(json.dumps({k: rec[k] for k in ("at", "kind", "data", "prev")}, sort_keys=True, default=str).encode()).hexdigest()
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            return rec

    def verify_chain(self) -> tuple[bool, str]:
        prev = "0" * 64
        if not self.audit_path.exists():
            return True, "empty"
        for n, line in enumerate(self.audit_path.read_text(encoding="utf-8").splitlines(), 1):
            rec = json.loads(line)
            body = json.dumps({k: rec[k] for k in ("at", "kind", "data", "prev")}, sort_keys=True, default=str)
            if rec["prev"] != prev or hashlib.sha256(body.encode()).hexdigest() != rec["hash"]:
                return False, f"record {n} does not match its chain"
            prev = rec["hash"]
        return True, f"{n} records verified"

    def first_time(self, event_id: str) -> bool:
        """True once per webhook event id: a replayed (re-delivered or re-sent) event is recognised and ignored."""
        with self._lock:
            try:
                self.db.execute("INSERT INTO webhook_events(id, seen) VALUES(?,?)", (event_id, time.time()))
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # ---- purchases
    def add(self, row: dict[str, Any]) -> None:
        with self._lock:
            now = time.time()
            row = {**row, "created": now, "updated": now}
            cols = ",".join(row)
            self.db.execute(f"INSERT INTO purchases({cols}) VALUES({','.join('?' * len(row))})", tuple(str(v) if isinstance(v, (Decimal, list)) else v for v in row.values()))
            self.db.commit()

    def update(self, pid: str, **fields: Any) -> None:
        with self._lock:
            fields["updated"] = time.time()
            self.db.execute(f"UPDATE purchases SET {','.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), pid))
            self.db.commit()

    def get(self, pid: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM purchases WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    def by_order(self, order_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM purchases WHERE paypal_order_id=?", (order_id,)).fetchone()
        return dict(r) if r else None

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM purchases" + (" WHERE status=?" if status else "") + " ORDER BY created DESC"
        return [dict(r) for r in self.db.execute(q, (status,) if status else ())]

    def totals(self, agent_id: str, *, now: float | None = None) -> Totals:
        now = time.time() if now is None else now
        t = time.gmtime(now)
        day0 = now - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)
        month0 = day0 - (t.tm_mday - 1) * 86400
        marks = ",".join("?" * len(COUNTED))

        def total(since: float) -> Decimal:
            rows = self.db.execute(f"SELECT amount FROM purchases WHERE agent_id=? AND created>=? AND status IN ({marks})", (agent_id, since, *COUNTED)).fetchall()
            return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))
        open_n = self.db.execute("SELECT COUNT(*) c FROM purchases WHERE agent_id=? AND status='PENDING_APPROVAL'", (agent_id,)).fetchone()["c"]
        hour_n = self.db.execute("SELECT COUNT(*) c FROM purchases WHERE agent_id=? AND created>=?", (agent_id, now - 3600)).fetchone()["c"]
        return Totals(total(day0), total(month0), open_n, hour_n)
