"""Telegram adapter: presentation layer and owner command channel.

Telegram is NOT the dispatcher. Outbound, it mirrors attributed messages. Inbound, it
accepts a small set of commands from the verified owner's Telegram account only, and
passes them to the Core, which applies the same rules as the web UI.

Loop and duplicate safety:
- Updates from any bot (including ours) are ignored.
- update_id is stored; a replayed update is ignored.
- Plain human messages become room notes. They never create tasks or permissions.
"""
from __future__ import annotations

import hmac
import logging

import httpx

from . import statemachine as sm
from .errors import CommonsError
from .util import iso

log = logging.getLogger("commons.telegram")
API = "https://api.telegram.org"

HELP = (
    "Commons commands (owner only):\n"
    "/status <task_id> - plain-language status\n"
    "/approve <approval_id> [note]\n"
    "/deny <approval_id> [note]\n"
    "/accept <task_id>\n"
    "/revise <task_id> <instructions>\n"
    "/cancel <task_id> [reason]\n"
    "/pending - list approvals waiting for you\n"
    "/stop - emergency stop all agent activity\n"
    "/resume - resume after an emergency stop\n"
    "/whoami - show your Telegram user id (for linking)"
)


class TelegramAdapter:
    def __init__(self, bot_token: str | None, webhook_secret: str | None = None, http: httpx.Client | None = None):
        self.token = bot_token
        self.webhook_secret = webhook_secret
        self.http = http or httpx.Client(timeout=15)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, **params) -> dict:
        r = self.http.post(f"{API}/bot{self.token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    def send(self, chat_id: str, text: str) -> dict:
        # Plain text (no parse_mode) so agent-written content cannot inject markup or links styling.
        return self._call("sendMessage", chat_id=chat_id, text=text[:4096], disable_web_page_preview=True)

    def get_updates(self, offset: int | None, timeout: int = 20) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", **params)

    def check_secret(self, header_value: str | None) -> bool:
        if not self.webhook_secret:
            return False
        return hmac.compare_digest(header_value or "", self.webhook_secret)


def handle_update(core, update: dict) -> str | None:
    """Process one Telegram update. Returns reply text (or None). Idempotent per update_id."""
    uid = update.get("update_id")
    if uid is None:
        return None
    try:
        core.db.run("INSERT INTO telegram_updates VALUES (?, ?)", (uid, iso()))
    except Exception:
        return None  # duplicate delivery of the same update
    msg = update.get("message") or {}
    sender = msg.get("from") or {}
    if sender.get("is_bot"):
        return None  # bots (including mirrored agent output) never drive the Commons
    text = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not text:
        return None
    if text.startswith("/whoami"):
        return f"Your Telegram user id is {sender.get('id')}. Link it in the Commons web app under Settings."
    owner = core.human_from_telegram(str(sender.get("id")))
    if owner is None:
        from .util import SYSTEM
        core.audit.record(SYSTEM, "telegram.unverified_sender", outcome="ignored",
                          telegram_user_id=str(sender.get("id")), chat_id=chat_id)
        return None if not text.startswith("/") else "You are not a verified owner in this Commons."
    if not text.startswith("/"):
        room = core.db.one("SELECT id FROM rooms WHERE telegram_chat_id=?", (chat_id,))
        if room:
            core.human_note(owner, room["id"], text)
        return None
    parts = text.split(maxsplit=2)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    rest = parts[2] if len(parts) > 2 else ""
    try:
        if cmd in ("/help", "/start"):
            return HELP
        if cmd == "/pending":
            ap = core.approvals_for_owner(owner)
            if not ap:
                return "Nothing is waiting for you."
            return "\n\n".join(f"{a['id']}: {a.get('task_title', '')}\n{a['reason']}" for a in ap[:10])
        if cmd == "/status":
            t = core.task_for_owner(owner, arg)
            return f"\"{t['title']}\"\n{t['state_plain']}\nTurns used: {t['turns_used']}/{t['max_turns']}"
        if cmd in ("/approve", "/deny"):
            a = core.decide_approval(owner, arg, approve=(cmd == "/approve"), note=rest)
            return f"Recorded: {a['status']}."
        if cmd == "/accept":
            t = core.accept_task(owner, arg, rest)
            return f"Accepted. {t['state_plain']}"
        if cmd == "/revise":
            t = core.revise_task(owner, arg, rest or "Please revise.")
            return f"Sent back for revision. {t['state_plain']}"
        if cmd == "/cancel":
            t = core.cancel_task(owner, arg, rest)
            return f"{sm.PLAIN[t['state']]}."
        if cmd == "/stop":
            core.set_emergency_stop(owner, True)
            return "Emergency stop is ON. All agent activity is paused."
        if cmd == "/resume":
            core.set_emergency_stop(owner, False)
            return "Emergency stop is OFF."
        return "Unknown command. Send /help."
    except CommonsError as e:
        return f"Could not do that: {e.message}"
