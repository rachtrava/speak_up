"""The Commons Core: the single authoritative dispatcher.

Every state change goes through this class. Adapters (MCP, REST, Telegram, simulator)
authenticate a caller into a `Principal` and call these methods. Nothing else writes
task state, grants or deliveries.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from . import statemachine as sm
from .audit import AuditLedger
from .db import Database, jdump, jload
from .errors import AuthError, Conflict, Forbidden, LimitExceeded, NotFound, CommonsError
from .permissions import ACTIONS, INFO_CATEGORIES, PermissionEngine, actions_for_level
from .security import (
    DUPLICATE_WINDOW_SECONDS, MAX_OPEN_TASKS_PER_AGENT, MAX_TASK_DEPTH, MAX_TURNS_CEILING,
    UNTRUSTED_NOTICE, SafetyEngine, check_size, screen_content,
)
from .util import (
    SYSTEM, Principal, hash_password, in_future, iso, new_id, new_token, now, parse_iso,
    sha256, verify_password,
)

CONNECTION_TYPES = {
    "native_assistant": "Existing personal assistant connected through a provider-supported connector (e.g. ChatGPT or Claude custom MCP connector)",
    "standalone_api_agent": "Standalone agent you run with a model API key. It is NOT your personal assistant and has none of its memory.",
    "coding_agent": "Coding agent (e.g. Claude Code, Codex CLI) connected over MCP",
    "autonomous_activation": "Existing assistant that the provider can wake up on events without you opening a chat",
    "simulated": "SIMULATED test agent run by the Commons itself. Not a real assistant.",
}


class Core:
    def __init__(self, db: Database):
        self.db = db
        self.audit = AuditLedger(db)
        self.perms = PermissionEngine(db)
        self.safety = SafetyEngine(db)

    # ================================================================ identity
    def create_human(self, display_name: str, email: str, password: str, telegram_user_id: str | None = None) -> dict:
        if len(password) < 12:
            raise CommonsError("Password must be at least 12 characters.")
        hid = new_id("hum")
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO humans (id, display_name, email, password_hash, telegram_user_id, created_at) VALUES (?,?,?,?,?,?)",
                (hid, display_name, email.lower(), hash_password(password), telegram_user_id, iso()),
            )
            self.audit.record(SYSTEM, "human.created", target_type="human", target_id=hid, display_name=display_name)
        return self.get_human(hid)

    def get_human(self, hid: str) -> dict:
        h = self.db.one("SELECT id, display_name, email, telegram_user_id, created_at FROM humans WHERE id=?", (hid,))
        if not h:
            raise NotFound("Person not found.")
        return h

    def set_telegram_user(self, owner: Principal, telegram_user_id: str | None) -> None:
        self.db.run("UPDATE humans SET telegram_user_id=? WHERE id=?", (telegram_user_id, owner.id))
        self.audit.record(owner, "human.telegram_linked", target_type="human", target_id=owner.id,
                          telegram_user_id=telegram_user_id)

    def login(self, email: str, password: str) -> str:
        h = self.db.one("SELECT * FROM humans WHERE email=?", (email.lower(),))
        if not h or not verify_password(password, h["password_hash"]):
            self.audit.record(SYSTEM, "human.login", outcome="denied", email=email.lower())
            raise AuthError("Email or password is incorrect.")
        token = new_token("ncs")
        with self.db.tx() as c:
            c.execute("INSERT INTO human_sessions VALUES (?,?,?,?,NULL)",
                      (sha256(token), h["id"], iso(), in_future(hours=12)))
            self.audit.record(Principal("human", h["id"], h["id"], h["display_name"]), "human.login",
                              target_type="human", target_id=h["id"])
        return token

    def logout(self, token: str) -> None:
        self.db.run("UPDATE human_sessions SET revoked_at=? WHERE token_hash=?", (iso(), sha256(token)))

    def human_from_session(self, token: str | None) -> Principal:
        if not token:
            raise AuthError("Please sign in.")
        s = self.db.one("SELECT * FROM human_sessions WHERE token_hash=?", (sha256(token),))
        if not s or s["revoked_at"] or s["expires_at"] <= iso():
            raise AuthError("Your session has ended. Please sign in again.")
        h = self.get_human(s["human_id"])
        return Principal("human", h["id"], h["id"], h["display_name"])

    def human_from_telegram(self, telegram_user_id: str) -> Principal | None:
        h = self.db.one("SELECT * FROM humans WHERE telegram_user_id=?", (str(telegram_user_id),))
        return Principal("human", h["id"], h["id"], h["display_name"], extra={"via": "telegram"}) if h else None

    # ---------------------------------------------------------------- agents
    def create_agent(self, owner: Principal, display_name: str, provider: str, connection_type: str,
                     description: str = "") -> dict:
        self._require_human(owner)
        if connection_type not in CONNECTION_TYPES:
            raise CommonsError(f"Unknown connection type. Use one of: {', '.join(CONNECTION_TYPES)}.")
        name = display_name.strip()
        if not name or len(name) > 60:
            raise CommonsError("Agent name must be 1 to 60 characters.")
        # Artificial identity: an agent may not carry a human member's name.
        for h in self.db.all("SELECT display_name FROM humans"):
            if h["display_name"].strip().lower() == name.lower():
                raise Forbidden("An AI agent cannot use a person's name. Choose a distinct assistant name.")
        aid = new_id("agt")
        simulated = 1 if connection_type == "simulated" else 0
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO agents (id, owner_id, display_name, provider, connection_type, is_simulated, description, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (aid, owner.id, name, provider, connection_type, simulated, description, iso()),
            )
            self.audit.record(owner, "agent.registered", target_type="agent", target_id=aid,
                              display_name=name, provider=provider, connection_type=connection_type)
        return self.get_agent(aid)

    def get_agent(self, aid: str) -> dict:
        a = self.db.one(
            "SELECT a.*, h.display_name AS owner_name FROM agents a JOIN humans h ON h.id=a.owner_id WHERE a.id=?", (aid,))
        if not a:
            raise NotFound("Agent not found.")
        a["is_simulated"] = bool(a["is_simulated"])
        a["label"] = agent_label(a)
        return a

    def list_agents(self, owner: Principal) -> list[dict]:
        self._require_human(owner)
        out = []
        for r in self.db.all("SELECT id FROM agents WHERE owner_id=? ORDER BY created_at", (owner.id,)):
            a = self.get_agent(r["id"])
            a["credentials"] = self.db.all(
                "SELECT id, kind, label, oauth_client_id, created_at, expires_at, revoked_at, last_used_at"
                " FROM credentials WHERE agent_id=? AND kind!='oauth_refresh' ORDER BY created_at DESC", (a["id"],))
            out.append(a)
        return out

    def set_agent_status(self, owner: Principal, agent_id: str, status: str) -> dict:
        self._owned_agent(owner, agent_id)
        if status not in ("active", "suspended"):
            raise CommonsError("Status must be 'active' or 'suspended'.")
        self.db.run("UPDATE agents SET status=? WHERE id=?", (status, agent_id))
        self.audit.record(owner, f"agent.{status}", target_type="agent", target_id=agent_id)
        return self.get_agent(agent_id)

    def issue_token(self, owner: Principal, agent_id: str, label: str = "api token", ttl_days: int = 30) -> dict:
        self._owned_agent(owner, agent_id)
        token = new_token("nca")
        cid = new_id("cred")
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO credentials (id, token_hash, agent_id, kind, label, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (cid, sha256(token), agent_id, "static", label, iso(), in_future(days=ttl_days)),
            )
            self.audit.record(owner, "credential.issued", target_type="agent", target_id=agent_id,
                              credential_id=cid, label=label, ttl_days=ttl_days)
        return {"credential_id": cid, "token": token, "shown_once": True}

    def revoke_credential(self, owner: Principal, credential_id: str) -> None:
        cred = self.db.one("SELECT * FROM credentials WHERE id=?", (credential_id,))
        if not cred:
            raise NotFound("Credential not found.")
        self._owned_agent(owner, cred["agent_id"])
        with self.db.tx() as c:
            c.execute("UPDATE credentials SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (iso(), credential_id))
            # Revoking an OAuth access token also revokes its refresh token(s) for that client.
            if cred["kind"] == "oauth_access" and cred["oauth_client_id"]:
                c.execute("UPDATE credentials SET revoked_at=? WHERE agent_id=? AND oauth_client_id=? AND revoked_at IS NULL",
                          (iso(), cred["agent_id"], cred["oauth_client_id"]))
            self.audit.record(owner, "credential.revoked", target_type="agent", target_id=cred["agent_id"],
                              credential_id=credential_id)

    def store_credential(self, agent_id: str, token: str, kind: str, oauth_client_id: str | None,
                         scopes: list[str], expires_at: str | None, label: str) -> str:
        cid = new_id("cred")
        self.db.run(
            "INSERT INTO credentials (id, token_hash, agent_id, kind, oauth_client_id, scopes, label, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, sha256(token), agent_id, kind, oauth_client_id, jdump(scopes), label, iso(), expires_at),
        )
        return cid

    def agent_from_token(self, token: str | None, kinds: tuple[str, ...] = ("static", "oauth_access")) -> Principal:
        if not token:
            raise AuthError("Missing agent credential.")
        cred = self.db.one("SELECT * FROM credentials WHERE token_hash=?", (sha256(token),))
        if not cred or cred["kind"] not in kinds:
            raise AuthError("Unknown agent credential.")
        if cred["revoked_at"]:
            self.audit.record(SYSTEM, "credential.rejected", outcome="denied", target_type="agent",
                              target_id=cred["agent_id"], credential_id=cred["id"], why="revoked")
            raise AuthError("This credential has been revoked.")
        if cred["expires_at"] and cred["expires_at"] <= iso():
            raise AuthError("This credential has expired.")
        a = self.get_agent(cred["agent_id"])
        if a["status"] != "active":
            raise AuthError("This agent is suspended by its owner.")
        self.db.run("UPDATE credentials SET last_used_at=? WHERE id=?", (iso(), cred["id"]))
        return Principal("agent", a["id"], a["owner_id"], a["display_name"], a["is_simulated"],
                         cred["id"], cred["kind"], cred["oauth_client_id"])

    # ================================================================ rooms
    def create_room(self, owner: Principal, name: str, purpose: str, telegram_chat_id: str | None = None,
                    default_max_turns: int = 6, retention_days: int = 90, kind: str = "private") -> dict:
        self._require_human(owner)
        rid = new_id("room")
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO rooms (id, name, purpose, kind, owner_id, telegram_chat_id, default_max_turns, retention_days, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, name, purpose, kind, owner.id, telegram_chat_id,
                 min(default_max_turns, MAX_TURNS_CEILING), retention_days, iso()),
            )
            c.execute("INSERT INTO room_members VALUES (?,?,?,?,?,NULL)", (rid, "human", owner.id, "owner", iso()))
            self.audit.record(owner, "room.created", target_type="room", target_id=rid, name=name)
        return self.get_room(rid)

    def update_room(self, owner: Principal, room_id: str, **fields) -> dict:
        self._owned_room(owner, room_id)
        allowed = {"name", "purpose", "telegram_chat_id", "default_max_turns", "retention_days", "status"}
        for k, v in fields.items():
            if k in allowed and v is not None:
                if k == "default_max_turns":
                    v = min(int(v), MAX_TURNS_CEILING)
                self.db.run(f"UPDATE rooms SET {k}=? WHERE id=?", (v, room_id))
        self.audit.record(owner, "room.updated", target_type="room", target_id=room_id,
                          fields={k: v for k, v in fields.items() if k in allowed})
        return self.get_room(room_id)

    def get_room(self, room_id: str) -> dict:
        r = self.db.one("SELECT * FROM rooms WHERE id=?", (room_id,))
        if not r:
            raise NotFound("Room not found.")
        r["members"] = self._members(room_id)
        return r

    def _members(self, room_id: str) -> list[dict]:
        out = []
        for m in self.db.all("SELECT * FROM room_members WHERE room_id=? AND removed_at IS NULL", (room_id,)):
            if m["member_type"] == "human":
                h = self.get_human(m["member_id"])
                out.append({"type": "human", "id": h["id"], "name": h["display_name"], "role": m["role"]})
            else:
                a = self.get_agent(m["member_id"])
                out.append({"type": "agent", "id": a["id"], "name": a["display_name"], "label": a["label"],
                            "provider": a["provider"], "represents": a["owner_name"],
                            "connection_type": a["connection_type"], "simulated": a["is_simulated"],
                            "status": a["status"], "role": m["role"]})
        return out

    def add_member(self, owner: Principal, room_id: str, member_type: str, member_id: str) -> dict:
        self._owned_room(owner, room_id)
        if member_type == "agent":
            self.get_agent(member_id)
        else:
            self.get_human(member_id)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO room_members VALUES (?,?,?,?,?,NULL) ON CONFLICT(room_id, member_type, member_id)"
                " DO UPDATE SET removed_at=NULL", (room_id, member_type, member_id, "member", iso()))
            self.audit.record(owner, "room.member_added", target_type="room", target_id=room_id,
                              member_type=member_type, member_id=member_id)
        return self.get_room(room_id)

    def remove_member(self, owner: Principal, room_id: str, member_type: str, member_id: str) -> dict:
        self._owned_room(owner, room_id)
        self.db.run("UPDATE room_members SET removed_at=? WHERE room_id=? AND member_type=? AND member_id=?",
                    (iso(), room_id, member_type, member_id))
        self.audit.record(owner, "room.member_removed", target_type="room", target_id=room_id,
                          member_type=member_type, member_id=member_id)
        return self.get_room(room_id)

    def list_rooms_for_human(self, owner: Principal) -> list[dict]:
        rows = self.db.all(
            "SELECT r.id FROM rooms r JOIN room_members m ON m.room_id=r.id WHERE m.member_type='human'"
            " AND m.member_id=? AND m.removed_at IS NULL ORDER BY r.created_at", (owner.id,))
        return [self.get_room(r["id"]) for r in rows]

    def list_rooms_for_agent(self, p: Principal) -> list[dict]:
        self._agent_gate(p)
        rows = self.db.all(
            "SELECT room_id FROM room_members WHERE member_type='agent' AND member_id=? AND removed_at IS NULL", (p.id,))
        out = []
        for r in rows:
            if self.perms.authorize(p, "rooms.read", room_id=r["room_id"]).allowed:
                room = self.get_room(r["room_id"])
                out.append({
                    "room_id": room["id"], "name": room["name"], "purpose": room["purpose"],
                    "members": room["members"],
                    "my_permissions": self.describe_permissions(p.id, room["id"]),
                })
        self.audit.record(p, "rooms.listed", count=len(out))
        return out

    # ================================================================ grants
    def create_grant(self, owner: Principal, agent_id: str, room_id: str | None = None,
                     actions: list[str] | None = None, level: int | None = None,
                     info_categories: list[str] | None = None, allowed_targets: list[str] | None = None,
                     max_amount: float = 0.0, max_per_hour: int = 30, max_turns: int | None = None,
                     requires_approval: bool = False, expires_in_hours: float | None = 24 * 30,
                     note: str = "") -> dict:
        self._owned_agent(owner, agent_id)
        if room_id:
            self._owned_room(owner, room_id)
        if actions is None:
            if level is None:
                raise CommonsError("Give either a list of actions or an autonomy level (0-4).")
            actions = actions_for_level(int(level))
        bad = [a for a in actions if a not in ACTIONS]
        if bad:
            raise CommonsError(f"Unknown actions: {bad}")
        cats = info_categories or ["general"]
        if any(c not in INFO_CATEGORIES for c in cats):
            raise CommonsError(f"Unknown information category. Use: {INFO_CATEGORIES}")
        if max_turns is not None:
            max_turns = min(max_turns, MAX_TURNS_CEILING)
        gid = new_id("grant")
        expires_at = in_future(hours=expires_in_hours) if expires_in_hours else None
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO grants (id, granted_by, agent_id, room_id, actions, info_categories, allowed_targets,"
                " max_amount, max_per_hour, max_turns, requires_approval, note, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, owner.id, agent_id, room_id, jdump(sorted(actions)), jdump(cats),
                 jdump(allowed_targets) if allowed_targets is not None else None, max_amount, max_per_hour,
                 max_turns, 1 if requires_approval else 0, note, iso(), expires_at),
            )
            self.audit.record(owner, "grant.created", target_type="grant", target_id=gid, agent_id=agent_id,
                              room_id=room_id, actions=sorted(actions), info_categories=cats,
                              max_amount=max_amount, expires_at=expires_at)
        return self.get_grant(gid)

    def get_grant(self, gid: str) -> dict:
        g = self.db.one("SELECT * FROM grants WHERE id=?", (gid,))
        if not g:
            raise NotFound("Permission not found.")
        for k in ("actions", "info_categories", "allowed_targets"):
            g[k] = jload(g[k], None)
        return g

    def list_grants(self, owner: Principal) -> list[dict]:
        self._require_human(owner)
        rows = self.db.all("SELECT id FROM grants WHERE granted_by=? ORDER BY created_at DESC", (owner.id,))
        return [self.get_grant(r["id"]) for r in rows]

    def revoke_grant(self, owner: Principal, grant_id: str) -> dict:
        g = self.get_grant(grant_id)
        if g["granted_by"] != owner.id:
            raise Forbidden("You can only revoke permissions you granted.")
        self.db.run("UPDATE grants SET revoked_at=?, revoked_by=? WHERE id=? AND revoked_at IS NULL",
                    (iso(), owner.id, grant_id))
        self.audit.record(owner, "grant.revoked", target_type="grant", target_id=grant_id, agent_id=g["agent_id"])
        return self.get_grant(grant_id)

    def describe_permissions(self, agent_id: str, room_id: str | None) -> dict:
        grants = self.perms.active_grants(agent_id, room_id)
        acts = sorted({a for g in grants for a in g["actions"]})
        cats = sorted({c for g in grants for c in g["info_categories"]})
        return {"actions": acts, "info_categories": cats,
                "note": "Anything outside these needs your owner's approval or is refused."}

    # ================================================================ tasks
    def submit_task(self, p: Principal, room_id: str, assignee_agent_id: str, title: str, brief: str,
                    info_category: str = "general", amount: float = 0.0, deadline_hours: float | None = 72,
                    idempotency_key: str | None = None, parent_task_id: str | None = None,
                    depends_on: list[str] | None = None, max_turns: int | None = None) -> dict:
        self._agent_gate(p)
        if idempotency_key:
            existing = self.db.one("SELECT id FROM tasks WHERE created_by_agent=? AND idempotency_key=?",
                                   (p.id, idempotency_key))
            if existing:
                self.audit.record(p, "task.submit_duplicate", target_type="task", target_id=existing["id"],
                                  idempotency_key=idempotency_key)
                return self._task_view(existing["id"], duplicate=True)
        check_size(title, brief)
        if assignee_agent_id == p.id:
            raise Forbidden("An agent cannot assign a task to itself.")
        assignee = self.get_agent(assignee_agent_id)
        if assignee["status"] != "active":
            raise Forbidden("That agent is suspended.")
        depth = 0
        action = "task.submit"
        if parent_task_id:
            parent = self._task_row(parent_task_id)
            if p.id not in (parent["assigned_to_agent"], parent["created_by_agent"]):
                raise Forbidden("You can only create sub-tasks of tasks you are part of.")
            depth = parent["depth"] + 1
            if depth > MAX_TASK_DEPTH:
                raise LimitExceeded(f"Sub-tasks can only nest {MAX_TASK_DEPTH} levels deep.")
            action = "task.delegate"
        open_n = self.db.one(
            f"SELECT COUNT(*) AS n FROM tasks WHERE created_by_agent=? AND state NOT IN ({_qs(sm.TERMINAL)})",
            (p.id, *sm.TERMINAL))["n"]
        if open_n >= MAX_OPEN_TASKS_PER_AGENT:
            raise LimitExceeded("This agent has too many open tasks. Finish or cancel some first.")
        h = sha256(f"{room_id}|{assignee_agent_id}|{title}|{brief}")
        cutoff = iso(now() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS))
        dup = self.db.one(
            "SELECT m.task_id FROM messages m WHERE m.sender_id=? AND m.content_hash=? AND m.kind='task_request'"
            " AND m.created_at>=?", (p.id, h, cutoff))
        if dup:
            raise Conflict("An identical task was submitted moments ago.", existing_task_id=dup["task_id"])

        decision = self.perms.authorize(p, action, room_id=room_id, info_category=info_category,
                                        amount=amount, target_agent=assignee_agent_id)
        if decision.effect == "deny":
            self.audit.record(p, "task.submit", outcome="denied", target_type="room", target_id=room_id,
                              reason=decision.reason)
            raise Forbidden(decision.reason, action=action)
        if decision.grant:
            self.safety.hit(p.id, f"grant:{decision.grant['id']}:{action}", decision.grant["max_per_hour"], 3600)
        room = self.get_room(room_id)
        turns = min(x for x in [max_turns or MAX_TURNS_CEILING, room["default_max_turns"],
                                (decision.grant or {}).get("max_turns") or MAX_TURNS_CEILING, MAX_TURNS_CEILING])
        flags = screen_content(title + "\n" + brief)
        tid = new_id("task")
        t = iso()
        deadline = in_future(hours=deadline_hours) if deadline_hours else None
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO tasks (id, room_id, parent_task_id, depth, created_by_agent, assigned_to_agent, owner_id,"
                " title, brief, info_category, amount, state, deadline, max_turns, grant_id, idempotency_key,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, room_id, parent_task_id, depth, p.id, assignee_agent_id, room["owner_id"], title, brief,
                 info_category, amount, sm.CREATED, deadline, turns,
                 decision.grant["id"] if decision.grant and decision.allowed else None, idempotency_key, t, t),
            )
            c.execute("INSERT INTO task_transitions (task_id, from_state, to_state, actor_type, actor_id, reason, at)"
                      " VALUES (?,?,?,?,?,?,?)", (tid, None, sm.CREATED, p.type, p.id, "submitted", t))
            for dep in depends_on or []:
                self._task_row(dep)
                c.execute("INSERT INTO task_dependencies VALUES (?,?)", (tid, dep))
            msg_id = self._insert_message(room_id, tid, p, assignee_agent_id, "task_request",
                                          f"{title}\n\n{brief}", idempotency_key=None, flags=flags,
                                          content_hash=h)
            self.audit.record(p, "task.submitted", target_type="task", target_id=tid, room_id=room_id,
                              assignee=assignee_agent_id, decision=decision.effect, reason=decision.reason,
                              injection_flags=flags)
            if decision.allowed:
                self._transition(tid, sm.AUTHORIZED, SYSTEM, "covered by standing delegation " + decision.grant["id"])
                self._queue_and_deliver(tid, msg_id)
            else:
                self._transition(tid, sm.AWAITING_AUTHORIZATION, SYSTEM, decision.reason)
                self._create_approval(tid, p.id, room["owner_id"], "task_authorization", decision.reason)
        return self._task_view(tid)

    def _queue_and_deliver(self, tid: str, msg_id: str) -> None:
        task = self._task_row(tid)
        unmet = self.db.all(
            f"SELECT d.depends_on FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on"
            f" WHERE d.task_id=? AND t.state!='{sm.ACCEPTED}'", (tid,))
        self._transition(tid, sm.QUEUED, SYSTEM, "lined up for delivery")
        if unmet:
            self.audit.record(SYSTEM, "task.waiting_dependencies", target_type="task", target_id=tid,
                              waiting_on=[u["depends_on"] for u in unmet])
            return
        self._deliver(msg_id, task["assigned_to_agent"])
        self._transition(tid, sm.DELIVERED, SYSTEM, "placed in the assignee's Commons inbox")

    def _release_dependents(self, tid: str) -> None:
        for d in self.db.all("SELECT task_id FROM task_dependencies WHERE depends_on=?", (tid,)):
            t = self._task_row(d["task_id"])
            if t["state"] != sm.QUEUED:
                continue
            unmet = self.db.one(
                f"SELECT COUNT(*) AS n FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on"
                f" WHERE d.task_id=? AND t.state!='{sm.ACCEPTED}'", (t["id"],))["n"]
            if unmet == 0:
                m = self.db.one("SELECT id FROM messages WHERE task_id=? AND kind='task_request'", (t["id"],))
                self._deliver(m["id"], t["assigned_to_agent"])
                self._transition(t["id"], sm.DELIVERED, SYSTEM, "dependencies accepted")

    def read_inbox(self, p: Principal, limit: int = 20, mark_retrieved: bool = True) -> dict:
        self._agent_gate(p)
        limit = max(1, min(limit, 50))
        rows = self.db.all(
            "SELECT d.id AS delivery_id, m.* FROM deliveries d JOIN messages m ON m.id=d.message_id"
            " WHERE d.channel='inbox' AND d.recipient=? AND d.retrieved_at IS NULL AND d.status='delivered'"
            " ORDER BY m.created_at LIMIT ?", (p.id, limit))
        items, skipped = [], 0
        for r in rows:
            # Authorization is re-checked at read time: revocation takes effect immediately.
            if not self.perms.authorize(p, "inbox.read", room_id=r["room_id"]).allowed:
                skipped += 1
                continue
            items.append(self._message_view(r))
            if mark_retrieved:
                with self.db.tx():
                    self.db.run("UPDATE deliveries SET retrieved_at=? WHERE id=?", (iso(), r["delivery_id"]))
                    if r["kind"] == "task_request" and r["task_id"]:
                        t = self._task_row(r["task_id"])
                        if t["state"] == sm.DELIVERED:
                            self._transition(t["id"], sm.RETRIEVED, p, "assignee fetched the task from its inbox")
                            self._mirror(t["room_id"], t["id"],
                                         f"{self._agent_tag(p.id)} opened the task \"{t['title']}\".")
        self.audit.record(p, "inbox.read", count=len(items), withheld=skipped, marked_retrieved=mark_retrieved)
        return {"notice": UNTRUSTED_NOTICE, "items": items, "withheld_due_to_permissions": skipped,
                "more_available": len(rows) == limit}

    def respond(self, p: Principal, task_id: str, body: str, final: bool = True,
                idempotency_key: str | None = None) -> dict:
        """Assignee: contributes work. Creator: posts a synthesis/follow-up."""
        self._agent_gate(p)
        if idempotency_key:
            prev = self.db.one("SELECT id FROM messages WHERE sender_id=? AND idempotency_key=?", (p.id, idempotency_key))
            if prev:
                return {"duplicate": True, "message_id": prev["id"], "task": self._task_view(task_id)}
        check_size(None, body)
        t = self._task_row(task_id)
        if p.id not in (t["assigned_to_agent"], t["created_by_agent"]):
            raise Forbidden("Only the agents on this task can post to it.")
        if sm.is_terminal(t["state"]):
            raise Conflict(f"This task is already {t['state']}.", state=t["state"])
        if t["state"] in (sm.CREATED, sm.AWAITING_AUTHORIZATION, sm.AUTHORIZED, sm.QUEUED):
            raise Conflict("This task has not been delivered yet.", state=t["state"])
        is_assignee = p.id == t["assigned_to_agent"]
        if is_assignee and t["state"] == sm.DELIVERED:
            raise Conflict("Read the task from your inbox before responding.", state=t["state"])
        if t["state"] == sm.AWAITING_HUMAN_INPUT:
            raise Conflict("This task is paused until the owner responds.", state=t["state"])
        action = "task.respond" if is_assignee else "message.send"
        decision = self.perms.authorize(p, action, room_id=t["room_id"], info_category=t["info_category"],
                                        target_agent=t["created_by_agent"] if is_assignee else t["assigned_to_agent"])
        if decision.effect == "deny":
            self.audit.record(p, action, outcome="denied", target_type="task", target_id=task_id, reason=decision.reason)
            raise Forbidden(decision.reason)
        if t["turns_used"] >= t["max_turns"]:
            with self.db.tx():
                self._transition(task_id, sm.AWAITING_HUMAN_INPUT, SYSTEM, "turn limit reached")
                self._create_approval(task_id, p.id, t["owner_id"], "turn_limit",
                                      f"The assistants used all {t['max_turns']} allowed turns.")
            raise LimitExceeded("Turn limit reached. The owner has been asked whether to allow more turns.")
        flags = screen_content(body)
        recipient = t["created_by_agent"] if is_assignee else t["assigned_to_agent"]
        kind = ("response" if final else "progress") if is_assignee else ("synthesis" if final else "follow_up")
        with self.db.tx() as c:
            c.execute("UPDATE tasks SET turns_used=turns_used+1, updated_at=? WHERE id=?", (iso(), task_id))
            msg_id = self._insert_message(t["room_id"], task_id, p, recipient, kind, body,
                                          idempotency_key=idempotency_key, flags=flags)
            self.audit.record(p, f"task.{kind}", target_type="task", target_id=task_id, message_id=msg_id,
                              decision=decision.effect, injection_flags=flags)
            if decision.effect == "needs_approval":
                self._transition(task_id, sm.AWAITING_HUMAN_INPUT, SYSTEM, decision.reason)
                self._create_approval(task_id, p.id, t["owner_id"], "release_message",
                                      f"Release a {kind} message: {decision.reason}", message_id=msg_id)
                return {"held_for_approval": True, "message_id": msg_id, "task": self._task_view(task_id)}
            self._post_message_state(task_id, p, kind, final)
            self._deliver(msg_id, recipient)
        return {"message_id": msg_id, "task": self._task_view(task_id)}

    def _post_message_state(self, task_id: str, p: Principal, kind: str, final: bool) -> None:
        t = self._task_row(task_id)
        if kind == "response":
            self._transition(task_id, sm.RESULT_READY, p, "assignee submitted a result")
            self.db.run("UPDATE tasks SET result=? WHERE id=?",
                        (self.db.one("SELECT body FROM messages WHERE task_id=? AND kind='response'"
                                     " ORDER BY created_at DESC LIMIT 1", (task_id,))["body"], task_id))
        elif kind == "progress" and t["state"] == sm.RETRIEVED:
            self._transition(task_id, sm.IN_PROGRESS, p, "assignee posted progress")
        elif kind == "synthesis":
            self.db.run("UPDATE tasks SET result=? WHERE id=?",
                        (self.db.one("SELECT body FROM messages WHERE task_id=? AND kind='synthesis'"
                                     " ORDER BY created_at DESC LIMIT 1", (task_id,))["body"], task_id))
            if t["state"] in (sm.RETRIEVED, sm.IN_PROGRESS):
                self._transition(task_id, sm.RESULT_READY, p, "creator posted a synthesis")

    def get_task_for_agent(self, p: Principal, task_id: str) -> dict:
        self._agent_gate(p)
        t = self._task_row(task_id)
        if p.id not in (t["assigned_to_agent"], t["created_by_agent"]):
            raise Forbidden("You are not part of this task.")
        self.perms.require(p, "task.status", room_id=t["room_id"])
        return self._task_view(task_id, include_messages=True)

    def request_approval(self, p: Principal, task_id: str, reason: str) -> dict:
        self._agent_gate(p)
        check_size(None, reason)
        t = self._task_row(task_id)
        if p.id not in (t["assigned_to_agent"], t["created_by_agent"]):
            raise Forbidden("You are not part of this task.")
        self.perms.require(p, "approval.request", room_id=t["room_id"])
        self.safety.hit(p.id, "approval.request", 10, 3600)
        with self.db.tx():
            if t["state"] in (sm.RETRIEVED, sm.IN_PROGRESS):
                self._transition(task_id, sm.AWAITING_HUMAN_INPUT, p, "agent asked the owner for input")
            elif t["state"] != sm.AWAITING_HUMAN_INPUT:
                raise Conflict(f"Approval can be requested only while work is under way (state: {t['state']}).")
            aid = self._create_approval(task_id, p.id, t["owner_id"], "agent_request", reason)
        return {"approval_id": aid, "status": "pending", "task": self._task_view(task_id)}

    def cancel_task(self, actor: Principal, task_id: str, reason: str = "") -> dict:
        t = self._task_row(task_id)
        if actor.type == "agent":
            self._agent_gate(actor)
            if actor.id != t["created_by_agent"]:
                raise Forbidden("Only the agent that created a task, or the owner, can cancel it.")
            self.perms.require(actor, "task.cancel", room_id=t["room_id"])
        else:
            self._require_owner_of_task(actor, t)
        if sm.is_terminal(t["state"]):
            raise Conflict(f"This task is already {t['state']}.", state=t["state"])
        with self.db.tx():
            self._transition(task_id, sm.CANCELLED, actor, reason or "cancelled")
            self.db.run("UPDATE approvals SET status='expired', decided_at=? WHERE task_id=? AND status='pending'",
                        (iso(), task_id))
            for aid in {t["assigned_to_agent"], t["created_by_agent"]} - {actor.id}:
                mid = self._insert_message(t["room_id"], task_id, SYSTEM, aid, "notice",
                                           f"Task \"{t['title']}\" was cancelled by {self._actor_tag(actor)}."
                                           + (f" Reason: {reason}" if reason else ""))
                if self._delivered_ever(task_id, aid):
                    self._deliver(mid, aid)
        return self._task_view(task_id)

    # ---------------------------------------------------------- human decisions
    def decide_approval(self, owner: Principal, approval_id: str, approve: bool, note: str = "",
                        extra_turns: int = 4) -> dict:
        a = self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not a:
            raise NotFound("Approval request not found.")
        if a["owner_id"] != owner.id:
            raise Forbidden("Only the owner can decide this request.")
        if a["status"] != "pending":
            raise Conflict(f"This request was already {a['status']}.")
        if a["expires_at"] <= iso():
            self.db.run("UPDATE approvals SET status='expired' WHERE id=?", (approval_id,))
            raise Conflict("This request expired.")
        status = "approved" if approve else "denied"
        t = self._task_row(a["task_id"]) if a["task_id"] else None
        with self.db.tx():
            self.db.run("UPDATE approvals SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                        (status, owner.id, iso(), note, approval_id))
            self.audit.record(owner, f"approval.{status}", target_type="approval", target_id=approval_id,
                              task_id=a["task_id"], kind=a["kind"], note=note)
            kind = a["kind"]
            if kind == "task_authorization":
                if approve:
                    self._transition(t["id"], sm.AUTHORIZED, owner, "approved by owner")
                    m = self.db.one("SELECT id FROM messages WHERE task_id=? AND kind='task_request'", (t["id"],))
                    self._queue_and_deliver(t["id"], m["id"])
                else:
                    self._transition(t["id"], sm.CANCELLED, owner, "owner declined: " + (note or "no reason given"))
            elif kind == "release_message":
                detail = jload(a["reason"].split("||", 1)[1]) if "||" in a["reason"] else {}
                mid = detail.get("message_id")
                msg = self.db.one("SELECT * FROM messages WHERE id=?", (mid,)) if mid else None
                self._transition(t["id"], sm.IN_PROGRESS, owner, "owner decided on held message")
                if approve and msg:
                    sender = Principal("agent", msg["sender_id"])
                    self._post_message_state(t["id"], sender, msg["kind"], True)
                    self._deliver(mid, msg["recipient_agent_id"])
                elif msg:
                    self._notify_agent(t, msg["sender_id"], f"Your owner did not release your message. {note}".strip())
            elif kind in ("agent_request", "turn_limit"):
                if kind == "turn_limit" and approve:
                    self.db.run("UPDATE tasks SET max_turns=MIN(max_turns+?, ?) WHERE id=?",
                                (extra_turns, MAX_TURNS_CEILING, t["id"]))
                if approve or kind == "agent_request":
                    self._transition(t["id"], sm.IN_PROGRESS, owner, f"owner {status} the request")
                    self._notify_agent(t, a["requested_by_agent"],
                                       f"Your owner {status} your request." + (f" Note from owner: {note}" if note else ""))
                else:
                    self._transition(t["id"], sm.CANCELLED, owner, "owner stopped the task at the turn limit")
        return self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))

    def accept_task(self, owner: Principal, task_id: str, note: str = "") -> dict:
        t = self._task_row(task_id)
        self._require_owner_of_task(owner, t)
        with self.db.tx():
            self._transition(task_id, sm.ACCEPTED, owner, note or "accepted by owner")
            for aid in (t["assigned_to_agent"], t["created_by_agent"]):
                self._notify_agent(t, aid, f"Your owner accepted the result of \"{t['title']}\".")
            self._release_dependents(task_id)
        return self._task_view(task_id)

    def revise_task(self, owner: Principal, task_id: str, instructions: str) -> dict:
        check_size(None, instructions)
        t = self._task_row(task_id)
        self._require_owner_of_task(owner, t)
        with self.db.tx():
            self._transition(task_id, sm.IN_PROGRESS, owner, "owner requested a revision")
            for aid in (t["assigned_to_agent"], t["created_by_agent"]):
                mid = self._insert_message(t["room_id"], task_id, owner, aid, "revision_request", instructions)
                self._deliver(mid, aid)
        return self._task_view(task_id)

    def human_note(self, owner: Principal, room_id: str, text: str) -> dict:
        """A human message in a room (e.g. from Telegram). Recorded; never creates tasks or permissions."""
        check_size(None, text)
        if not self.perms.is_member(room_id, "human", owner.id):
            raise Forbidden("You are not a member of this room.")
        mid = self._insert_message(room_id, None, owner, None, "human_note", text)
        self.audit.record(owner, "room.human_note", target_type="room", target_id=room_id, message_id=mid)
        return {"message_id": mid}

    # ================================================================ shared context
    def add_context(self, p: Principal, room_id: str, content: str, info_category: str = "general",
                    task_id: str | None = None, provenance: str = "") -> dict:
        check_size(None, content)
        if p.type == "agent":
            self._agent_gate(p)
            self.perms.require(p, "context.write", room_id=room_id, info_category=info_category)
        else:
            self._owned_room(p, room_id)
        room = self.get_room(room_id)
        cid = new_id("ctx")
        self.db.run(
            "INSERT INTO context_items VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, room_id, task_id, p.type, p.id, info_category, content,
             provenance or f"added by {p.type} {p.display_name or p.id}", iso(),
             in_future(days=room["retention_days"])))
        self.audit.record(p, "context.added", target_type="room", target_id=room_id, context_id=cid,
                          info_category=info_category)
        return {"context_id": cid}

    def list_context(self, p: Principal, room_id: str) -> dict:
        if p.type == "agent":
            self._agent_gate(p)
            self.perms.require(p, "context.read", room_id=room_id)
            cats = set(self.describe_permissions(p.id, room_id)["info_categories"])
        else:
            self._owned_room(p, room_id)
            cats = set(INFO_CATEGORIES)
        rows = self.db.all("SELECT * FROM context_items WHERE room_id=? AND retain_until>? ORDER BY created_at",
                           (room_id, iso()))
        visible = [r for r in rows if r["info_category"] in cats]
        self.audit.record(p, "context.read", target_type="room", target_id=room_id, count=len(visible))
        return {"notice": UNTRUSTED_NOTICE, "items": visible, "withheld": len(rows) - len(visible)}

    # ================================================================ event subscriptions
    def subscribe(self, p: Principal, callback_url: str, event_types: list[str]) -> dict:
        self._agent_gate(p)
        if not callback_url.startswith("https://"):
            raise CommonsError("Callback URLs must use https.")
        sid = new_id("sub")
        secret = new_token("whs")
        self.db.run("INSERT INTO event_subscriptions VALUES (?,?,?,?,?,1,?)",
                    (sid, p.id, callback_url, secret, jdump(event_types), iso()))
        self.audit.record(p, "events.subscribed", target_type="subscription", target_id=sid,
                          callback_url=callback_url, event_types=event_types)
        return {"subscription_id": sid, "signing_secret": secret, "shown_once": True}

    def unsubscribe(self, p: Principal, subscription_id: str) -> None:
        self.db.run("UPDATE event_subscriptions SET active=0 WHERE id=? AND agent_id=?", (subscription_id, p.id))
        self.audit.record(p, "events.unsubscribed", target_type="subscription", target_id=subscription_id)

    # ================================================================ maintenance
    def sweep(self) -> dict:
        """Expire overdue tasks/approvals and purge context past retention. Safe to run repeatedly."""
        t = iso()
        expired_tasks = 0
        for r in self.db.all(
                f"SELECT id, state FROM tasks WHERE deadline IS NOT NULL AND deadline<=? AND state NOT IN ({_qs(sm.TERMINAL)})",
                (t, *sm.TERMINAL)):
            if sm.EXPIRED in sm.TRANSITIONS[r["state"]]:
                self._transition(r["id"], sm.EXPIRED, SYSTEM, "deadline passed")
                expired_tasks += 1
        n_appr = self.db.run("UPDATE approvals SET status='expired' WHERE status='pending' AND expires_at<=?", (t,)).rowcount
        purged = self.db.run("DELETE FROM context_items WHERE retain_until<=?", (t,)).rowcount
        if expired_tasks or n_appr or purged:
            self.audit.record(SYSTEM, "maintenance.sweep", expired_tasks=expired_tasks,
                              expired_approvals=n_appr, purged_context=purged)
        return {"expired_tasks": expired_tasks, "expired_approvals": n_appr, "purged_context": purged}

    def set_emergency_stop(self, owner: Principal, on: bool) -> dict:
        self._require_human(owner)
        self.safety.set_stop(on, owner.id)
        self.audit.record(owner, "emergency_stop." + ("on" if on else "off"))
        return {"emergency_stop": on}

    # ================================================================ pilot evidence
    def pilot_record(self, actor: Principal, step: str, note: str, manual: bool = False, evidence: dict | None = None) -> dict:
        pid = new_id("pilot")
        self.db.run("INSERT INTO pilot_log VALUES (?,?,?,?,?,?,?)",
                    (pid, iso(), step, f"{actor.type}:{actor.display_name or actor.id}", 1 if manual else 0,
                     note, jdump(evidence or {})))
        self.audit.record(actor, "pilot.recorded", target_type="pilot", target_id=pid, step=step, manual=manual)
        return {"id": pid}

    def pilot_list(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM pilot_log ORDER BY at")
        for r in rows:
            r["evidence"] = jload(r["evidence"], {})
            r["manual"] = bool(r["manual"])
        return rows

    # ================================================================ views for humans
    def tasks_for_owner(self, owner: Principal, room_id: str | None = None) -> list[dict]:
        q, params = "SELECT id FROM tasks WHERE owner_id=?", [owner.id]
        if room_id:
            q += " AND room_id=?"
            params.append(room_id)
        return [self._task_view(r["id"]) for r in self.db.all(q + " ORDER BY created_at DESC LIMIT 100", tuple(params))]

    def task_for_owner(self, owner: Principal, task_id: str) -> dict:
        t = self._task_row(task_id)
        self._require_owner_of_task(owner, t)
        v = self._task_view(task_id, include_messages=True)
        v["transitions"] = self.db.all("SELECT * FROM task_transitions WHERE task_id=? ORDER BY id", (task_id,))
        v["deliveries"] = self.db.all(
            "SELECT d.* FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE m.task_id=? ORDER BY d.created_at",
            (task_id,))
        v["approvals"] = self.db.all("SELECT * FROM approvals WHERE task_id=? ORDER BY created_at", (task_id,))
        return v

    def approvals_for_owner(self, owner: Principal, status: str = "pending") -> list[dict]:
        rows = self.db.all("SELECT * FROM approvals WHERE owner_id=? AND status=? ORDER BY created_at DESC",
                           (owner.id, status))
        for r in rows:
            r["reason"] = r["reason"].split("||", 1)[0]
            if r["task_id"]:
                t = self._task_row(r["task_id"])
                r["task_title"] = t["title"]
            if r["requested_by_agent"]:
                r["requested_by"] = self.get_agent(r["requested_by_agent"])["label"]
        return rows

    def room_feed(self, owner: Principal, room_id: str, limit: int = 100) -> list[dict]:
        self._owned_room(owner, room_id)
        rows = self.db.all("SELECT * FROM messages WHERE room_id=? ORDER BY created_at DESC LIMIT ?", (room_id, limit))
        return [self._message_view(r) for r in reversed(rows)]

    # ================================================================ internals
    def _require_human(self, p: Principal) -> None:
        if p.type != "human":
            raise Forbidden("Only a signed-in person can do this.")

    def _owned_agent(self, owner: Principal, agent_id: str) -> dict:
        self._require_human(owner)
        a = self.get_agent(agent_id)
        if a["owner_id"] != owner.id:
            raise Forbidden("You do not own this agent.")
        return a

    def _owned_room(self, owner: Principal, room_id: str) -> dict:
        self._require_human(owner)
        r = self.get_room(room_id)
        if r["owner_id"] != owner.id:
            raise Forbidden("You do not own this room.")
        return r

    def _require_owner_of_task(self, owner: Principal, t: dict) -> None:
        self._require_human(owner)
        if t["owner_id"] != owner.id:
            raise Forbidden("Only the room owner can decide on this task.")

    def _agent_gate(self, p: Principal) -> None:
        if p.type != "agent":
            raise Forbidden("This operation is for agents.")
        self.safety.require_running()
        self.safety.global_hit(p.id)
        a = self.get_agent(p.id)
        if a["status"] != "active":
            raise Forbidden("This agent is suspended by its owner.")

    def _task_row(self, task_id: str) -> dict:
        t = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not t:
            raise NotFound("Task not found.")
        return t

    def _transition(self, task_id: str, to: str, actor: Principal, reason: str) -> None:
        with self.db.tx() as c:
            t = self._task_row(task_id)
            sm.check(t["state"], to)
            cur = c.execute("UPDATE tasks SET state=?, version=version+1, updated_at=? WHERE id=? AND version=?",
                            (to, iso(), task_id, t["version"]))
            if cur.rowcount != 1:
                raise Conflict("The task changed at the same time. Please retry.")
            c.execute("INSERT INTO task_transitions (task_id, from_state, to_state, actor_type, actor_id, reason, at)"
                      " VALUES (?,?,?,?,?,?,?)", (task_id, t["state"], to, actor.type, actor.id, reason, iso()))
            self.audit.record(actor, "task.state_changed", target_type="task", target_id=task_id,
                              from_state=t["state"], to_state=to, reason=reason)
            if to in (sm.AWAITING_AUTHORIZATION, sm.RESULT_READY, sm.ACCEPTED, sm.CANCELLED, sm.EXPIRED,
                      sm.FAILED, sm.AWAITING_HUMAN_INPUT, sm.DELIVERED):
                self._mirror(t["room_id"], task_id,
                             f"Task \"{t['title']}\": {sm.PLAIN[to]}." + (f" ({reason})" if to in (
                                 sm.CANCELLED, sm.AWAITING_HUMAN_INPUT, sm.AWAITING_AUTHORIZATION) else ""))

    def _insert_message(self, room_id: str, task_id: str | None, sender: Principal, recipient: str | None,
                        kind: str, body: str, idempotency_key: str | None = None, flags: list[str] | None = None,
                        content_hash: str | None = None) -> str:
        mid = new_id("msg")
        self.db.run(
            "INSERT INTO messages (id, room_id, task_id, sender_type, sender_id, recipient_agent_id, kind, body,"
            " content_hash, correlation_id, idempotency_key, injection_flags, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, room_id, task_id, sender.type, sender.id, recipient, kind, body,
             content_hash or sha256(body), task_id, idempotency_key, jdump(flags or []), iso()))
        return mid

    def _deliver(self, msg_id: str, recipient_agent: str) -> None:
        """Place a message in an agent inbox and fire the agent's event subscriptions.
        Delivery != retrieval: retrieved_at stays NULL until the agent reads it."""
        did = new_id("dlv")
        self.db.run("INSERT INTO deliveries (id, message_id, channel, recipient, status, attempts, delivered_at, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)", (did, msg_id, "inbox", recipient_agent, "delivered", 1, iso(), iso()))
        m = self.db.one("SELECT * FROM messages WHERE id=?", (msg_id,))
        if not self.db.one("SELECT 1 FROM deliveries WHERE message_id=? AND channel='telegram'", (msg_id,)):
            self._mirror_message(msg_id)  # visible copy for the room; content is mirrored only once delivered
        for s in self.db.all("SELECT * FROM event_subscriptions WHERE agent_id=? AND active=1", (recipient_agent,)):
            if "inbox.new" in jload(s["event_types"], []) or "*" in jload(s["event_types"], []):
                # Event payloads carry IDs only. Content must be fetched over the authenticated channel.
                payload = {"event_id": new_id("evt"), "type": "inbox.new", "agent_id": recipient_agent,
                           "task_id": m["task_id"], "message_id": msg_id, "kind": m["kind"], "at": iso(),
                           "subscription_id": s["id"]}
                wid = new_id("dlv")
                self.db.run("INSERT INTO deliveries (id, message_id, channel, recipient, status, created_at)"
                            " VALUES (?,?,?,?,?,?)", (wid, msg_id, "webhook", s["id"], "pending", iso()))
                self._enqueue("webhook", wid, payload)
        self.audit.record(SYSTEM, "message.delivered_to_inbox", target_type="message", target_id=msg_id,
                          recipient=recipient_agent, task_id=m["task_id"])

    def _delivered_ever(self, task_id: str, agent_id: str) -> bool:
        return bool(self.db.one(
            "SELECT 1 FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE m.task_id=? AND d.recipient=?"
            " AND d.channel='inbox'", (task_id, agent_id)))

    def _notify_agent(self, t: dict, agent_id: str, text: str) -> None:
        mid = self._insert_message(t["room_id"], t["id"], SYSTEM, agent_id, "notice", text)
        self._deliver(mid, agent_id)

    def _mirror_message(self, msg_id: str) -> None:
        m = self.db.one("SELECT * FROM messages WHERE id=?", (msg_id,))
        room = self.db.one("SELECT * FROM rooms WHERE id=?", (m["room_id"],))
        if m["kind"] == "human_note" and m["sender_type"] == "human":
            return  # came from the room itself; mirroring it back would duplicate it
        did = new_id("dlv")
        if not room or not room["telegram_chat_id"]:
            self.db.run("INSERT INTO deliveries (id, message_id, channel, recipient, status, last_error, created_at)"
                        " VALUES (?,?,?,?,?,?,?)", (did, msg_id, "telegram", "-", "skipped",
                                                     "room has no Telegram chat configured", iso()))
            return
        self.db.run("INSERT INTO deliveries (id, message_id, channel, recipient, status, created_at) VALUES (?,?,?,?,?,?)",
                    (did, msg_id, "telegram", room["telegram_chat_id"], "pending", iso()))
        self._enqueue("telegram", did, {"chat_id": room["telegram_chat_id"], "message_id": msg_id})

    def _mirror(self, room_id: str, task_id: str | None, text: str) -> None:
        """Status line for the room's visible surface. Never delivered to agent inboxes."""
        mid = new_id("msg")
        self.db.run(
            "INSERT INTO messages (id, room_id, task_id, sender_type, sender_id, recipient_agent_id, kind, body,"
            " content_hash, correlation_id, injection_flags, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, room_id, task_id, "system", SYSTEM.id, None, "status", text, sha256(text), task_id, "[]", iso()))
        self._mirror_message(mid)

    def _enqueue(self, channel: str, delivery_id: str, payload: dict) -> None:
        self.db.run("INSERT INTO outbox (id, channel, delivery_id, payload, status, next_attempt_at, created_at)"
                    " VALUES (?,?,?,?,?,?,?)", (new_id("out"), channel, delivery_id, jdump(payload), "pending", iso(), iso()))

    def _create_approval(self, task_id: str | None, agent_id: str | None, owner_id: str, kind: str, reason: str,
                         message_id: str | None = None, hours: int = 48) -> str:
        aid = new_id("appr")
        stored_reason = reason + (("||" + jdump({"message_id": message_id})) if message_id else "")
        self.db.run("INSERT INTO approvals (id, task_id, requested_by_agent, owner_id, kind, reason, created_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?,?)", (aid, task_id, agent_id, owner_id, kind, stored_reason, iso(),
                                                  in_future(hours=hours)))
        self.audit.record(SYSTEM, "approval.requested", target_type="approval", target_id=aid, task_id=task_id,
                          kind=kind, reason=reason)
        if task_id:
            t = self._task_row(task_id)
            self._mirror(t["room_id"], task_id,
                         f"Approval needed ({kind.replace('_', ' ')}): {reason}\n"
                         f"Reply /approve {aid} or /deny {aid}")
        return aid

    def _agent_tag(self, agent_id: str) -> str:
        return self.get_agent(agent_id)["label"]

    def _actor_tag(self, p: Principal) -> str:
        if p.type == "agent":
            return self._agent_tag(p.id)
        if p.type == "human":
            return f"{p.display_name} (human owner)"
        return "the Commons system"

    def _message_view(self, r: dict) -> dict:
        if r["sender_type"] == "agent":
            a = self.get_agent(r["sender_id"])
            sender = {"type": "agent", "id": a["id"], "name": a["display_name"], "label": a["label"],
                      "represents": a["owner_name"], "provider": a["provider"], "simulated": a["is_simulated"]}
        elif r["sender_type"] == "human":
            h = self.get_human(r["sender_id"])
            sender = {"type": "human", "id": h["id"], "name": h["display_name"]}
        else:
            sender = {"type": "system", "id": SYSTEM.id, "name": "Commons Core"}
        out = {"message_id": r["id"], "task_id": r["task_id"], "room_id": r["room_id"], "kind": r["kind"],
               "from": sender, "sent_at": r["created_at"], "content": r["body"],
               "screening_flags": jload(r["injection_flags"], [])}
        if r["task_id"] and r["kind"] == "task_request":
            t = self._task_row(r["task_id"])
            out["task"] = {"title": t["title"], "info_category": t["info_category"], "deadline": t["deadline"],
                           "max_turns": t["max_turns"], "turns_used": t["turns_used"]}
        return out

    def _task_view(self, task_id: str, include_messages: bool = False, duplicate: bool = False) -> dict:
        t = self._task_row(task_id)
        v = {k: t[k] for k in ("id", "room_id", "title", "brief", "info_category", "amount", "state", "deadline",
                               "max_turns", "turns_used", "result", "parent_task_id", "created_at", "updated_at")}
        v["state_plain"] = sm.PLAIN[t["state"]]
        v["created_by"] = self._agent_tag(t["created_by_agent"])
        v["created_by_agent_id"] = t["created_by_agent"]
        v["assigned_to"] = self._agent_tag(t["assigned_to_agent"])
        v["assigned_to_agent_id"] = t["assigned_to_agent"]
        v["depends_on"] = [d["depends_on"] for d in self.db.all("SELECT depends_on FROM task_dependencies WHERE task_id=?", (task_id,))]
        if duplicate:
            v["duplicate"] = True
        if include_messages:
            msgs = self.db.all("SELECT * FROM messages WHERE task_id=? AND kind!='status' ORDER BY created_at", (task_id,))
            v["notice"] = UNTRUSTED_NOTICE
            v["messages"] = [self._message_view(m) for m in msgs]
        return v


def agent_label(a: dict) -> str:
    sim = " [SIMULATED]" if a.get("is_simulated") else ""
    return f"🤖 {a['display_name']} (AI, {a['provider']}, for {a['owner_name']}){sim}"


def _qs(items) -> str:
    return ",".join("?" for _ in items)
