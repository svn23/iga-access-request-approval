# AccessMate Teams Bot API

> ⚠️ **Status: Under active development (v0.1.0).** APIs, schemas, and environment variables may change without notice. Not production-hardened — do a security review before any production deployment.

AccessMate is a FastAPI backend that turns Microsoft Teams into an identity access-request and approval workflow. A user asks for access in Teams, the bot gathers the required parameters (optionally with an LLM), routes an Adaptive Approval Card privately to the user's manager, and on approval provisions the access through a MidPoint connector (Java JAR) or an MCP server.

For architecture, all API endpoints, JAR integration, and testing, see **[DEV.md](DEV.md)**.

---

## ✨ Features

- **Teams webhook backend** — single `POST /api/messages` entry point for all Bot Framework activities (messages + Adaptive Card submissions).
- **Conversational access requests** — detects intent, gathers missing parameters (`system`, `role`, `reason`) turn-by-turn.
- **Optional AI mode** (`ENABLE_AI`) — LLM-based intent parsing and parameter extraction (Gemini / OpenAI / Anthropic / Groq). Falls back to keyword matching when disabled or unavailable.
- **Proactive manager routing** — resolves the manager via Microsoft Graph (Entra ID) or the local directory, and delivers the approval card to the manager's private 1:1 chat (hidden from the requester).
- **Approval safety** — blocks self-approval and double-processing of already approved/rejected requests.
- **Provisioning integration** — forwards approved requests to a MidPoint connector (`midpoint` mode) or MCP server (`mcp` mode) for permanent or temporary (≤30 day) access.
- **Catalog sync** — pulls available roles/systems from MidPoint every `CATALOG_SYNC_MINUTES` (default 30), with a built-in fallback catalog.
- **Structured JSON logging** — request-correlated logs with rotation.

---

## 🚀 Quick Start

### 1. Virtual environment
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 2. Install dependencies
```powershell
pip install -r requirements.txt
```

### 3. Configure `.env`
Copy `.env.example` to `.env` and fill in credentials:
```powershell
Copy-Item .env.example .env
```
Minimum required keys:
```env
TEAMS_APP_ID=your_teams_app_id
TEAMS_APP_PASSWORD=your_teams_app_password
TEAMS_APP_TENANT_ID=your_tenant_id
MCP_URL=http://your-mcp-host:8000/       # if PROVISIONING_MODE=mcp
MIDPOINT_BASE_URL=https://your-midpoint  # if PROVISIONING_MODE=midpoint
MIDPOINT_API_TOKEN=your_bot_connector_token
ENABLE_AI=false                          # set true + LLM_PROVIDER + API key for AI mode
```
See [DEV.md → Configuration](DEV.md#configuration) for the full variable list.

### 4. Run
```powershell
.venv\Scripts\python -m app.main
```
Server starts on `http://localhost:8000`. Verify:
```powershell
Invoke-WebRequest http://localhost:8000/api/health -UseBasicParsing
# {"status":"healthy","version":"0.1.0"}
```

### 5. Expose to Teams (dev)
```bash
ngrok http 8000
```
Put the HTTPS host in `manifest.json` → `validDomains`, and point the bot messaging endpoint at `https://<ngrok-host>/api/messages`.

---

## 🗂️ Project Layout

```
teams-bot-main/
├── app/
│   ├── main.py                # FastAPI app, lifespan, logging middleware
│   ├── config.py              # Pydantic settings (.env)
│   ├── logging_config.py      # Structured JSON logging
│   ├── api/
│   │   ├── router.py          # Aggregates routers under /api
│   │   └── endpoints/
│   │       ├── bot.py         # POST /api/messages (Teams webhook)
│   │       ├── health.py      # GET  /api/health
│   │       └── requests.py    # GET  /api/requests/{email}/status, POST /api/notifications/send
│   └── services/              # bot_handler, teams, graph, mcp, catalog, db, bot_auth
├── midpoint-bot-connector/    # Java MidPoint connector (JAR) — see its own README
├── manifest.json              # Teams app manifest
├── requirements.txt
├── .env.example
├── README.md                  # this file
└── DEV.md                     # developer guide (endpoints, JAR, testing)
```

---

## 📋 Version

| Component            | Version |
|---------------------|---------|
| AccessMate Bot API  | 0.1.0   |
| Teams manifest      | 1.0.0 (manifestVersion 1.16) |
| MidPoint Connector  | 1.0.0   |

---

## 📄 License

Internal / proprietary — 1CentroXY. Not for external distribution.
