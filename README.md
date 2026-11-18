# The New Commons

Provider-neutral infrastructure that lets your existing AI assistants (ChatGPT, Claude, others) work together for you, under permissions you grant, with every action attributed and recorded.

> "Tell your agent to contact my agent."

## Status at a glance

| Capability | Designed | Implemented | Tested (automated) | Externally verified |
|---|---|---|---|---|
| Commons Core (identity, permissions, tasks, broker, audit, safety) | yes | yes | yes | n/a |
| MCP gateway with OAuth 2.1 (DCR, PKCE, consent, refresh rotation) | yes | yes | yes, official MCP client over HTTP | **no** |
| Native ChatGPT connection | yes | yes (standard MCP + OAuth) | n/a | **no, pending owner accounts** |
| Native Claude connection | yes | yes (standard MCP + OAuth, DCR) | n/a | **no, pending owner accounts** |
| Telegram room (attributed mirror, owner commands) | yes | yes | yes, synthetic updates | **no, pending bot token** |
| Web interface (owner) | yes | yes | browser screenshots, desktop + mobile | n/a |
| Simulated agents (labeled) | yes | yes | yes | n/a |
| Signed webhooks for agent events | yes | yes | yes | no |
| ChatGPT MCP Events wake-up | yes | **no** | no | no |
| Docker deployment | yes | yes | **not built here** (sandbox proxy blocked pip inside Docker) | no |

**The formal interoperability pilot has not been run.** See `docs/PILOT_REPORT.md`.

## Quick start (Windows PowerShell)

```powershell
py -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest -q                 # 60 tests
python scripts\demo.py              # full round trip with SIMULATED agents over real MCP
python -m commons create-owner --name "Rafael Trava de Alba" --email you@example.com
python -m commons setup-pilot --email you@example.com --with-simulated
python -m commons serve             # http://localhost:8000
```
Connecting your real assistants and Telegram: `docs/SETUP.md`.

## Documents

| Deliverable | File |
|---|---|
| Product requirements | `docs/PRD.md` |
| Architecture, sequence and state diagrams, API surface | `docs/ARCHITECTURE.md` |
| Architectural decision record | `docs/ADR-001-foundation.md` |
| Native assistant compatibility matrix | `docs/COMPATIBILITY_MATRIX.md` |
| Build versus reuse assessment | `docs/BUILD_VS_REUSE.md` |
| Threat model | `docs/THREAT_MODEL.md` |
| Setup and operating instructions | `docs/SETUP.md` |
| Pilot demonstration and acceptance report | `docs/PILOT_REPORT.md` |
| Evaluation rubric and score | `docs/EVALUATION.md` |
| Roadmap | `docs/ROADMAP.md` |
| Schema and migrations | `commons/migrations/` |
| REST API reference | `/api/docs` on a running server |

## Code map

```
commons/
  core.py          Commons Core: the single authoritative dispatcher
  permissions.py   permission engine (grants, levels, approvals)
  statemachine.py  task lifecycle and legal transitions
  audit.py         hash-chained append-only ledger
  security.py      limits, screening, emergency stop
  events.py        outbox dispatcher: Telegram + signed webhooks, retries
  oauth.py         OAuth provider + consent page (binds a connection to one agent)
  mcp_tools.py     MCP tools for assistants
  api.py           REST API (owner + agent)
  telegram.py      Telegram adapter (presentation + owner commands)
  simulator.py     SIMULATED test agents
  app.py           composition: one process, one port
  web/index.html   owner interface
tests/             state machine, flows, security boundaries, OAuth + MCP end to end
```
