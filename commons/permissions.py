"""Permission engine.

Authorization comes only from human-created grants plus backend policy.
Every agent operation calls `authorize()`; there is no code path where message
content, a Telegram message, or a model's output can create or widen a grant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import Database, jload
from .errors import Forbidden
from .util import Principal, iso

# action -> conceptual autonomy level (convenience grouping only)
ACTIONS: dict[str, int] = {
    "rooms.read": 0,
    "context.read": 0,
    "inbox.read": 1,
    "message.send": 1,
    "approval.request": 1,
    "task.status": 1,
    "task.submit": 2,
    "task.respond": 2,
    "task.cancel": 2,
    "context.write": 2,
    "action.execute": 3,       # external service actions (none enabled in the MVP)
    "task.delegate": 4,        # create sub-tasks inside a bounded workflow
}

LEVEL_NAMES = {
    0: "Observe shared information",
    1: "Communicate and exchange information",
    2: "Collaborate, research, create and propose",
    3: "Execute explicitly permitted actions",
    4: "Coordinate bounded multistep workflows",
}

PLAIN_ACTIONS = {
    "rooms.read": "see which rooms it belongs to and who is in them",
    "context.read": "read shared notes in the room",
    "inbox.read": "read messages and tasks sent to it",
    "message.send": "send messages in the room",
    "approval.request": "ask you for approval",
    "task.status": "check the status of tasks it is part of",
    "task.submit": "ask another assistant in the room to do a task",
    "task.respond": "reply to tasks assigned to it",
    "task.cancel": "cancel tasks it started",
    "context.write": "add notes to the room's shared knowledge",
    "action.execute": "take actions in outside services (email, calendar, payments)",
    "task.delegate": "split a task into sub-tasks for other assistants",
}

# Actions that may be escalated to a human instead of being refused outright.
APPROVABLE = {"task.submit", "task.respond", "action.execute", "task.delegate"}

INFO_CATEGORIES = ["general", "project", "schedule", "contact", "financial", "health", "personal_memory"]


def actions_for_level(level: int) -> list[str]:
    return sorted(a for a, lv in ACTIONS.items() if lv <= level)


@dataclass
class Decision:
    effect: str  # 'allow' | 'needs_approval' | 'deny'
    reason: str
    grant: dict[str, Any] | None = None

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


class PermissionEngine:
    def __init__(self, db: Database):
        self.db = db

    def active_grants(self, agent_id: str, room_id: str | None = None) -> list[dict]:
        rows = self.db.all(
            "SELECT * FROM grants WHERE agent_id=? AND revoked_at IS NULL", (agent_id,)
        )
        t = iso()
        out = []
        for g in rows:
            if g["expires_at"] and g["expires_at"] <= t:
                continue
            if room_id is not None and g["room_id"] not in (None, room_id):
                continue
            g["actions"] = jload(g["actions"], [])
            g["info_categories"] = jload(g["info_categories"], [])
            g["allowed_targets"] = jload(g["allowed_targets"], None)
            out.append(g)
        return out

    def is_member(self, room_id: str, member_type: str, member_id: str) -> bool:
        return bool(self.db.one(
            "SELECT 1 FROM room_members WHERE room_id=? AND member_type=? AND member_id=? AND removed_at IS NULL",
            (room_id, member_type, member_id),
        ))

    def authorize(
        self,
        p: Principal,
        action: str,
        room_id: str | None = None,
        info_category: str = "general",
        amount: float = 0.0,
        target_agent: str | None = None,
    ) -> Decision:
        if action not in ACTIONS:
            return Decision("deny", f"Unknown action '{action}'.")
        if p.type != "agent":
            return Decision("deny", "Only agents are evaluated by the agent permission engine.")
        agent = self.db.one("SELECT * FROM agents WHERE id=?", (p.id,))
        if not agent or agent["status"] != "active":
            return Decision("deny", "This agent is suspended or does not exist.")
        if room_id is not None:
            room = self.db.one("SELECT * FROM rooms WHERE id=?", (room_id,))
            if not room or room["status"] != "active":
                return Decision("deny", "Room not found or not active.")
            if not self.is_member(room_id, "agent", p.id):
                return Decision("deny", "This agent is not a member of that room.")
            if target_agent and not self.is_member(room_id, "agent", target_agent):
                return Decision("deny", "The target agent is not a member of that room.")
        if info_category not in INFO_CATEGORIES:
            return Decision("deny", f"Unknown information category '{info_category}'.")

        grants = [g for g in self.active_grants(p.id, room_id) if action in g["actions"]]
        if not grants:
            if action in APPROVABLE:
                return Decision("needs_approval", f"No standing delegation lets this agent {PLAIN_ACTIONS[action]} here.")
            return Decision("deny", f"No active permission for '{action}' in this room.")

        # Prefer a grant that fully covers the request without approval.
        reasons = []
        for g in grants:
            r = []
            if info_category not in g["info_categories"]:
                r.append(f"information category '{info_category}' is not covered")
            if g["allowed_targets"] is not None and target_agent and target_agent not in g["allowed_targets"]:
                r.append("that assistant is not an approved collaborator")
            if amount > (g["max_amount"] or 0):
                r.append(f"amount {amount:.2f} exceeds the limit of {g['max_amount']:.2f}")
            if g["requires_approval"]:
                r.append("this delegation requires approval each time")
            if not r:
                return Decision("allow", "Covered by a standing delegation.", g)
            reasons.append("; ".join(r))
        if action in APPROVABLE:
            return Decision("needs_approval", reasons[0], grants[0])
        return Decision("deny", reasons[0], grants[0])

    def require(self, p: Principal, action: str, **kw) -> Decision:
        d = self.authorize(p, action, **kw)
        if d.effect != "allow":
            raise Forbidden(d.reason, action=action, decision=d.effect)
        return d
