"""Composition root: one process, one SQLite file, one HTTP port.

  /mcp                    MCP (streamable HTTP), OAuth-protected
  /authorize /token /register /.well-known/*   OAuth 2.1 endpoints (MCP SDK)
  /oauth/consent          owner consent page
  /api/*                  REST API (owner + agents)
  /telegram/webhook       Telegram updates (secret-header verified)
  /                       web interface
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings

from .api import build_api
from .config import Settings
from .core import Core
from .db import Database
from .events import Dispatcher
from .mcp_tools import build_mcp
from .oauth import SCOPE, CommonsOAuthProvider
from .simulator import Simulator
from .telegram import TelegramAdapter, handle_update

log = logging.getLogger("commons")
WEB = Path(__file__).parent / "web"


def create_app(settings: Settings | None = None, db: Database | None = None, run_background: bool = True):
    settings = settings or Settings.from_env()
    db = db or Database(settings.db_path)
    core = Core(db)
    telegram = TelegramAdapter(settings.telegram_bot_token, settings.telegram_webhook_secret)
    dispatcher = Dispatcher(db, core, telegram)
    simulator = Simulator(core)
    provider = CommonsOAuthProvider(core, settings.public_url, settings.redirect_allowlist)
    auth = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=settings.public_url + "/mcp",
        validate_token_resource=False,  # tokens are bound to an agent by our own store
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                              default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
    )
    mcp = build_mcp(core, provider, auth)

    @mcp.custom_route("/oauth/consent", methods=["GET", "POST"])
    async def consent(request: Request) -> Response:
        return await provider.consent_page(request)

    @mcp.custom_route("/telegram/webhook", methods=["POST"])
    async def tg_webhook(request: Request) -> Response:
        if not telegram.check_secret(request.headers.get("x-telegram-bot-api-secret-token")):
            return JSONResponse({"ok": False}, status_code=403)
        update = await request.json()
        reply = handle_update(core, update)
        chat = ((update.get("message") or {}).get("chat") or {}).get("id")
        if reply and chat and telegram.enabled:
            with contextlib.suppress(Exception):
                telegram.send(str(chat), reply)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request) -> Response:
        return FileResponse(WEB / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=settings.allowed_hosts,
                                                     allowed_origins=[settings.public_url]),
    )
    app.mount("/api", build_api(core, settings, simulator, dispatcher))

    original = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with original(a):
            task = None
            if run_background:
                task = asyncio.create_task(_background(core, dispatcher, settings.dispatch_interval))
            try:
                yield
            finally:
                if task:
                    task.cancel()

    app.router.lifespan_context = lifespan
    app.state.core = core
    app.state.dispatcher = dispatcher
    app.state.simulator = simulator
    app.state.settings = settings
    return app


async def _background(core: Core, dispatcher: Dispatcher, interval: float) -> None:
    while True:
        try:
            await asyncio.to_thread(core.sweep)
            await asyncio.to_thread(dispatcher.process)
        except Exception:  # noqa: BLE001
            log.exception("background dispatch failed")
        await asyncio.sleep(interval)
