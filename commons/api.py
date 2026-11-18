"""REST API: owner (human) endpoints and agent endpoints. Thin layer over the Core."""
from __future__ import annotations

from typing import Any

from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .core import CONNECTION_TYPES, Core
from .errors import AuthError, CommonsError
from .permissions import ACTIONS, INFO_CATEGORIES, LEVEL_NAMES, PLAIN_ACTIONS
from .statemachine import ALL_STATES, PLAIN, TRANSITIONS
from .util import Principal

COOKIE = "commons_session"


class Login(BaseModel):
    email: str
    password: str


class AgentIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)
    provider: str = Field(min_length=1, max_length=40)
    connection_type: str
    description: str = ""


class TokenIn(BaseModel):
    label: str = "api token"
    ttl_days: int = Field(default=30, ge=1, le=365)


class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=500)
    telegram_chat_id: str | None = None
    default_max_turns: int = Field(default=6, ge=1, le=20)
    retention_days: int = Field(default=90, ge=1, le=3650)


class RoomPatch(BaseModel):
    name: str | None = None
    purpose: str | None = None
    telegram_chat_id: str | None = None
    default_max_turns: int | None = Field(default=None, ge=1, le=20)
    retention_days: int | None = None
    status: str | None = None


class MemberIn(BaseModel):
    member_type: str
    member_id: str


class GrantIn(BaseModel):
    agent_id: str
    room_id: str | None = None
    actions: list[str] | None = None
    level: int | None = Field(default=None, ge=0, le=4)
    info_categories: list[str] = ["general"]
    allowed_targets: list[str] | None = None
    max_amount: float = 0
    max_per_hour: int = Field(default=30, ge=1, le=1000)
    max_turns: int | None = Field(default=None, ge=1, le=20)
    requires_approval: bool = False
    expires_in_hours: float | None = 24 * 30
    note: str = ""


class Decision(BaseModel):
    approve: bool
    note: str = ""
    extra_turns: int = Field(default=4, ge=1, le=10)


class Note(BaseModel):
    note: str = ""


class Revise(BaseModel):
    instructions: str = Field(min_length=1)


class TaskIn(BaseModel):
    room_id: str
    assignee_agent_id: str
    title: str
    brief: str
    info_category: str = "general"
    amount: float = 0
    deadline_hours: float | None = 72
    idempotency_key: str | None = None
    parent_task_id: str | None = None
    depends_on: list[str] | None = None


class ResponseIn(BaseModel):
    content: str
    final: bool = True
    idempotency_key: str | None = None


class ApprovalReq(BaseModel):
    reason: str


class SubIn(BaseModel):
    callback_url: str
    event_types: list[str] = ["inbox.new"]


class ContextIn(BaseModel):
    content: str
    info_category: str = "general"
    task_id: str | None = None


