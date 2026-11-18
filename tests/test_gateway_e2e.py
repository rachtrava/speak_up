"""End-to-end over real HTTP: OAuth 2.1 (DCR + PKCE + consent) and the MCP tools via the official MCP client.

These run the same code paths a ChatGPT or Claude custom connector would use. They do NOT
prove that ChatGPT or Claude can connect: that requires the native pilot (docs/PILOT_REPORT.md).
"""
import asyncio
import base64
import hashlib
import json
import re
import secrets

import httpx
import pytest

from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def pkce():
    v = secrets.token_urlsafe(48)
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


def oauth_connect(url, agent_id, password="correct horse battery staple", redirect=REDIRECT):
    with httpx.Client(base_url=url, follow_redirects=False) as c:
        reg = c.post("/register", json={"client_name": "Test Connector", "redirect_uris": [redirect],
                                        "token_endpoint_auth_method": "none",
                                        "grant_types": ["authorization_code", "refresh_token"],
                                        "response_types": ["code"]})
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]
        verifier, challenge = pkce()
        r = c.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
                                        "code_challenge": challenge, "code_challenge_method": "S256",
                                        "state": "st8", "scope": "commons"})
        assert r.status_code == 302, r.text
        consent = r.headers["location"]
        req = re.search(r"req=([\w-]+)", consent).group(1)
        page = c.get(consent)
        assert "Sign in" in page.text and agent_id not in page.text  # no agent list before login
        page = c.post("/oauth/consent", data={"req": req, "step": "login", "email": "owner@example.com",
                                              "password": password})
        assert agent_id in page.text
        r = c.post("/oauth/consent", data={"req": req, "step": "choose", "agent_id": agent_id, "decision": "allow"})
        assert r.status_code == 302 and r.headers["location"].startswith(redirect)
        code = re.search(r"code=([\w-]+)", r.headers["location"]).group(1)
        assert "state=st8" in r.headers["location"]
        tok = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                     "client_id": client_id, "code_verifier": verifier})
        assert tok.status_code == 200, tok.text
        replay = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                        "client_id": client_id, "code_verifier": verifier})
        assert replay.status_code == 400  # authorization codes are single use
        return client_id, tok.json()


async def mcp_call(url, token, tool, args=None):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http:
        async with streamable_http_client(url + "/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                res = await s.call_tool(tool, args or {})
                if res.is_error if hasattr(res, "is_error") else res.isError:
                    return {"__error__": res.content[0].text}
                sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
                return sc if sc is not None else json.loads(res.content[0].text)


def call(url, token, tool, args=None):
    out = asyncio.run(mcp_call(url, token, tool, args))
    if isinstance(out, dict) and "__error__" in out:
        raise RuntimeError(out["__error__"])
    return out


def test_oauth_binds_connection_to_chosen_agent(live):
    e = live
    client_id, tok = oauth_connect(e.url, e.claude["id"])
    me = call(e.url, tok["access_token"], "whoami")
    assert me["agent_id"] == e.claude["id"] and me["credential"] == "oauth_access" and me["simulated"] is False
    # Refresh rotation: new tokens issued, old refresh token dead.
    with httpx.Client(base_url=e.url) as c:
        r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                   "client_id": client_id})
        assert r.status_code == 200
        again = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                       "client_id": client_id})
        assert again.status_code == 400
    pilot = e.core.pilot_list()
    assert pilot and pilot[0]["manual"] and pilot[0]["evidence"]["redirect_uri"] == REDIRECT


def test_unlisted_redirect_uri_rejected(live):
    r = httpx.post(live.url + "/register", json={"client_name": "evil", "redirect_uris": ["https://evil.example/cb"],
                                                "token_endpoint_auth_method": "none"})
    assert r.status_code == 400


def test_wrong_password_does_not_bind(live):
    with pytest.raises(AssertionError):
        oauth_connect(live.url, live.claude["id"], password="wrong password!!")


