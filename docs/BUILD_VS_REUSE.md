# Build versus reuse assessment

## Candidates

| Project | What it is | License | Fit for "connect my existing ChatGPT and Claude" |
|---|---|---|---|
| OpenAgents | "Collaboration OS for AI agents": Workspace, Launcher for coding agents, network SDK with mods; claims MCP and A2A support. About 4.4k stars, no formal releases | Apache-2.0 | Weak. Its agents are processes it launches or SDK clients. It does not address consumer assistants that can only act as MCP clients inside their own apps. Its permission model is not built around human-granted, revocable delegations |
| agentgateway | Rust proxy for MCP/A2A traffic: OAuth/JWT/API keys, CEL RBAC, rate limits, OpenTelemetry. Linux Foundation, about 6.3k stars, "active development" | Apache-2.0 | Good **in front of** a Commons later (TLS, rate limits, policy). It is a gateway, not a task/permission/approval system. Adds a second runtime and YAML policy |
| A2A | Agent-to-agent protocol: Agent Cards, tasks, JSON-RPC, SSE, push | Apache-2.0 | Right for future federation and agent servers. Consumer assistants do not speak it, so it cannot carry the pilot |
| LangGraph | In-process orchestration of LLM workflows | MIT | Wrong layer. It orchestrates model calls you run yourself, which is the "standalone API bot" substitution the brief forbids |
| MCP Python SDK | Official server/client, Streamable HTTP, full OAuth 2.1 authorization-server scaffolding | MIT | Strong. Gives us spec-compliant MCP and OAuth endpoints for free |

## Three approaches compared

| Criterion | 1. Adopt a platform (OpenAgents) | 2. Reuse components | 3. Minimal independent core |
|---|---|---|---|
| Native assistant compatibility | Unproven; built for launched agents | Depends on core | Built around MCP-client assistants from day one |
| Security model fit | Would need its permission model replaced | Good: proven OAuth/MCP plumbing | Must write it, but small and fully tested |
| Interoperability | MCP/A2A claims | MCP spec compliance from SDK | Same as 2, plus adapters |
| Maintainability | Large codebase to track | Small core plus pinned SDK | Small, but we own everything |
| Licensing | Apache-2.0 | MIT/Apache | n/a |
| Cost | Free, heavier infra | Free | Free |
| Dev complexity for pilot | High (learn, then bend) | Low | Medium |

## Decision: approach 2

Build a small **Commons Core** (identity, permissions, task state machine, broker, audit, approvals) ourselves, because no candidate implements human-granted, revocable delegation with approval routing. **Reuse** the official MCP SDK for the protocol and the OAuth 2.1 endpoints (registration, PKCE, token, revocation, metadata), so we do not hand-roll security-critical protocol code. Keep agentgateway and A2A as planned additions once there is more than one owner or more than one Commons instance.

What we deliberately did not build: our own OAuth protocol handlers, our own MCP transport, a message queue server, a model runtime.