class PilotIn(BaseModel):
    step: str
    note: str
    manual: bool = True
    evidence: dict[str, Any] = {}


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def build_api(core: Core, settings, simulator=None, dispatcher=None) -> FastAPI:
    api = FastAPI(title="The New Commons API", version="0.1.0", docs_url="/docs", openapi_url="/openapi.json")

    @api.exception_handler(CommonsError)
    async def _err(_: Request, e: CommonsError):
        return JSONResponse(e.to_dict(), status_code=e.status)

    def human(request: Request) -> Principal:
        tok = request.cookies.get(COOKIE)
        b = _bearer(request)
        if not tok and b and b.startswith("ncs_"):
            tok = b
        return core.human_from_session(tok)

    def agent(request: Request) -> Principal:
        b = _bearer(request)
        if not b or b.startswith("ncs_"):
            raise AuthError("Agent bearer token required.")
        return core.agent_from_token(b)

    # ------------------------------------------------------------ public
    @api.get("/health")
    def health():
        return {"ok": True, "emergency_stop": core.safety.stopped()}

    @api.get("/meta")
    def meta():
        return {
            "connection_types": CONNECTION_TYPES, "actions": ACTIONS, "plain_actions": PLAIN_ACTIONS,
            "levels": LEVEL_NAMES, "info_categories": INFO_CATEGORIES, "states": ALL_STATES,
            "state_plain": PLAIN, "transitions": {k: sorted(v) for k, v in TRANSITIONS.items()},
            "mcp_url": settings.public_url + "/mcp", "public_url": settings.public_url,
            "telegram_enabled": bool(settings.telegram_bot_token),
        }

    # ------------------------------------------------------------ human session
    @api.post("/session")
    def login(body: Login):
        tok = core.login(body.email, body.password)
        r = JSONResponse({"ok": True, "session_token": tok})
        r.set_cookie(COOKIE, tok, httponly=True, samesite="strict", secure=settings.cookie_secure, max_age=12 * 3600)
        return r

    @api.delete("/session")
    def logout(request: Request):
        tok = request.cookies.get(COOKIE)
        if tok:
            core.logout(tok)
        r = JSONResponse({"ok": True})
        r.delete_cookie(COOKIE)
        return r

    @api.get("/me")
    def me(h: Principal = Depends(human)):
        return {**core.get_human(h.id), "emergency_stop": core.safety.stopped()}

    @api.put("/me/telegram")
    def link_telegram(body: dict = Body(...), h: Principal = Depends(human)):
        core.set_telegram_user(h, str(body.get("telegram_user_id") or "") or None)
        return core.get_human(h.id)

    # ------------------------------------------------------------ agents
    @api.get("/agents")
    def agents(h: Principal = Depends(human)):
        return core.list_agents(h)

    @api.post("/agents")
    def create_agent(body: AgentIn, h: Principal = Depends(human)):
        return core.create_agent(h, body.display_name, body.provider, body.connection_type, body.description)

    @api.post("/agents/{agent_id}/tokens")
    def issue_token(agent_id: str, body: TokenIn, h: Principal = Depends(human)):
        return core.issue_token(h, agent_id, body.label, body.ttl_days)

    @api.post("/agents/{agent_id}/status")
    def agent_status(agent_id: str, body: dict = Body(...), h: Principal = Depends(human)):
        return core.set_agent_status(h, agent_id, body.get("status", ""))

    @api.delete("/credentials/{cred_id}")
    def revoke_cred(cred_id: str, h: Principal = Depends(human)):
        core.revoke_credential(h, cred_id)
        return {"ok": True}

    # ------------------------------------------------------------ rooms
    @api.get("/rooms")
    def rooms(h: Principal = Depends(human)):
        return core.list_rooms_for_human(h)

    @api.post("/rooms")
    def create_room(body: RoomIn, h: Principal = Depends(human)):
        return core.create_room(h, body.name, body.purpose, body.telegram_chat_id, body.default_max_turns,
                                body.retention_days)

    @api.patch("/rooms/{room_id}")
    def patch_room(room_id: str, body: RoomPatch, h: Principal = Depends(human)):
        return core.update_room(h, room_id, **body.model_dump(exclude_none=True))

    @api.post("/rooms/{room_id}/members")
    def add_member(room_id: str, body: MemberIn, h: Principal = Depends(human)):
        return core.add_member(h, room_id, body.member_type, body.member_id)

    @api.delete("/rooms/{room_id}/members/{member_type}/{member_id}")
    def remove_member(room_id: str, member_type: str, member_id: str, h: Principal = Depends(human)):
        return core.remove_member(h, room_id, member_type, member_id)

    @api.get("/rooms/{room_id}/feed")
    def feed(room_id: str, h: Principal = Depends(human)):
        return core.room_feed(h, room_id)

    @api.get("/rooms/{room_id}/context")
    def room_context(room_id: str, h: Principal = Depends(human)):
        return core.list_context(h, room_id)

    @api.post("/rooms/{room_id}/context")
    def add_room_context(room_id: str, body: ContextIn, h: Principal = Depends(human)):
        return core.add_context(h, room_id, body.content, body.info_category, body.task_id)

    @api.post("/rooms/{room_id}/notes")
    def room_note(room_id: str, body: Note, h: Principal = Depends(human)):
        return core.human_note(h, room_id, body.note)

    # ------------------------------------------------------------ grants
    @api.get("/grants")
    def grants(h: Principal = Depends(human)):
        return core.list_grants(h)

    @api.post("/grants")
    def create_grant(body: GrantIn, h: Principal = Depends(human)):
        return core.create_grant(h, **body.model_dump())

    @api.delete("/grants/{grant_id}")
    def revoke_grant(grant_id: str, h: Principal = Depends(human)):
        return core.revoke_grant(h, grant_id)

    # ------------------------------------------------------------ tasks & approvals
    @api.get("/tasks")
    def tasks(room_id: str | None = None, h: Principal = Depends(human)):
        return core.tasks_for_owner(h, room_id)

    @api.get("/tasks/{task_id}")
    def task(task_id: str, h: Principal = Depends(human)):
        return core.task_for_owner(h, task_id)

    @api.post("/tasks/{task_id}/accept")
    def accept(task_id: str, body: Note, h: Principal = Depends(human)):
        return core.accept_task(h, task_id, body.note)

    @api.post("/tasks/{task_id}/revise")
    def revise(task_id: str, body: Revise, h: Principal = Depends(human)):
        return core.revise_task(h, task_id, body.instructions)

    @api.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str, body: Note, h: Principal = Depends(human)):
        return core.cancel_task(h, task_id, body.note)

    @api.get("/approvals")
    def approvals(status: str = "pending", h: Principal = Depends(human)):
        return core.approvals_for_owner(h, status)

    @api.post("/approvals/{approval_id}")
    def decide(approval_id: str, body: Decision, h: Principal = Depends(human)):
        return core.decide_approval(h, approval_id, body.approve, body.note, body.extra_turns)

    # ------------------------------------------------------------ oversight
    @api.get("/audit")
    def audit(limit: int = 200, target_id: str | None = None, h: Principal = Depends(human)):
        return core.audit.list(min(limit, 1000), target_id)

    @api.get("/audit/verify")
    def audit_verify(h: Principal = Depends(human)):
        return core.audit.verify()

    @api.post("/emergency-stop")
    def estop(body: dict = Body(...), h: Principal = Depends(human)):
        return core.set_emergency_stop(h, bool(body.get("on")))

    @api.get("/pilot")
    def pilot(h: Principal = Depends(human)):
        return core.pilot_list()

    @api.post("/pilot")
    def pilot_add(body: PilotIn, h: Principal = Depends(human)):
        return core.pilot_record(h, body.step, body.note, body.manual, body.evidence)

    @api.post("/simulator/step")
    def sim_step(h: Principal = Depends(human)):
        if simulator is None:
            raise CommonsError("Simulator is not enabled.")
        return simulator.step_all(h)

    @api.post("/dispatch")
    def dispatch(h: Principal = Depends(human)):
        core.sweep()
        return dispatcher.process() if dispatcher else {}

    # ------------------------------------------------------------ agent endpoints (bearer)
    @api.get("/agent/whoami")
    def a_whoami(p: Principal = Depends(agent)):
        return core.get_agent(p.id)

    @api.get("/agent/rooms")
    def a_rooms(p: Principal = Depends(agent)):
        return core.list_rooms_for_agent(p)

    @api.get("/agent/inbox")
    def a_inbox(limit: int = 10, p: Principal = Depends(agent)):
        return core.read_inbox(p, limit)

    @api.post("/agent/tasks")
    def a_submit(body: TaskIn, p: Principal = Depends(agent)):
        return core.submit_task(p, **body.model_dump())

    @api.get("/agent/tasks/{task_id}")
    def a_task(task_id: str, p: Principal = Depends(agent)):
        return core.get_task_for_agent(p, task_id)

    @api.post("/agent/tasks/{task_id}/responses")
    def a_respond(task_id: str, body: ResponseIn, p: Principal = Depends(agent)):
        return core.respond(p, task_id, body.content, body.final, body.idempotency_key)

    @api.post("/agent/tasks/{task_id}/approval-requests")
    def a_approval(task_id: str, body: ApprovalReq, p: Principal = Depends(agent)):
        return core.request_approval(p, task_id, body.reason)

    @api.post("/agent/tasks/{task_id}/cancel")
    def a_cancel(task_id: str, body: Note, p: Principal = Depends(agent)):
        return core.cancel_task(p, task_id, body.note)

    @api.post("/agent/subscriptions")
    def a_sub(body: SubIn, p: Principal = Depends(agent)):
        return core.subscribe(p, body.callback_url, body.event_types)

    @api.delete("/agent/subscriptions/{sub_id}")
    def a_unsub(sub_id: str, p: Principal = Depends(agent)):
        core.unsubscribe(p, sub_id)
        return {"ok": True}

    @api.get("/agent/rooms/{room_id}/context")
    def a_ctx(room_id: str, p: Principal = Depends(agent)):
        return core.list_context(p, room_id)

    @api.post("/agent/rooms/{room_id}/context")
    def a_ctx_add(room_id: str, body: ContextIn, p: Principal = Depends(agent)):
        return core.add_context(p, room_id, body.content, body.info_category, body.task_id)

    return api
