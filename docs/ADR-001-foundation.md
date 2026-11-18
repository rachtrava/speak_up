# ADR-001: Foundation of the Commons Core

Status: accepted (2026-11-18). Reversible decisions are marked.

## Context
A single-owner pilot connecting the owner's existing ChatGPT and Claude through provider-supported connectors, with Telegram as the visible room. The brief asks for the simplest defensible system that is secure, auditable and replaceable.

## Decisions

1. **One Python process, one port.** MCP endpoint, OAuth endpoints, REST API, Telegram webhook and web UI are served by the same Starlette/FastAPI app. *Why:* fewest moving parts; nothing in the pilot needs a second service. *Reversible:* the Core is a plain class; adapters can be split out later.

2. **SQLite instead of PostgreSQL for the pilot.** WAL mode, foreign keys on, ordered SQL migrations in `commons/migrations`. *Why:* one owner, low volume, zero ops, file backup. The brief prefers PostgreSQL; we deviate on purpose. *Exit:* the schema is portable SQL (only `AUTOINCREMENT` and `datetime('now')` need edits). Move when there are multiple owners, concurrent writers, or a hosted deployment. *Reversible.*

3. **The official MCP SDK (mcp 2.x) provides MCP transport and OAuth 2.1 endpoints.** We implement only the storage-backed provider and the consent page. *Why:* do not hand-roll security protocol code.

4. **Identity comes from credentials, never from content.** Every token (static API token or OAuth access token) maps to exactly one agent. Tokens are 256-bit random values stored as SHA-256 hashes. MCP tools never accept a caller agent id. OAuth connections are bound to an agent by the owner on the consent page.

5. **Authorization comes only from human grants plus hard ceilings.** The permission engine is called on every agent operation, including at read time (so revocation is immediate). Five autonomy levels are presets over granular actions. Out-of-grant requests become approval requests; unapprovable ones are denied.

6. **Durable outbox, no message broker.** State change, audit record and outbound events (Telegram, webhooks) commit in one transaction; a background loop dispatches with exponential backoff. *Reversible* to a real queue later.

7. **Delivery and retrieval are different facts.** `deliveries` rows record inbox placement, Telegram posts and webhooks separately; `retrieved_at` is set only when the recipient agent itself reads its inbox. Task states `delivered` and `retrieved` mirror this.

8. **Hash-chained append-only audit ledger.** SQLite triggers block UPDATE/DELETE; a hash chain detects tampering by anyone with raw file access.

9. **Telegram is a presentation and owner-command adapter.** It is never the dispatcher. Bot updates are ignored, unknown senders are ignored, `update_id` is deduplicated, plain text never creates tasks.

10. **Simulated agents are first class and loudly labeled.** `connection_type='simulated'`, `[SIMULATED]` in every label, a banner on every message, `simulated: true` in every audit record, and they cannot be bound to an OAuth connection.

## Consequences
- The pilot runs on a laptop plus a free tunnel. No paid dependencies.
- Native activation limits (no server-initiated wake for Claude; ChatGPT MCP Events unverified) are product constraints we document, not engineer around with substitute bots.
- One process means a crash stops everything. Acceptable for a pilot; restart is safe because all state is durable.
