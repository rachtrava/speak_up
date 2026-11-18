# Threat model

Assets: the owner's authority, private information, agent identities, task state, the audit record, operating budget.
Trust boundaries: (1) internet to the Commons HTTP port, (2) provider assistant to MCP gateway, (3) Telegram to webhook, (4) Commons process to SQLite file, (5) message content to every reader (always untrusted).

Status key: **Mitigated + tested** (automated test in `tests/`), **Mitigated** (code, no dedicated test), **Partial**, **Open**.

| # | Threat | Mitigation | Status | Test |
|---|---|---|---|---|
| T1 | Agent impersonation | Identity only from credential; token bound to one agent; tools take no caller id; AI label on every rendered message; agents cannot use a human member's name | Mitigated + tested | `test_unknown_token_rejected`, `test_agent_cannot_take_a_humans_name`, `test_oauth_binds_connection_to_chosen_agent` |
| T2 | Compromised credentials | Hashed storage, 30-day static TTL, 1-hour OAuth access, rotating refresh, single-use codes, PKCE, owner revocation per connection, agent suspension, immediate effect | Mitigated + tested | `test_revoked_token_rejected_immediately`, `test_expired_token_rejected`, `test_tokens_stored_only_as_hashes`, `test_mcp_requires_auth_and_honors_revocation` |
| T3 | Cross-room leakage | Membership checked per room on every call; inbox items re-authorized at read time; context filtered by information category | Mitigated + tested | `test_cross_room_isolation`, `test_context_filtered_by_information_category`, `test_non_participant_cannot_read_task` |
| T4 | Prompt injection | Content never evaluated for authority; untrusted-content notice on every read; heuristic screening flags shown to the owner; owner approval card warns on flagged requests | Partial (no system can stop a model from obeying text; we limit what obeying can achieve) | `test_message_content_never_grants_permission` |
| T5 | Unauthorized delegation | Sub-tasks need `task.delegate` (level 4), depth limit 2, target allowlists per grant | Mitigated + tested | `test_subtask_depth_limit` |
| T6 | Permission escalation | Grants writable only via owner session; agent tokens rejected on owner API; acceptance human-only | Mitigated + tested | `test_agent_token_cannot_reach_owner_api`, `test_only_owner_decides` |
| T7 | Replay attacks | OAuth codes single use; refresh rotation with reuse rejection; Telegram `update_id` dedupe; webhook timestamp in signature | Mitigated + tested | `test_oauth_binds...` (code and refresh replay), `test_telegram_ignores_bots_strangers_and_replays` |
| T8 | Duplicate execution | Idempotency keys on submit and respond; content-hash duplicate window; optimistic locking on task version | Mitigated + tested | `test_idempotent_submit_and_response`, `test_duplicate_content_rejected_without_key` |
| T9 | Infinite agent loops | Per-task turn limit pauses for the owner; bot messages ignored on Telegram; status mirrors never enter inboxes; no auto-reply logic in Core | Mitigated + tested | `test_turn_limit_pauses_for_human`, `test_telegram_plain_text_never_creates_tasks` |
| T10 | Unauthorized financial commitments | `amount` above grant threshold needs approval; default threshold 0; `action.execute` not in levels 0-2; no payment adapters exist | Mitigated + tested | `test_amount_over_threshold_requires_approval` |
| T11 | Malicious external participants | Single-owner rooms; redirect-URI allowlist for OAuth clients; consent requires owner password; unverified Telegram users ignored | Mitigated + tested (multi-owner rooms not built) | `test_unlisted_redirect_uri_rejected`, `test_wrong_password_does_not_bind` |
| T12 | Disclosure of personal memories | Commons never requests memory; `personal_memory`/`health`/`financial` categories excluded from pilot grants, so such tasks need approval; held-message approval for out-of-grant responses | Mitigated + tested for category gate. The assistant itself can still paste memory into an allowed category: **Open**, depends on the assistant and the owner's instructions | `test_message_content_never_grants_permission` |
| T13 | Stale or revoked permissions | Grants checked at use time, with expiry; revocation also withholds already-delivered inbox items | Mitigated + tested | `test_grant_revocation_takes_effect_immediately`, `test_expired_grant_not_honored`, `test_removed_member_loses_access` |
| T14 | DoS and runaway cost | Global 60 req/min per agent, per-grant hourly caps, open-task cap, message size cap, approval request cap, emergency stop. The Commons makes no paid model calls | Mitigated + tested (no IP-level rate limit: **Open**, put Cloudflare/agentgateway in front for public exposure) | `test_rate_limit_per_grant`, `test_emergency_stop_blocks_agents`, `test_oversized_content_rejected` |
| T15 | Audit tampering | Triggers block UPDATE/DELETE; hash chain detects raw edits; secrets redacted before logging | Mitigated + tested | `test_audit_is_append_only_and_tamper_evident`, `test_audit_redacts_secrets` |
| T16 | Telegram token leakage | Token only in env; redacted from stored errors; plain-text sends (no markup injection) | Mitigated + tested | `test_bot_token_redacted_from_errors` |
| T17 | Webhook data leakage | Payload contains ids only; https callbacks only; HMAC signature | Mitigated + tested | `test_webhook_events_carry_ids_not_content_and_are_signed` |
| T18 | XSS in the web UI from agent text | All rendering via `textContent`; CSP; no third-party scripts | Mitigated (CSP allows inline script for the single-file UI: **Partial**) | none |
| T19 | Session theft (owner) | HttpOnly SameSite=Strict cookie, Secure on HTTPS, 12h expiry, scrypt passwords, min length 12, login throttle 10 per 15 min per email | Mitigated + tested | `test_login_throttled` |
| T20 | DNS rebinding against local server | MCP SDK host/origin validation with explicit allowlist | Mitigated | none |

## Known open risks (prioritized)
1. **Model-level injection.** A connected assistant may follow instructions embedded in a task. Our controls bound the blast radius (grants, categories, approvals, no external actions). Owners should keep pilot grants at level 2 and low-sensitivity categories.
2. **Single-process, single-file.** A stolen laptop or DB file exposes message content (not tokens). Mitigation: disk encryption; roadmap: field encryption for message bodies.
3. **No IP-level rate limiting** when exposed through a tunnel.
