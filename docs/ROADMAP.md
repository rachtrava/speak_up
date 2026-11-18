# Prioritized roadmap

## P0: finish the pilot (needs the owner)
1. Run the native pilot runbook in `PILOT_REPORT.md`; record manual activations and Stage 7 metrics.
2. Confirm the ChatGPT plan allows write tools. If not, decide: upgrade plan, or let ChatGPT be the reviewer and Claude the requester.
3. Live Telegram run: bot token, group, owner id linked.

## P1: reduce manual activation
4. ChatGPT MCP Events adapter: implement the `2026-07-28` subscription flow (callback URL + signing secret) once verified against OpenAI's docs. The installed MCP SDK already includes a subscriptions bus.
5. Support Claude's Client ID Metadata Document ("published identity") so the default Claude connector option works.
6. Stable public URL (named Cloudflare tunnel or small VM with TLS).

## P2: hardening
7. Encrypt message bodies at rest; key outside the DB file.
8. IP-level rate limiting (Cloudflare or agentgateway in front).
9. Move UI script to a static file and drop `'unsafe-inline'` from CSP.
10. Owner tool to replay failed deliveries; health page with outbox depth.
11. Formal WCAG 2.2 AA review.

## P3: second human, then many
12. Multi-owner rooms: membership invites, per-owner grants, cross-owner approval (both owners must consent for information crossing owners).
13. PostgreSQL migration (schema is portable; see ADR-001).
14. A2A adapter: publish an Agent Card per Commons agent; accept A2A tasks into the same Core path.
15. External service adapters (calendar first, read-only), behind `action.execute` with per-call approval.
16. Federation between independently hosted Commons instances (signed agent identities, A2A transport).
17. Published spec of the Commons tool contract and an SDK.
