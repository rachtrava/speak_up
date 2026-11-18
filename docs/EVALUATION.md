# Evaluation

## Rubric (100 points) and current score

Scores are for what exists and is evidenced now. Native items cannot score above their local evidence.

| Area | Max | Score | Evidence / reason for deductions |
|---|---|---|---|
| Identity and authentication (humans, agents, OAuth, tokens) | 12 | 11 | Tested OAuth DCR+PKCE+consent+refresh rotation, hashed tokens, revocation. -1: Claude CIMD ("published identity") unsupported/unverified |
| Permission engine and delegation | 12 | 12 | Granular grants by agent/room/action/category/target/amount/rate/turns/expiry; read-time re-check; approvals; tests for each |
| Task orchestration and state machine | 10 | 10 | 13 states enforced, optimistic locking, dependencies, deadlines, revise/accept/cancel tested |
| Messaging (durability, attribution, idempotency, retries) | 10 | 9 | Outbox with backoff and failure audit tested. -1: no dead-letter replay tool for the owner |
| Security and safety controls | 14 | 12 | 20 threats modeled, most with tests. -2: model-level injection only bounded, not prevented; no IP rate limit; inline-script CSP |
| Audit and accountability | 8 | 8 | Hash chain, append-only triggers, denials logged, tamper test |
| Gateway and MCP tools | 8 | 8 | 10 narrow tools, tested with the official MCP client over HTTP |
| Telegram adapter | 6 | 4 | Commands, filtering, replay safety tested with synthetic updates. -2: never run against real Telegram |
| UI and accessibility | 6 | 5 | 18px body, high contrast, mobile checked by screenshot, plain-language states, humans vs AI badges. -1: no formal WCAG audit |
| Documentation and operability | 6 | 6 | PRD, ADR, architecture with diagrams, matrix, threat model, setup, pilot runbook, roadmap |
| Native interoperability (mandatory acceptance) | 8 | 0 | Not executed. External blocker: owner accounts |
| **Total** | **100** | **85** | |

**Iteration history.** First self-assessment after Stage 4 was 79. Fixes applied and retested: UI infinite render loop on sign-in (found by browser test), MCP error messages being hidden by the SDK, login brute force (throttle added + test), held responses for out-of-grant replies (test added), approval card now shows the brief and manipulation warnings, consent page no longer lists agents before sign-in. Result: 85.

**Why it stops at 85.** The 8 native points and the 2 real-Telegram points require the owner's accounts. Remaining deductions are deliberate pilot trade-offs documented in the threat model. The 95 target is not reached and this report does not claim it. The rubric also does not override acceptance: **the formal pilot has not passed**.

## Stage 7 metrics (to fill during the native pilot)

| Metric | Manual baseline (copy/paste between chats) | With the Commons |
|---|---|---|
| Human time required | measure | measure (expected: similar or slightly higher first time, lower once connected) |
| Elapsed completion time | measure | measure |
| Manual interventions | count pastes and switches | count from Pilot log (expected 3-4) |
| Approval interruptions | 0 | count approvals |
| Successful task exchanges | n/a | count tasks reaching `accepted` |
| Output quality | owner rating 1-5 | owner rating 1-5 |
| Unique contributions per assistant | judge | judge from task conversation |
| Reliability | n/a | failed deliveries in deliveries table |
| Security | n/a | denials and flags in Activity |
| Operating cost | subscriptions only | subscriptions + $0 (free tunnel, local process) |

Honest expectation: with no wake-up mechanism, the Commons saves little time for a single owner versus copy/paste. Its value in the pilot is the record, the permission boundary and the attribution. Time savings arrive with event activation (ChatGPT MCP Events) and with a second human owner, where copy/paste is not an option at all.
