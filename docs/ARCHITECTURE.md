# Architecture

## Layers as built

```mermaid
flowchart TD
    H[Human owner] -->|chats| A1[Personal ChatGPT]
    H -->|chats| A2[Personal Claude]
    H -->|web UI / Telegram commands| UI
    A1 -->|MCP over HTTPS, OAuth token bound to agent| G
    A2 -->|MCP over HTTPS, OAuth token bound to agent| G
    SIM[SIMULATED agents] -.->|in-process, labeled| C
    CA[Coding agents] -->|MCP or REST, bearer token| G
    subgraph Commons process
      G[Gateway: MCP tools + OAuth 2.1 + REST] --> C[Commons Core]
      UI[Owner web UI + REST] --> C
      TGIN[Telegram webhook / poller] --> C
      C --> ID[Identity registry]
      C --> PE[Permission engine]
      C --> MB[Message broker: messages, inbox deliveries]
      C --> TO[Task orchestrator: state machine]
      C --> CTX[Shared context]
      C --> AU[Audit ledger: hash chain]
      C --> SE[Safety engine: limits, screening, e-stop]
      C --> OB[Outbox]
      OB --> D[Dispatcher with retries]
    end
    D -->|sendMessage, attributed| TG[Telegram private group]
    D -->|signed webhook, ids only| WH[Agent event subscribers]
    C --- DB[(SQLite)]
```

Code map:

| Layer | Module |
|---|---|
| 1 Human ownership | `core.py` (humans, sessions, agents, grants, approvals), `oauth.py` consent |
| 2 Personal agents | outside the Commons. `CONNECTION_TYPES` in `core.py` keeps the four kinds distinct |
| 3 Gateway | `mcp_tools.py` (MCP), `oauth.py` (OAuth provider), `api.py` (REST) |
| 4 Commons Core | `core.py`, `permissions.py`, `statemachine.py`, `audit.py`, `security.py`, `events.py` |
| 5 Rooms | `rooms`, `room_members` tables; `telegram.py` presentation adapter; `web/index.html` |
| 6 External services | `action.execute` permission exists; no adapters enabled (by instruction) |

## Acceptance scenario sequence

```mermaid
sequenceDiagram
    actor R as Rafael
    participant GPT as ChatGPT (native)
    participant C as Commons Core
    participant TG as Telegram room
    participant CL as Claude (native)
    R->>GPT: "Work with my Claude on this brief"
    GPT->>C: submit_task (OAuth token = ChatGPT agent)
    C->>C: authenticate, authorize (grant), store, audit
    C->>C: state created>authorized>queued>delivered (Claude inbox)
    C-->>TG: attributed copy "ChatGPT (AI, for Rafael) -> Claude"
    Note over R,CL: MANUAL ACTIVATION 1: Rafael asks Claude to check its Commons inbox
    R->>CL: "Check my Commons inbox"
    CL->>C: read_inbox (OAuth token = Claude agent)
    C->>C: delivered>retrieved (only now)
    CL->>C: send_response (independent analysis)
    C->>C: retrieved>result_ready, deliver to ChatGPT inbox
    C-->>TG: attributed result
    C-->>GPT: (future) MCP Events notification, if available
    Note over R,GPT: MANUAL ACTIVATION 2: Rafael asks ChatGPT to check for the reply
    R->>GPT: "Any reply from Claude?"
    GPT->>C: read_inbox, then send_response (synthesis)
    GPT->>R: synthesized result
    R->>C: accept / revise / cancel (web UI or Telegram)
    C-->>TG: status
```

## Task state machine

```mermaid
stateDiagram-v2
    [*] --> created
    created --> awaiting_authorization: outside standing delegation
    created --> authorized: covered by grant
    awaiting_authorization --> authorized: owner approves
    awaiting_authorization --> cancelled: owner declines
    authorized --> queued
    queued --> delivered: placed in assignee inbox
    delivered --> retrieved: assignee reads inbox
    retrieved --> in_progress
    retrieved --> result_ready
    in_progress --> result_ready
    in_progress --> awaiting_human_input: agent asks / turn limit / held message
    retrieved --> awaiting_human_input
    awaiting_human_input --> in_progress: owner decides
    result_ready --> accepted: owner accepts
    result_ready --> in_progress: owner asks for revision
    accepted --> [*]
    note right of delivered: Telegram copy and webhooks never move a task to retrieved
```
Every non-terminal state can also go to `cancelled`, `expired` (deadline sweep) or `failed`. Transitions are enforced in `statemachine.check()` with optimistic locking on `tasks.version`; every transition is stored in `task_transitions` and the audit ledger.

## Authorization path (every agent call)

1. Token resolves to one agent (`agent_from_token`): unknown, revoked, expired or suspended -> 401.
2. Emergency stop, global rate limit, agent active (`_agent_gate`).
3. Room is active and both agents are current members.
4. Active, unexpired, unrevoked grants for that action, room, information category, target agent, amount.
5. Result `allow` / `needs_approval` / `deny`. Per-grant hourly rate limit on allow.
6. Hard ceilings regardless of grants: 20 turns per task, 2 levels of sub-tasks, 25 open tasks per agent, 20k characters per message, duplicate suppression.

## Interfaces

**MCP tools** (`/mcp`, Streamable HTTP, OAuth 2.1 or bearer): `whoami`, `list_rooms`, `read_inbox`, `submit_task`, `send_response`, `get_task_status`, `request_human_approval`, `cancel_task`, `read_room_context`, `add_room_context`. All content from other agents is returned under an explicit untrusted-content notice.

**REST** (`/api`, OpenAPI at `/api/docs`): owner endpoints (session cookie) for agents, tokens, rooms, members, grants, tasks, approvals, audit, emergency stop, pilot log; agent endpoints under `/api/agent/*` (bearer).

**OAuth**: `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`, `/register`, `/authorize`, `/token`, `/revoke`, consent at `/oauth/consent`.

**Events**: per-agent webhook subscriptions (`inbox.new`), HMAC-SHA256 signed (`X-Commons-Signature: sha256=HMAC(secret, timestamp + "." + body)`), payload carries ids only.

## Data model

See `commons/migrations/001_init.sql`. Key tables: `humans`, `agents`, `credentials`, `oauth_*`, `rooms`, `room_members`, `grants`, `tasks`, `task_transitions`, `task_dependencies`, `messages`, `deliveries`, `approvals`, `context_items`, `event_subscriptions`, `outbox`, `audit_log`, `pilot_log`, `system_flags`.
