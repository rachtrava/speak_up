# Pilot demonstration and acceptance report

Date: 2026-11-18. Environment: cloud development container (Linux, Python 3.13, MCP SDK 2.3.0). No access to the owner's ChatGPT, Claude or Telegram accounts.

## Verdict

| Scope | Result |
|---|---|
| Local MVP with SIMULATED agents over real MCP + OAuth machinery | **PASS** (evidence below) |
| Formal native interoperability pilot (owner's real ChatGPT and Claude) | **NOT RUN.** Blocked on owner-held accounts, a public HTTPS URL, and a Telegram bot token. No success is claimed |

## Evidence for the local run

`python -m pytest -q` → 60 passed. `python scripts/demo.py` output (abridged, actual run):

```
[1] connection acts as: 🤖 Sim Requester (AI, simulator, for Rafael Trava de Alba) [SIMULATED]
[3] Core authenticated, authorized and stored it. State: delivered (In the assistant's inbox, not opened yet)
    retry with same idempotency key returns same task: True
[5] Telegram mirror: ["skipped", "skipped"]  (no Telegram chat configured in this demo)
[6] from: 🤖 Sim Requester (...) [SIMULATED]   state now: retrieved
[7] state now: result_ready
[10] final state: accepted - Done: you accepted the result
Transitions: created -> authorized -> queued -> delivered -> retrieved -> result_ready -> accepted
Audit chain: {'valid': True, 'records_checked': 34}
```

## Acceptance steps against the brief

| Step | What the brief requires | Local (simulated) | Native |
|---|---|---|---|
| 1 | Owner instructs ChatGPT | Scripted | Pending: owner types it in ChatGPT |
| 2 | ChatGPT submits via its Commons connection | PASS (MCP `submit_task`, own token) | Pending; depends on ChatGPT write-tool eligibility |
| 3 | Core authenticates, validates, stores | PASS (tests + audit) | Same code path |
| 4 | Task in Claude's authenticated inbox | PASS (`delivered`, `retrieved_at` null) | Same code path |
| 5 | Attributed message in Telegram | Logic PASS with synthetic updates; real send not run | Pending bot token |
| 6 | Claude retrieves in its own environment | PASS (simulated agent) | Pending; **requires manual activation** |
| 7 | Claude submits independent analysis | PASS (rule-based stand-in, labeled) | Pending |
| 8 | Core stores and notifies ChatGPT | Stored + inbox delivery PASS; webhook event PASS (signed, ids only) | Notification to ChatGPT: **not available** until an MCP Events adapter exists and is verified |
| 9 | ChatGPT retrieves and synthesizes | PASS (simulated) | Pending; **manual activation** |
| 10 | Owner accepts/revises/cancels | PASS (core, REST, Telegram command logic) | Pending |

## Expected manual activations in the native pilot

1. Owner approves each OAuth connection once (recorded automatically in the pilot log).
2. Owner prompts ChatGPT to start (inherent to the scenario, not a gap).
3. Owner prompts Claude to check its Commons inbox (gap: no provider wake-up mechanism).
4. Owner prompts ChatGPT to check for Claude's reply (gap until MCP Events works).
5. Possibly: per-tool confirmation clicks in ChatGPT/Claude for write tools (provider UX).

## Native pilot runbook (owner, about 45 minutes)

1. Follow `SETUP.md` sections 1 to 8.
2. In ChatGPT: "Use the Commons. List my rooms, then ask my Claude to critique this brief: <paste brief>. Use info category project." Record step in Pilot log.
3. Check Telegram for the attributed task and the web UI for `In the assistant's inbox, not opened yet`.
4. In Claude: "Check my Commons inbox and give an independent analysis of the task, then send your response." Record.
5. In ChatGPT: "Check my Commons inbox for Claude's reply and synthesize both views." Record.
6. Accept, revise or cancel in the web UI or with `/accept <task_id>` in Telegram.
7. Export evidence: Activity page (tamper check), task detail (transitions + deliveries), Pilot log. The audit entries for the native run must show `via: oauth_access` and the OAuth client ids registered from chatgpt.com and claude.ai redirect URIs. That is the evidence the assistants, not simulators, did the work.

Pass criteria: steps 2, 6, 7, 9 executed by the native assistants over their own OAuth connections; audit chain valid; no simulated agent involved in that task.
