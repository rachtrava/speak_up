"""Command line: python -m commons <command>

  create-owner      create the human owner account (prompts for password)
  setup-pilot       register the pilot agents, room and standing delegations
  serve             run the Commons (HTTP + MCP + background dispatcher)
  telegram-poll     receive Telegram commands by long polling (no public URL needed)
  telegram-chat-id  print chat ids seen by the bot (to find your group's id)
  verify-audit      check the audit hash chain
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time

from .config import Settings
from .core import Core
from .db import Database
from .util import Principal


def _core(settings: Settings) -> Core:
    return Core(Database(settings.db_path))


def _owner(core: Core, email: str) -> Principal:
    h = core.db.one("SELECT * FROM humans WHERE email=?", (email.lower(),))
    if not h:
        sys.exit(f"No owner with email {email}. Run create-owner first.")
    return Principal("human", h["id"], h["id"], h["display_name"])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="commons")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-owner")
    c.add_argument("--name", required=True)
    c.add_argument("--email", required=True)
    c.add_argument("--telegram-user-id")
    s = sub.add_parser("setup-pilot")
    s.add_argument("--email", required=True)
    s.add_argument("--telegram-chat-id")
    s.add_argument("--with-simulated", action="store_true", help="also create two clearly labeled simulated agents")
    sub.add_parser("serve")
    sub.add_parser("telegram-poll")
    sub.add_parser("telegram-chat-id")
    sub.add_parser("verify-audit")
    args = ap.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == "create-owner":
        core = _core(settings)
        pw = os.getenv("COMMONS_OWNER_PASSWORD") or getpass.getpass("Choose a password (12+ characters): ")
        h = core.create_human(args.name, args.email, pw, args.telegram_user_id)
        print(json.dumps(h, indent=2))
    elif args.cmd == "setup-pilot":
        core = _core(settings)
        o = _owner(core, args.email)
        out = setup_pilot(core, o, args.telegram_chat_id, args.with_simulated)
        print(json.dumps(out, indent=2))
    elif args.cmd == "serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(settings), host=os.getenv("COMMONS_HOST", "127.0.0.1"), port=settings.port,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    elif args.cmd in ("telegram-poll", "telegram-chat-id"):
        from .telegram import TelegramAdapter, handle_update
        if not settings.telegram_bot_token:
            sys.exit("Set TELEGRAM_BOT_TOKEN first.")
        tg = TelegramAdapter(settings.telegram_bot_token)
        core = _core(settings)
        offset = None
        print("Listening for Telegram updates. Ctrl+C to stop.")
        while True:
            for u in tg.get_updates(offset):
                offset = u["update_id"] + 1
                chat = (u.get("message") or {}).get("chat") or {}
                if args.cmd == "telegram-chat-id":
                    print(f"chat id {chat.get('id')} ({chat.get('type')}: {chat.get('title') or chat.get('username')})")
                    continue
                reply = handle_update(core, u)
                if reply and chat.get("id"):
                    tg.send(str(chat["id"]), reply)
            time.sleep(0.5)
    elif args.cmd == "verify-audit":
        print(json.dumps(_core(settings).audit.verify(), indent=2))


def setup_pilot(core: Core, o: Principal, telegram_chat_id: str | None, with_simulated: bool) -> dict:
    room = core.create_room(o, "Pilot: project brief", "Low-sensitivity collaboration on a selected project brief",
                            telegram_chat_id=telegram_chat_id, default_max_turns=6)
    gpt = core.create_agent(o, "ChatGPT", "openai", "native_assistant",
                            "Rafael's existing ChatGPT, connected through a ChatGPT custom MCP connector")
    claude = core.create_agent(o, "Claude", "anthropic", "native_assistant",
                               "Rafael's existing Claude, connected through a Claude custom connector")
    agents = [gpt, claude]
    if with_simulated:
        agents += [core.create_agent(o, "Sim Requester", "simulator", "simulated"),
                   core.create_agent(o, "Sim Analyst", "simulator", "simulated")]
    for a in agents:
        core.add_member(o, room["id"], "agent", a["id"])
        others = [b["id"] for b in agents if b["id"] != a["id"] and b["is_simulated"] == a["is_simulated"]]
        core.create_grant(o, a["id"], room["id"], level=2, info_categories=["general", "project"],
                          allowed_targets=others, max_amount=0, max_per_hour=20, max_turns=6,
                          expires_in_hours=24 * 14, note="Pilot standing delegation: low-sensitivity text only")
    return {"room_id": room["id"], "agents": {a["display_name"]: a["id"] for a in agents},
            "mcp_url": "<COMMONS_PUBLIC_URL>/mcp"}


if __name__ == "__main__":
    main()
