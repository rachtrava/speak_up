"""Append-only, hash-chained audit ledger.

Each record stores sha256(prev_hash + canonical record). Editing or deleting any row
breaks verification from that point on. SQLite triggers also block UPDATE/DELETE.
"""
from __future__ import annotations

from typing import Any

from .db import Database, jdump, jload
from .util import Principal, iso, sha256

GENESIS = "0" * 64
SECRET_KEYS = {"token", "password", "secret", "access_token", "refresh_token", "code"}


def _redact(detail: dict) -> dict:
    out = {}
    for k, v in detail.items():
        if k.lower() in SECRET_KEYS:
            out[k] = "[redacted]"
        elif isinstance(v, dict):
            out[k] = _redact(v)
        else:
            out[k] = v
    return out


class AuditLedger:
    def __init__(self, db: Database):
        self.db = db
        with db.tx() as c:
            c.execute(
                "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log "
                "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
            )
            c.execute(
                "CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log "
                "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
            )

    def record(
        self,
        actor: Principal,
        action: str,
        outcome: str = "ok",
        target_type: str | None = None,
        target_id: str | None = None,
        **detail: Any,
    ) -> int:
        detail = _redact(detail)
        if actor.type == "agent":
            detail.setdefault("simulated", actor.is_simulated)
            if actor.credential_kind:
                detail.setdefault("via", actor.credential_kind)
            if actor.oauth_client_id:
                detail.setdefault("oauth_client_id", actor.oauth_client_id)
        with self.db.tx() as c:
            prev = c.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev["hash"] if prev else GENESIS
            at = iso()
            body = jdump([at, actor.type, actor.id, action, target_type, target_id, outcome, detail])
            h = sha256(prev_hash + body)
            cur = c.execute(
                "INSERT INTO audit_log (at, actor_type, actor_id, action, target_type, target_id, outcome, detail, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (at, actor.type, actor.id, action, target_type, target_id, outcome, jdump(detail), prev_hash, h),
            )
            return cur.lastrowid

    def verify(self) -> dict:
        prev_hash = GENESIS
        n = 0
        for r in self.db.all("SELECT * FROM audit_log ORDER BY seq"):
            body = jdump([
                r["at"], r["actor_type"], r["actor_id"], r["action"], r["target_type"],
                r["target_id"], r["outcome"], jload(r["detail"], {}),
            ])
            if r["prev_hash"] != prev_hash or sha256(prev_hash + body) != r["hash"]:
                return {"valid": False, "broken_at_seq": r["seq"], "records_checked": n}
            prev_hash = r["hash"]
            n += 1
        return {"valid": True, "records_checked": n, "head": prev_hash}

    def list(self, limit: int = 200, target_id: str | None = None) -> list[dict]:
        if target_id:
            rows = self.db.all(
                "SELECT * FROM audit_log WHERE target_id=? ORDER BY seq DESC LIMIT ?", (target_id, limit)
            )
        else:
            rows = self.db.all("SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,))
        for r in rows:
            r["detail"] = jload(r["detail"], {})
        return rows
