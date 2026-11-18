"""Local end-to-end demonstration with SIMULATED agents.

Starts a real Commons server, connects two SIMULATED agents over MCP (each with its own
credential), runs the acceptance scenario's message path, and prints every step.
This proves the Commons machinery. It does NOT prove native ChatGPT/Claude connectivity.

    python scripts/demo.py                 # fresh demo DB at data/demo.db
    python -m commons serve                # (with COMMONS_DB=data/demo.db) to browse it in the UI
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from commons.__main__ import setup_pilot  # noqa: E402
from commons.app import create_app  # noqa: E402
from commons.config import Settings  # noqa: E402
from commons.core import Core  # noqa: E402
from commons.db import Database  # noqa: E402
from commons.simulator import analyze, synthesize  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # noqa: E402

PORT = int(os.getenv("DEMO_PORT", "8765"))
EMAIL, PASSWORD = "owner@example.com", os.getenv("DEMO_PASSWORD", "demo password 123")


async def tool(url, token, name, args=None):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, streamable_http_client(url + "/mcp", http_client=http) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool(name, args or {})
        sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        return sc if sc is not None else json.loads(res.content[0].text)


def step(n, text):
    print(f"\n[{n}] {text}")


def main():
    db_path = Path("data/demo.db")
    db_path.parent.mkdir(exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    db = Database(str(db_path))
    core = Core(db)
    core.create_human("Rafael Trava de Alba", EMAIL, PASSWORD)
    owner = core.human_from_session(core.login(EMAIL, PASSWORD))
    ids = setup_pilot(core, owner, telegram_chat_id=None, with_simulated=True)
    req, ana = ids["agents"]["Sim Requester"], ids["agents"]["Sim Analyst"]
    tok_req = core.issue_token(owner, req, "demo")["token"]
    tok_ana = core.issue_token(owner, ana, "demo")["token"]

    settings = Settings(db_path=str(db_path), public_url=f"http://localhost:{PORT}", port=PORT)
    server = uvicorn.Server(uvicorn.Config(create_app(settings, db, run_background=False), host="127.0.0.1",
                                           port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://localhost:{PORT}"
    run = lambda *a: asyncio.run(tool(url, *a))  # noqa: E731

    print("THE NEW COMMONS - local demonstration with SIMULATED agents (not native assistants)")
    step(1, "Owner instructs the requester agent (simulated: scripted here).")
    me = run(tok_req, "whoami")
    print("   connection acts as:", me["label"])
    step(2, "Requester submits the task through its own MCP connection.")
    brief = ("Project brief: launch a bilingual newsletter for adults over forty. "
             "Budget is small and the launch deadline is in six weeks. What are we missing?")
    t = run(tok_req, "submit_task", {"room_id": ids["room_id"], "assignee_agent_id": ana, "title": "Critique the newsletter brief",
                                     "brief": brief, "info_category": "project", "idempotency_key": "demo-1"})
    step(3, f"Core authenticated, authorized and stored it. State: {t['state']} ({t['state_plain']})")
    dup = run(tok_req, "submit_task", {"room_id": ids["room_id"], "assignee_agent_id": ana, "title": "Critique the newsletter brief",
                                       "brief": brief, "info_category": "project", "idempotency_key": "demo-1"})
    print("   retry with same idempotency key returns same task:", dup["id"] == t["id"])
    step(4, "Task is in the analyst's inbox (delivered, not yet retrieved).")
    step(5, "Telegram mirror: " + json.dumps([d["status"] for d in db.all(
        "SELECT d.status FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE m.task_id=? AND d.channel='telegram'",
        (t["id"],))]) + "  (skipped = no Telegram chat configured in this demo)")
    step(6, "Analyst retrieves it through its own connection.")
    inbox = run(tok_ana, "read_inbox")
    item = inbox["items"][0]
    print("   from:", item["from"]["label"])
    print("   state now:", run(tok_ana, "get_task_status", {"task_id": t["id"]})["state"])
    step(7, "Analyst contributes an independent analysis (rule-based stand-in).")
    r = run(tok_ana, "send_response", {"task_id": t["id"], "content": analyze(item["content"])})
    print("   state now:", r["task"]["state"])
    step(8, "Core stores it and delivers to the requester's inbox.")
    back = run(tok_req, "read_inbox")["items"][0]
    step(9, "Requester retrieves the response and posts a synthesis.")
    run(tok_req, "send_response", {"task_id": t["id"], "content": synthesize(t["title"], back["content"])})
    step(10, "Owner reviews and accepts (via Core; same as the web UI or Telegram /accept).")
    final = core.accept_task(owner, t["id"], "Good enough for a first pass")
    print("   final state:", final["state"], "-", final["state_plain"])
    print("\nTransitions:", " -> ".join(x["to_state"] for x in db.all(
        "SELECT to_state FROM task_transitions WHERE task_id=? ORDER BY id", (t["id"],))))
    print("Audit chain:", core.audit.verify())
    print(f"\nDemo database: {db_path}. Browse it: COMMONS_DB={db_path} python -m commons serve  "
          f"then open http://localhost:8000 and sign in as {EMAIL}")
    server.should_exit = True
    time.sleep(0.5)


if __name__ == "__main__":
    main()
