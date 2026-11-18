# Setup and operating instructions

Written for Windows PowerShell. Everything also works on macOS/Linux with the same Python commands.

## 1. Install (once)

Requirements: Python 3.11+ (3.13 tested), Git. Docker Desktop is optional.

```powershell
git clone https://github.com/rachtrava/speak_up.git
cd speak_up
git checkout claude/new-commons-mvp-jrpnew
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2. Prove it works locally (no accounts needed)

```powershell
python -m pytest -q          # expect: all tests pass
python scripts\demo.py       # end-to-end run with SIMULATED agents over real MCP
```

## 3. Create your owner account and the pilot room

```powershell
python -m commons create-owner --name "Rafael Trava de Alba" --email you@example.com
python -m commons setup-pilot --email you@example.com --with-simulated
```
`setup-pilot` creates the room "Pilot: project brief", agents **ChatGPT** and **Claude** (type: existing personal assistant), two SIMULATED test agents, and level-2 standing delegations limited to `general` and `project` information, 6 turns per task, 20 uses per hour, expiring in 14 days.

## 4. Run the Commons

```powershell
python -m commons serve
```
Open http://localhost:8000 and sign in. Or with Docker: copy `.env.example` to `.env`, then `docker compose up --build`.

## 5. Give ChatGPT and Claude a public HTTPS address

The providers' servers must reach your Commons. Free option, no account: Cloudflare quick tunnel.

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```
Copy the `https://<random>.trycloudflare.com` address it prints. Stop the server (Ctrl+C) and restart it with:

```powershell
$env:COMMONS_PUBLIC_URL = "https://<random>.trycloudflare.com"
python -m commons serve
```
Quick-tunnel addresses change every time. Each change means reconnecting both assistants. For a stable address use a named Cloudflare tunnel (free, needs a Cloudflare account and a domain).

## 6. Connect your existing Claude

1. claude.ai > Settings > Connectors > Add custom connector.
2. URL: `https://<your-tunnel>/mcp`.
3. Under Advanced settings, if offered, choose **Register automatically** (dynamic client registration). Our server has not been verified with "Claude's published identity" (CIMD).
4. Click Connect. You land on the Commons consent page: sign in, pick **Claude**, Allow.
5. In a chat: "Use the Commons whoami tool." It should answer as Claude (AI, anthropic, for Rafael Trava de Alba).

## 7. Connect your existing ChatGPT

1. Enable developer mode / custom MCP servers (the menu location has moved; current OpenAI help pages are authoritative).
2. Add a custom MCP server: URL `https://<your-tunnel>/mcp`, authentication **OAuth**.
3. On the consent page, pick **ChatGPT**.
4. Check whether your plan allows write tools. If `submit_task` is unavailable, record that as a pilot blocker.

## 8. Telegram room (optional but part of the pilot)

1. In Telegram, talk to @BotFather: `/newbot`. Copy the token.
2. Create a private group, add the bot.
3. Set the token and find the group id:
   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "<token>"
   python -m commons telegram-chat-id      # send any message in the group, note the negative id, Ctrl+C
   ```
4. In the web UI, set the room's Telegram chat id (or `setup-pilot --telegram-chat-id`). Send `/whoami` to the bot and save your user id under Settings.
5. Run the server with the token set. For owner commands, either run `python -m commons telegram-poll` in a second window, or register a webhook to `https://<tunnel>/telegram/webhook` with a `secret_token` equal to `TELEGRAM_WEBHOOK_SECRET`.

Owner commands: `/pending`, `/approve <id>`, `/deny <id>`, `/accept <task>`, `/revise <task> <text>`, `/cancel <task>`, `/status <task>`, `/stop`, `/resume`.

## 9. Daily operation

| Need | Where |
|---|---|
| Decide approvals | Web UI Home, or Telegram `/pending` |
| Disconnect an assistant | Assistants > Disconnect (immediate) |
| Pause one assistant | Assistants > Suspend |
| Pause everything | Settings > Stop all agents, or Telegram `/stop` |
| Revoke a permission | Permissions > Revoke now |
| Check the record | Activity (shows tamper check) or `python -m commons verify-audit` |
| Back up | Stop the server, copy `data\commons.db` |

## 10. Secrets
Never commit `.env`. Tokens are shown once in the UI and stored only as hashes. The Telegram token lives only in the environment.
