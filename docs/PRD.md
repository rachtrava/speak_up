# Product requirements: The New Commons MVP

## Problem
People already have capable personal assistants, each locked in its own ecosystem. Coordinating them today means the human copies text between chat windows. "Tell your agent to contact my agent" has no trustworthy plumbing: no shared identity, no permission model, no record of who agreed to what.

## Users
- **Owner** (pilot: Rafael Trava de Alba). Non-engineer comfort with settings pages and chat. Wants control without babysitting.
- **AI representatives**: the owner's existing ChatGPT and Claude, connected through provider connectors.
- Later: other owners, households, teams, communities.

## Pilot goal
Two of one owner's existing assistants exchange one low-sensitivity text task through the Commons, visibly in Telegram, under standing permissions, with the owner deciding the outcome.

## Requirements (MVP)

| ID | Requirement | Status |
|---|---|---|
| R1 | Owner account; owner registers agents with an explicit connection type | Implemented, tested |
| R2 | Each assistant connects with its own credential bound to one agent (OAuth for native assistants) | Implemented, tested locally. Native: pending |
| R3 | Rooms with purpose, members (humans and AI clearly distinguished), Telegram link, turn and retention policy | Implemented, tested |
| R4 | Granular, expiring, revocable delegations by agent, room, action, information category, target, amount, rate, turns | Implemented, tested |
| R5 | Anything outside a delegation becomes an owner approval request; message content never changes authorization | Implemented, tested |
| R6 | Task lifecycle with 13 enforced states; delivery separate from retrieval | Implemented, tested |
| R7 | Durable inboxes, attribution, correlation by task, idempotency, duplicate suppression, retries | Implemented, tested |
| R8 | MCP tools: list rooms, read inbox, submit task, send response, task status, request approval, cancel (plus whoami, room context) | Implemented, tested over real MCP |
| R9 | Telegram mirror with attribution; owner commands; loop and replay safety | Implemented, tested with synthetic updates. Native: pending |
| R10 | Accept, revise, cancel by the owner (web and Telegram) | Implemented, tested |
| R11 | Append-only, tamper-evident audit of every action, denial and decision | Implemented, tested |
| R12 | Emergency stop; rate, turn, depth, size limits | Implemented, tested |
| R13 | Simulated agents clearly separated from native ones | Implemented, tested |
| R14 | Pilot log of manual activations | Implemented (automatic entry on OAuth connect; manual entries via UI) |
| R15 | Accessible web UI: 18px body, high contrast, mobile, plain language | Implemented; visually checked desktop and mobile. No formal WCAG audit |

## Out of scope for MVP
Multiple owners per room, external service actions (email, calendar, payments), agent discovery across Commons, mobile apps, federation, A2A transport.

## Missing requirements identified during build
1. **Activation gap.** The brief's step 6 ("Claude retrieves") assumes Claude can be prompted by the system. It cannot today: the owner must open Claude. The product must make this cheap (one sentence) and measure it.
2. **Plan eligibility.** ChatGPT write tools may depend on plan tier. This is an account-level dependency to confirm before the pilot.
3. **Public URL.** Native connectors need HTTPS reachability. A free tunnel works but its address changes; a stable domain is a practical requirement for anything past the pilot.
4. **Approval context.** Owners need to see what they approve, including manipulation warnings. Added.
5. **Held responses.** Disclosure control must apply to replies, not only to new tasks. Added (`release_message` approvals).
