"""OAuth 2.1 authorization server for assistant connectors (ChatGPT / Claude custom MCP connectors).

Flow: the provider registers dynamically (RFC 7591) -> sends the owner to /authorize ->
we redirect to our consent page -> the owner signs in and picks WHICH of their agents
this connection will act as -> code (PKCE, handled by the SDK) -> access + refresh tokens.

The token is bound to exactly one agent identity. An assistant can never choose or
claim its identity in a message; identity comes from this binding.
"""
from __future__ import annotations

import html
import time
from urllib.parse import urlparse

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, RegistrationError,
    TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .db import jdump, jload
from .errors import AuthError, CommonsError
from .util import Principal, in_future, iso, new_id, new_token, sha256, verify_password

ACCESS_TTL = 3600
REFRESH_TTL_DAYS = 30
SCOPE = "commons"


class CommonsOAuthProvider:
    def __init__(self, core, public_url: str, redirect_allowlist: list[str]):
        self.core = core
        self.db = core.db
        self.public_url = public_url.rstrip("/")
        self.redirect_allowlist = redirect_allowlist

    # ---- clients ---------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        r = self.db.one("SELECT info_json FROM oauth_clients WHERE client_id=?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(r["info_json"]) if r else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not self.redirect_allowed(str(uri)):
                raise RegistrationError("invalid_redirect_uri",
                                        f"Redirect URI {uri} is not on this Commons' allowlist.")
        self.db.run("INSERT INTO oauth_clients VALUES (?,?,?)",
                    (client_info.client_id, client_info.model_dump_json(), iso()))
        from .util import SYSTEM
        self.core.audit.record(SYSTEM, "oauth.client_registered", target_type="oauth_client",
                               target_id=client_info.client_id, client_name=client_info.client_name,
                               redirect_uris=[str(u) for u in client_info.redirect_uris or []])

    def redirect_allowed(self, uri: str) -> bool:
        return any(uri.startswith(prefix) for prefix in self.redirect_allowlist)

    # ---- authorize -> consent ------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.redirect_allowed(str(params.redirect_uri)):
            raise AuthorizeError("invalid_request", "redirect_uri not allowed")
        rid = new_id("oreq")
        self.db.run("INSERT INTO oauth_pending (id, client_id, params_json, created_at, expires_at) VALUES (?,?,?,?,?)",
                    (rid, client.client_id, params.model_dump_json(), iso(), in_future(minutes=10)))
        return f"{self.public_url}/oauth/consent?req={rid}"

    def _pending(self, rid: str) -> tuple[dict, AuthorizationParams, OAuthClientInformationFull]:
        p = self.db.one("SELECT * FROM oauth_pending WHERE id=?", (rid,))
        if not p or p["used_at"] or p["expires_at"] <= iso():
            raise CommonsError("This connection request has expired. Start again from your assistant.")
        params = AuthorizationParams.model_validate_json(p["params_json"])
        client = OAuthClientInformationFull.model_validate_json(
            self.db.one("SELECT info_json FROM oauth_clients WHERE client_id=?", (p["client_id"],))["info_json"])
        return p, params, client

    async def consent_page(self, request: Request) -> Response:
        rid = request.query_params.get("req") or (await request.form()).get("req", "")
        try:
            p, params, client = self._pending(str(rid))
        except CommonsError as e:
            return HTMLResponse(_page("Connection expired", f"<p>{html.escape(e.message)}</p>"), status_code=400)
        error = ""
        host = html.escape(urlparse(str(params.redirect_uri)).netloc)
        intro = (f"<p>An assistant at <strong>{host}</strong> ({html.escape(client.client_name or 'unnamed app')}) "
                 "is asking to connect to your Commons.</p>")
        if request.method == "POST":
            form = await request.form()
            step = form.get("step")
            if step == "login":
                email = str(form.get("email", "")).lower()
                h = self.db.one("SELECT * FROM humans WHERE email=?", (email,))
                if h and verify_password(str(form.get("password", "")), h["password_hash"]):
                    self.db.run("UPDATE oauth_pending SET human_id=? WHERE id=?", (h["id"], rid))
                    p["human_id"] = h["id"]
                else:
                    error = "Email or password is incorrect."
            elif step == "choose" and p["human_id"]:
                h = self.db.one("SELECT * FROM humans WHERE id=?", (p["human_id"],))
                agent_id = str(form.get("agent_id", ""))
                agent = self.db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
                if form.get("decision") != "allow":
                    self.db.run("UPDATE oauth_pending SET used_at=? WHERE id=?", (iso(), rid))
                    return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), error="access_denied",
                                                                   state=params.state), status_code=302)
                if not agent or agent["owner_id"] != h["id"] or agent["status"] != "active":
                    error = "Choose one of your own active assistants."
                elif agent["is_simulated"]:
                    error = "Simulated agents cannot be connected to a real assistant."
                else:
                    return self._complete(rid, client, params, h, agent)
        if not p["human_id"]:
            body = intro + f"""<p>Sign in to choose which of your assistants this is.</p>
{f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ''}
<form method="post"><input type="hidden" name="req" value="{html.escape(str(rid))}">
<input type="hidden" name="step" value="login">
<label>Email<input name="email" type="email" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button>Sign in</button></form>"""
            return HTMLResponse(_page("Connect an assistant", body))
        agents = self.db.all("SELECT * FROM agents WHERE owner_id=? AND is_simulated=0 AND status='active'"
                             " ORDER BY created_at", (p["human_id"],))
        opts = "".join(
            f'<label class="opt"><input type="radio" name="agent_id" value="{a["id"]}" required> '
            f'{html.escape(a["display_name"])} <span class="sub">({html.escape(a["provider"])}, '
            f'{html.escape(a["connection_type"].replace("_", " "))})</span></label>' for a in agents)
        body = intro + f"""<p>It will act as the AI agent you choose, with only the permissions you granted that agent.
You can disconnect it at any time from the Commons.</p>
{f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ''}
<form method="post"><input type="hidden" name="req" value="{html.escape(str(rid))}">
<input type="hidden" name="step" value="choose">
<fieldset><legend>Which of your assistants is this?</legend>{opts or '<p>No agents yet. Register one in the Commons first.</p>'}</fieldset>
<div class="row"><button name="decision" value="allow">Allow connection</button>
<button name="decision" value="deny" class="secondary" formnovalidate>Deny</button></div></form>"""
        return HTMLResponse(_page("Connect an assistant", body))

    def _complete(self, rid, client, params, h, agent) -> Response:
        agent_id = agent["id"]
        code = new_token("nco")
        ac = AuthorizationCode(
            code="-", scopes=params.scopes or [SCOPE], expires_at=time.time() + 300,
            client_id=client.client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource, subject=agent_id)
        redirect_host = urlparse(str(params.redirect_uri)).netloc
        with self.db.tx():
            self.db.run("UPDATE oauth_pending SET used_at=? WHERE id=?", (iso(), rid))
            self.db.run("INSERT INTO oauth_codes VALUES (?,?,?,?,?,NULL)",
                        (sha256(code), client.client_id, agent_id, ac.model_dump_json(), in_future(minutes=5)))
            owner = Principal("human", h["id"], h["id"], h["display_name"])
            self.core.audit.record(owner, "oauth.connection_approved", target_type="agent", target_id=agent_id,
                                   oauth_client_id=client.client_id, client_name=client.client_name,
                                   redirect_host=redirect_host)
            self.core.pilot_record(owner, "connect_assistant",
                                   f"Owner approved an OAuth connection for agent {agent['display_name']} "
                                   f"from {redirect_host}", manual=True,
                                   evidence={"oauth_client_id": client.client_id, "client_name": client.client_name,
                                             "redirect_uri": str(params.redirect_uri)})
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                status_code=302)

    # ---- codes & tokens --------------------------------------------------
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        r = self.db.one("SELECT * FROM oauth_codes WHERE code_hash=?", (sha256(authorization_code),))
        if not r or r["used_at"] or r["expires_at"] <= iso() or r["client_id"] != client.client_id:
            return None
        ac = AuthorizationCode.model_validate_json(r["data_json"])
        return ac.model_copy(update={"code": authorization_code})

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        cur = self.db.run("UPDATE oauth_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL",
                          (iso(), sha256(authorization_code.code)))
        if cur.rowcount != 1:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.subject, authorization_code.scopes)

    def _issue(self, client_id: str, agent_id: str, scopes: list[str]) -> OAuthToken:
        access, refresh = new_token("nca"), new_token("ncr")
        self.core.store_credential(agent_id, access, "oauth_access", client_id, scopes, in_future(seconds=ACCESS_TTL),
                                   "oauth access token")
        self.core.store_credential(agent_id, refresh, "oauth_refresh", client_id, scopes,
                                   in_future(days=REFRESH_TTL_DAYS), "oauth refresh token")
        from .util import SYSTEM
        self.core.audit.record(SYSTEM, "oauth.tokens_issued", target_type="agent", target_id=agent_id,
                               oauth_client_id=client_id)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, refresh_token=refresh, scope=" ".join(scopes))

    async def load_refresh_token(self, client, refresh_token: str):
        r = self.db.one("SELECT * FROM credentials WHERE token_hash=? AND kind='oauth_refresh'", (sha256(refresh_token),))
        if not r or r["revoked_at"] or (r["expires_at"] and r["expires_at"] <= iso()) or r["oauth_client_id"] != client.client_id:
            return None
        agent = self.db.one("SELECT status FROM agents WHERE id=?", (r["agent_id"],))
        if not agent or agent["status"] != "active":
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=jload(r["scopes"], [SCOPE]),
                            subject=r["agent_id"])

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        cur = self.db.run("UPDATE credentials SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                          (iso(), sha256(refresh_token.token)))
        if cur.rowcount != 1:
            raise TokenError("invalid_grant", "refresh token already used")
        return self._issue(client.client_id, refresh_token.subject, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            p = self.core.agent_from_token(token)
        except AuthError:
            return None
        return AccessToken(token=token, client_id=p.oauth_client_id or "static-token", scopes=[SCOPE],
                           subject=p.id)

    async def revoke_token(self, token) -> None:
        self.db.run("UPDATE credentials SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                    (iso(), sha256(token.token)))


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} - The New Commons</title>
<style>
body{{font:18px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f7f5f0;color:#1a1a2e;margin:0;padding:24px}}
main{{max-width:560px;margin:auto;background:#fff;border:2px solid #1f2a5a;border-radius:12px;padding:28px}}
h1{{color:#1f2a5a;font-size:28px;margin-top:0}} label{{display:block;margin:14px 0;font-weight:600}}
input[type=email],input[type=password]{{display:block;width:100%;font-size:18px;padding:10px;border:2px solid #555;border-radius:8px;box-sizing:border-box;margin-top:6px}}
.opt{{font-weight:400;border:1px solid #ccc;border-radius:8px;padding:10px}} .sub{{color:#444;font-size:15px}}
fieldset{{border:none;padding:0}} legend{{font-weight:700;margin-bottom:6px}}
button{{font-size:18px;padding:12px 20px;border-radius:8px;border:2px solid #1f2a5a;background:#1f2a5a;color:#fff;cursor:pointer;min-height:48px}}
button.secondary{{background:#fff;color:#1f2a5a}} .row{{display:flex;gap:12px;flex-wrap:wrap}} .err{{color:#a30000;font-weight:700}}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
