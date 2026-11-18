"""Event engine: durable outbox dispatch to Telegram and signed webhooks, with retries.

The outbox row is written in the same transaction as the state change that caused it,
so events are never lost and never sent for changes that rolled back.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta
from typing import Callable

import httpx

from .db import Database, jdump, jload
from .util import iso, now

log = logging.getLogger("commons.events")
MAX_ATTEMPTS = 6

Sender = Callable[[str, dict], None]  # (channel, payload) -> raises on failure


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


class Dispatcher:
    def __init__(self, db: Database, core, telegram=None, http: httpx.Client | None = None):
        self.db = db
        self.core = core
        self.telegram = telegram
        self.http = http or httpx.Client(timeout=10)

    def process(self, limit: int = 50) -> dict:
        rows = self.db.all(
            "SELECT * FROM outbox WHERE status='pending' AND next_attempt_at<=? ORDER BY created_at LIMIT ?",
            (iso(), limit))
        stats = {"sent": 0, "failed": 0, "retry": 0, "skipped": 0}
        for r in rows:
            payload = jload(r["payload"], {})
            try:
                outcome = self._send(r["channel"], payload)
            except Exception as e:  # noqa: BLE001 - every failure is recorded, then retried
                attempts = r["attempts"] + 1
                final = attempts >= MAX_ATTEMPTS
                delay = timedelta(seconds=min(2 ** attempts * 5, 3600))
                self.db.run("UPDATE outbox SET attempts=?, status=?, last_error=?, next_attempt_at=? WHERE id=?",
                            (attempts, "failed" if final else "pending", _safe_err(e), iso(now() + delay), r["id"]))
                self.db.run("UPDATE deliveries SET attempts=?, status=?, last_error=?, next_attempt_at=? WHERE id=?",
                            (attempts, "failed" if final else "pending", _safe_err(e), iso(now() + delay), r["delivery_id"]))
                if final:
                    from .util import SYSTEM
                    self.core.audit.record(SYSTEM, f"delivery.{r['channel']}_failed", outcome="error",
                                           target_type="delivery", target_id=r["delivery_id"], error=_safe_err(e))
                stats["failed" if final else "retry"] += 1
                continue
            status = "skipped" if outcome == "skipped" else "delivered"
            self.db.run("UPDATE outbox SET status=?, attempts=attempts+1 WHERE id=?", (status, r["id"]))
            self.db.run("UPDATE deliveries SET status=?, attempts=attempts+1, delivered_at=?, last_error=? WHERE id=?",
                        (status, iso(), "telegram not configured" if outcome == "skipped" else None, r["delivery_id"]))
            stats["skipped" if outcome == "skipped" else "sent"] += 1
        return stats

    def _send(self, channel: str, payload: dict) -> str:
        if channel == "telegram":
            if not self.telegram or not self.telegram.enabled:
                return "skipped"
            msg = self.db.one("SELECT * FROM messages WHERE id=?", (payload["message_id"],))
            self.telegram.send(payload["chat_id"], render_for_telegram(self.core, msg))
            return "sent"
        if channel == "webhook":
            sub = self.db.one("SELECT * FROM event_subscriptions WHERE id=?", (payload["subscription_id"],))
            if not sub or not sub["active"]:
                return "skipped"
            body = jdump(payload).encode()
            ts = str(int(now().timestamp()))
            r = self.http.post(sub["callback_url"], content=body, headers={
                "Content-Type": "application/json",
                "X-Commons-Event-Id": payload["event_id"],
                "X-Commons-Timestamp": ts,
                "X-Commons-Signature": sign_webhook(sub["secret"], ts, body),
            })
            r.raise_for_status()
            return "sent"
        raise ValueError(f"unknown channel {channel}")


def render_for_telegram(core, msg: dict) -> str:
    """Every visible line is attributed. AI senders always carry the AI label and whom they represent."""
    if msg["sender_type"] == "agent":
        who = core.get_agent(msg["sender_id"])["label"]
    elif msg["sender_type"] == "human":
        who = f"👤 {core.get_human(msg['sender_id'])['display_name']} (human)"
    else:
        who = "⚙️ Commons"
    kind = {"task_request": "New task", "response": "Result", "progress": "Progress update",
            "synthesis": "Synthesis", "follow_up": "Follow-up", "notice": "Notice",
            "revision_request": "Revision requested", "status": "Status"}.get(msg["kind"], msg["kind"])
    to = ""
    if msg["recipient_agent_id"]:
        to = f" → {core.get_agent(msg['recipient_agent_id'])['display_name']}"
    body = msg["body"]
    if len(body) > 3200:
        body = body[:3200] + "\n… (truncated; full text in the Commons)"
    flags = jload(msg["injection_flags"], [])
    warn = f"\n⚠️ Screening flags: {', '.join(flags)}" if flags else ""
    ref = f"\n#{msg['task_id']}" if msg["task_id"] else ""
    return f"{who}{to}\n[{kind}]\n\n{body}{warn}{ref}"


def _safe_err(e: Exception) -> str:
    s = str(e)
    # Never persist bot tokens that may appear in request URLs.
    import re
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<redacted>", s)[:500]