def test_mcp_requires_auth_and_honors_revocation(live):
    e = live
    with pytest.raises(Exception):
        call(e.url, "nca_forged", "whoami")
    assert call(e.url, e.tok_gpt, "whoami")["agent_id"] == e.gpt["id"]
    cred = e.db.one("SELECT id FROM credentials WHERE agent_id=? AND kind='static'", (e.gpt["id"],))
    e.core.revoke_credential(e.owner, cred["id"])
    with pytest.raises(Exception):
        call(e.url, e.tok_gpt, "whoami")


def test_full_round_trip_over_mcp(live):
    """Acceptance steps 2-10 with two separately authenticated MCP connections."""
    e = live
    rooms = call(e.url, e.tok_gpt, "list_rooms")["rooms"]
    assert rooms[0]["room_id"] == e.room["id"]
    assert any(m["type"] == "human" for m in rooms[0]["members"])
    task = call(e.url, e.tok_gpt, "submit_task", {
        "room_id": e.room["id"], "assignee_agent_id": e.claude["id"], "title": "Critique the brief",
        "brief": "Brief: launch a newsletter for adults over forty. What is missing?",
        "info_category": "project", "idempotency_key": "e2e-1"})
    assert task["state"] == "delivered"
    inbox = call(e.url, e.tok_claude, "read_inbox", {"limit": 5})
    assert inbox["items"][0]["task_id"] == task["id"] and "UNTRUSTED" in inbox["notice"]
    assert call(e.url, e.tok_claude, "get_task_status", {"task_id": task["id"]})["state"] == "retrieved"
    resp = call(e.url, e.tok_claude, "send_response", {"task_id": task["id"], "content": "Missing: audience research.",
                                                       "idempotency_key": "e2e-r1"})
    assert resp["task"]["state"] == "result_ready"
    back = call(e.url, e.tok_gpt, "read_inbox")
    assert back["items"][0]["kind"] == "response" and back["items"][0]["from"]["name"] == "Claude"
    call(e.url, e.tok_gpt, "send_response", {"task_id": task["id"], "content": "Synthesis: add audience research."})
    assert e.core.accept_task(e.owner, task["id"])["state"] == "accepted"
    assert e.core.audit.verify()["valid"]
    # Every MCP action is attributed to the authenticated agent with its credential type.
    acts = [r for r in e.core.audit.list(500) if r["actor_id"] == e.claude["id"]]
    assert acts and all(r["detail"].get("via") == "static" for r in acts)


def test_mcp_cancel_and_approval_tools(live):
    e = live
    t = call(e.url, e.tok_gpt, "submit_task", {"room_id": e.room["id"], "assignee_agent_id": e.claude["id"],
                                               "title": "T", "brief": "B", "info_category": "project"})
    call(e.url, e.tok_claude, "read_inbox")
    r = call(e.url, e.tok_claude, "request_human_approval", {"task_id": t["id"], "reason": "Need a decision"})
    assert r["status"] == "pending"
    with pytest.raises(Exception, match="forbidden"):
        call(e.url, e.tok_claude, "cancel_task", {"task_id": t["id"]})
    assert call(e.url, e.tok_gpt, "cancel_task", {"task_id": t["id"], "reason": "done"})["state"] == "cancelled"


def test_rest_agent_api_and_owner_api(live):
    e = live
    with httpx.Client(base_url=e.url) as c:
        assert c.post("/api/session", json={"email": "owner@example.com", "password": "nope"}).status_code == 401
        assert c.post("/api/session", json={"email": "owner@example.com",
                                            "password": "correct horse battery staple"}).status_code == 200
        assert len(c.get("/api/rooms").json()) == 2
        ah = {"Authorization": f"Bearer {e.tok_gpt}"}
        t = c.post("/api/agent/tasks", headers=ah, json={"room_id": e.room["id"], "assignee_agent_id": e.claude["id"],
                                                         "title": "R", "brief": "B", "info_category": "project"}).json()
        assert t["state"] == "delivered"
        detail = c.get(f"/api/tasks/{t['id']}").json()
        assert [d["channel"] for d in detail["deliveries"]].count("inbox") == 1
        assert c.get("/api/audit/verify").json()["valid"]
        assert c.get("/api/meta").json()["mcp_url"].endswith("/mcp")
