import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn

from commons.config import Settings
from commons.core import Core
from commons.db import Database

PW = "correct horse battery staple"


def build_env(db: Database | None = None, with_grants: bool = True):
    db = db or Database(":memory:")
    core = Core(db)
    core.create_human("Rafael Trava de Alba", "owner@example.com", PW)
    owner = core.human_from_session(core.login("owner@example.com", PW))
    gpt = core.create_agent(owner, "ChatGPT", "openai", "native_assistant")
    claude = core.create_agent(owner, "Claude", "anthropic", "native_assistant")
    room = core.create_room(owner, "Pilot", "Project brief")
    other_room = core.create_room(owner, "Private", "Something else")
    grants = {}
    for a in (gpt, claude):
        core.add_member(owner, room["id"], "agent", a["id"])
        if with_grants:
            grants[a["id"]] = core.create_grant(owner, a["id"], room["id"], level=2,
                                                info_categories=["general", "project"], max_turns=6)
    tok_gpt = core.issue_token(owner, gpt["id"])["token"]
    tok_claude = core.issue_token(owner, claude["id"])["token"]
    return SimpleNamespace(
        db=db, core=core, owner=owner, gpt=gpt, claude=claude, room=room, other_room=other_room, grants=grants,
        tok_gpt=tok_gpt, tok_claude=tok_claude,
        p_gpt=core.agent_from_token(tok_gpt), p_claude=core.agent_from_token(tok_claude),
    )


@pytest.fixture
def env():
    return build_env()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live(tmp_path):
    """A real Commons server on a local port, sharing its database with the test."""
    from commons.app import create_app
    port = _free_port()
    db = Database(str(tmp_path / "commons.db"))
    e = build_env(db)
    settings = Settings(db_path=str(tmp_path / "commons.db"), public_url=f"http://localhost:{port}", port=port)
    app = create_app(settings, db, run_background=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    e.url = f"http://localhost:{port}"
    e.app = app
    yield e
    server.should_exit = True
    t.join(timeout=5)
