# AccessMate — Developer Guide

Consolidated developer reference: architecture, configuration, every exposed API (Python bot + Java JAR), request flow, AI toggle, logging, and testing. For quick-start / run instructions see [README.md](README.md).

- **Version:** Bot API `0.1.0` · Teams manifest `1.0.0` · MidPoint Connector `1.0.0`
- **Status:** Under active development — nothing here is API-stable.

---

## Table of Contents
1. [Consoles & Portals](#consoles--portals)
2. [Architecture](#architecture)
3. [Configuration](#configuration)
4. [Python Bot API (exposed)](#python-bot-api-exposed)
5. [Java MidPoint Connector API (the JAR)](#java-midpoint-connector-api-the-jar)
6. [Request / Approval Flow](#request--approval-flow)
7. [AI Toggle](#ai-toggle)
8. [Proactive Manager Routing & Approval Validation](#proactive-manager-routing--approval-validation)
9. [Logging](#logging)
10. [Testing](#testing)
11. [Deployment](#deployment)

---

## Consoles & Portals

Registration and management surfaces for this bot:

| Purpose | URL |
|---------|-----|
| Teams Developer Portal (app registration, manifest, publish) | https://dev.teams.microsoft.com/apps |
| Bot Framework registration (this bot) | https://dev.botframework.com/bots?id=917722f2-c4f8-4361-8d2f-cfc41402099a |
| Azure Entra ID (app registration / secret / Graph permissions) | https://portal.azure.com → Entra ID → App registrations |

- **Bot / App ID:** `917722f2-c4f8-4361-8d2f-cfc41402099a`
- The **App ID** is both the Teams `botId` and the Entra ID client ID.
- The Entra **client secret** is the `TEAMS_APP_PASSWORD`.
- After changing the manifest, re-upload the app package in the Teams Developer Portal and update `validDomains` to the current public host (ngrok in dev).

---

## Architecture

```
┌──────────────────────────────────────────────┐
│                MICROSOFT TEAMS               │
│  Teams App (manifest.json) — bot, cards      │
└──────────────────────────────────────────────┘
        ↓ POST /api/messages (HTTPS)   ↑ replies via Bot Connector API
┌──────────────────────────────────────────────┐
│         ACCESSMATE BOT  (Python FastAPI)     │
│  • POST /api/messages   webhook              │
│  • POST /api/notify     (inbound from JAR)   │
│  • GET  /api/health                          │
│  • GET  /api/requests/{email}/status         │
│  • POST /api/notifications/send              │
│  Services: bot_handler, teams, graph, mcp,   │
│            catalog, db, bot_auth             │
└──────────────────────────────────────────────┘
     ↓                 ↓                    ↓
[1] MidPoint       [2] Microsoft Graph   [3] Database
    Connector          Entra ID              (SQLite / MySQL*)
    (Java JAR)         manager lookup        access requests,
    /api/v1/*                                directory, audit
```

\* The bot config defaults to SQLite (`SQLITE_DB_PATH`); some `db_service` queries use MySQL-style placeholders (`%s`). Confirm the active driver before deploying.

**Components**
- **Teams App** (`manifest.json`) — defines the bot in Teams; `botId` = App/Client ID; scopes personal/team/groupChat.
- **AccessMate Bot** (Python FastAPI) — receives activities, runs conversational logic, routes approvals, calls provisioning.
- **MidPoint Connector** (Java JAR, `midpoint-bot-connector/`) — provisions/revokes access, exposes the catalog, integrates with IAM (Keycloak, Okta, Entra, WSO2, …).
- **Microsoft Graph** — manager lookup and user profiles (Entra ID).
- **Database** — access requests (`bot_access_requests`), user directory (`bot_user_directory`), messages/audit.

---

## Configuration

All settings load from `.env` (see `app/config.py`). `.env` is intentionally tracked in this repo.

| Variable | Default | Purpose |
|----------|---------|---------|
| `TEAMS_APP_ID` | — | Bot / Entra client ID |
| `TEAMS_APP_PASSWORD` | — | Entra client secret |
| `TEAMS_APP_TENANT_ID` | — | Tenant ID (also used as proactive-conversation fallback) |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind address |
| `SQLITE_DB_PATH` | `data/accessmate.sqlite3` | SQLite DB file |
| `ENABLE_AI` | `false` | Toggle LLM mode |
| `LLM_PROVIDER` | `gemini` | `gemini` \| `openai` \| `anthropic` \| `groq` |
| `LLM_MODEL` | — | Optional model override |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GROQ_API_KEY` | — | Provider keys |
| `OPENAI_API_BASE` | — | Custom OpenAI-compatible base URL |
| `MCP_URL` | — | MCP server endpoint (mode `mcp`) |
| `PROVISIONING_MODE` | `midpoint` | `mcp` \| `midpoint` \| `disabled` |
| `MIDPOINT_BASE_URL` | — | MidPoint host, e.g. `https://midpoint.example.com:8443` |
| `MIDPOINT_API_URL` | — | Direct provisioning endpoint (alternative) |
| `MIDPOINT_API_TOKEN` | — | Bearer token for the JAR / MidPoint |
| `MIDPOINT_TLS_VERIFY` | `true` | Set `false` only for self-signed certs in dev |
| `TEAMS_SERVICE_URL` | — | Fallback Bot Framework serviceUrl for proactive DMs from inbound `/api/notify` events (region-stable, e.g. `https://smba.trafficmanager.net/in/`) |
| `CATALOG_SYNC_MINUTES` | `30` | Catalog refresh interval |
| `BOT_NAME` | `AccessMate` | Display name |
| `MAX_ACTIVITY_BYTES` | `65536` | Reject larger webhook payloads (413) |
| `VALIDATE_BOT_AUTH` | `true` | Enforce Bot Framework auth |
| `ALLOW_UNAUTHENTICATED_DEV` | `false` | Emulator-only bypass — **never in prod** |
| `AUTH_MODE` | `jwt` | `jwt` (Bot Framework JWT) \| `token` (static `TEAMS_APP_PASSWORD` bearer, for local testing) |

---

## Python Bot API (exposed)

Base path prefix: `/api`. All non-health routes call `validate_bot_framework_authorization` (JWT or static token per `AUTH_MODE`).

### `GET /api/health`
Liveness probe. No auth.
```json
{ "status": "healthy", "version": "0.1.0" }
```

### `POST /api/messages`
Microsoft Teams / Bot Framework webhook. Accepts any activity (message or Adaptive Card `Action.Submit`), dispatches to `app.services.bot_handler.process_activity`.
- Rejects payloads over `MAX_ACTIVITY_BYTES` → `413`.
- Body must be a JSON object with a string `type` → else `400`.
- Activity bodies are **not** logged (may contain PII / access data).
- Returns `{"status":"success"}` on handled activities; `500` on handler exceptions.

### `GET /api/requests/{email}/status`
Returns the latest ≤10 access requests for a requester email.
```json
{
  "status": "accepted",
  "email": "jane@company.com",
  "requests": [
    {
      "requestId": 123, "resource": "AWS", "role": "Developer",
      "targetType": "resource", "targetName": "AWS Production",
      "requestStatus": "approved", "approver": "Kumar Gourab",
      "accessUntil": null, "timestamp": "2026-07-15T19:00:00"
    }
  ]
}
```

### `POST /api/notify`  (inbound from the MidPoint connector JAR)
The JAR forwards midPoint access-request lifecycle events here; the bot DMs the right person a card. **Not** Bot Framework auth — secured with `Authorization: Bearer <MIDPOINT_API_TOKEN>` (the shared connector secret, constant-time compared). Distinct from `/api/messages`.

Body (produced by the JAR's `BotNotifier`):
```jsonc
{ "event":"ACCESS_REQUEST_RAISED|ACCESS_REQUEST_COMPLETED",
  "recipientRole":"manager|requester", "recipientEmail":"...",
  "title":"...", "message":"...", "status":"PENDING_APPROVAL|GRANTED|REJECTED",
  "caseOid":"...", "workItemId":"1", "requesterEmail":"...", "requesterName":"...", "requestedItem":"VPN Access" }
```
- `ACCESS_REQUEST_RAISED` → interactive approval card DM'd to `recipientEmail` (the manager). Buttons carry `caseOid`/`workItemId`/`requesterEmail`/`requestedItem` so the decision can complete the midPoint work item.
- `ACCESS_REQUEST_COMPLETED` → outcome card DM'd to the requester.
- Recipient resolution: `bot_user_directory.email → teams_user_id`; serviceUrl = most recent per-user `bot_access_requests.service_url`, else `TEAMS_SERVICE_URL`.
- Returns `{status: delivered|undeliverable|ignored|no_recipient, event, recipient}`. `401` bad token; `502` on delivery failure.

**Approve/reject callback** (`mp_approve` / `mp_approve_temporary` / `mp_reject` in `bot_handler._handle_midpoint_decision`): blocks self-approval, requires a reject comment, validates the temporary date (≤30 days), calls the JAR `POST /api/v1/requests/{email}/approve|reject`, then **updates the card in place** (`teams_service.update_card_in_teams`, HTTP PUT) to a locked decided state so it cannot be re-clicked. The requester is notified by midPoint's own `COMPLETED` event — no double-notify.

### `POST /api/notifications/send`
Prepares a notification (currently a stub — does not send email yet).
```json
// request
{ "transactionId": "NOTIF-001", "recipientEmail": "john@company.com",
  "audience": "user", "subject": "Test", "message": "Hello" }
// response
{ "status": "accepted", "transactionId": "NOTIF-001", "delivery": "prepared", ... }
```
Validation: `transactionId` ≤80, `subject` ≤180, `message` ≤4000, `recipientEmail` must be a valid email → else `422`.

---

## Java MidPoint Connector API (the JAR)

The bot calls the Java connector for provisioning and catalog. All endpoints require `Authorization: Bearer <MIDPOINT_API_TOKEN>` (a.k.a. `BOT_CONNECTOR_API_TOKEN`, a 32+ char shared secret). Default port `8088`.

Run it:
```powershell
cd midpoint-bot-connector
mvn clean package
java -jar target/midpoint-bot-connector-1.0.0.jar
```
Required env for the JAR: `BOT_CONNECTOR_API_TOKEN`, `MIDPOINT_BASE_URL`, `MIDPOINT_USERNAME`, `MIDPOINT_PASSWORD` (least-privileged service identity).

### Endpoints

| Method & Path | Purpose |
|---------------|---------|
| `GET  /actuator/health` | Health probe (`{"status":"UP"}`) |
| `GET  /api/v1/access-catalog` | Current roles/systems; refreshed ~every 30 min |
| `GET  /api/v1/catalog/requestable` | Live requestable roles menu `[{type, midpointType, name, oid, description, risk}]`; optional `?type=role` filter |
| `GET  /api/v1/users/lookup` | `?email=` → `{user:{oid, name, fullName, email, assignmentCount}}` (session-start profile) |
| `POST /api/v1/requests/permanent` | Create a permanent access request |
| `POST /api/v1/requests/temporary` | Create a temporary request (`approval.accessUntil`, ≤30 days) |
| `GET  /api/v1/requests/{id}/status` | Request status |
| `POST /api/v1/requests/{userEmailId}/approve` | Manager approval → provision |
| `POST /api/v1/requests/{userEmailId}/reject` | Manager rejection |
| `POST /api/v1/requests/{userEmailId}/temporary-access` | Grant temporary access directly |
| `POST /api/v1/notifications/manager` | Manager notification (stub) |
| `POST /api/v1/notifications/user` | User notification (stub) |
| `POST /api/v1/revocations` | Revoke access |
| `POST /api/v1/provisioning` | Legacy generic provisioning |

### Provisioning payload (permanent / temporary)
```json
{
  "transactionId": "REQ-123",
  "requester": { "midpointUserOid": "uuid-or-null", "email": "john@company.com" },
  "provisioning": { "targetType": "resource|role|group", "targetName": "AWS Production", "role": "Developer" },
  "approval": { "accessUntil": "2026-08-15" }   // temporary only
}
```

### Expected responses
```json
{ "status": "accepted", "message": "Provisioning request accepted by MidPoint",
  "transactionId": "REQ-123", "details": { } }
```
Errors: `400` invalid params · `401` bad/missing token · `413` payload too large · `422` validation · `500` server.

---

## Request / Approval Flow

```
1. User: "Request access to AWS"
   → POST /api/messages → bot_handler.process_activity()
     → detect intent (create_request), extract target=AWS
     → gather missing params (role, reason) conversationally
     → store request in DB
2. Params complete
     → fetch/validate against catalog (MidPoint)
     → resolve manager (Graph API or local directory)
     → send Adaptive Approval Card to manager's private 1:1 chat
     → confirm to requester
3. Manager clicks Approve
   → POST /api/messages (Action.Submit)
     → validate approver is the correct manager (block self-approval)
     → block if already approved/rejected
     → call JAR: POST /api/v1/requests/permanent (or /temporary)
     → update DB status=approved, notify user, audit
```

Approval Adaptive Card carries `data: { "action": "approve"|"reject", "request_id": <id> }` on its `Action.Submit` buttons.

### Requestable menu (choose what to request)

Two-step dynamic wizard driven by live midPoint data:
- **Greeting** (`hi`): the bot resolves the user's email → `provisioning_service.fetch_user_profile` (`GET /api/v1/users/lookup`) to authenticate + fetch the logged-in user's midPoint profile (oid, fullName, assignmentCount), personalizes the greeting. Best-effort — never blocks.
- **Step 1** `open_access_form` → `cards.requestable_type_card` — an access-**type** dropdown from `cards.REQUESTABLE_CATEGORIES` (roles only for now; extend the list to add services/etc.). `Next` → `req_pick_type`.
- **Step 2** `req_pick_type` fetches `fetch_requestable_access(access_type)` (`GET /api/v1/catalog/requestable?type=…`) and renders `requestable_target_card` — the **target-name** dropdown populated with live requestable items of the chosen type. No "Request access for" header, no business-justification field.
- Choice value encodes `type|oid|name`; `submit_requestable` → `requestable_confirm_card` → `confirm_requestable` → `bot_handler._submit_requestable` persists the request (trusting the authoritative menu, no pairing check) with `requested_target_type` + exact `target_oid`, then routes the manager card.
- On approval the provisioning payload carries the real `provisioning.targetType` (`role`) and `provisioning.targetOid`, so the JAR grants exactly that object (`midPointType`: role→`c:RoleType`).

---

## AI Toggle

Controlled by `ENABLE_AI`.

| Feature | AI Enabled | AI Disabled |
|---------|-----------|-------------|
| Intent parsing | LLM (flexible NL) | Hardcoded keywords |
| Parameter extraction | Conversational NL | Form submission |
| Schema classification | Dynamic (LLM inspects MCP tool schema) | Static fields |
| External deps | LLM API key required | None |

**Enable:** set `ENABLE_AI=true`, `LLM_PROVIDER`, and the matching API key, then restart.
**Disable:** set `ENABLE_AI=false`, restart (no LLM creds needed).

Keyword intent rules (AI disabled):
- `create_request`: access, request, get, need, want, grant, assign
- `check_status`: status, check, pending, approved, rejected
- `general_chat`: everything else

**Fallbacks:** AI enabled but LLM unavailable → static schema + keyword rules. On MCP mode, `get_user_facing_schema()` uses the LLM to split user-input vs system-managed fields and caches the result, evicting the cache when the MCP tool list changes.

---

## Proactive Manager Routing & Approval Validation

Notes from resolving the private-card routing (`teams_service.create_proactive_conversation`):

- **Mock ID filtering:** placeholder IDs (`29:approver-…`, `29:requester-…`) are stripped so the resolver does a live chat-member lookup instead of failing on a fake ID.
- **Proactive payload:** Teams requires root-level `tenantId` and `isGroup: false` to open a 1:1 chat — missing them causes `400 "Bad format of conversation ID"`. Falls back to `.env` tenant/app ID when absent from the activity.
- **Directory seeding:** real Teams ID + email must exist in `bot_user_directory`, else the bot falls back to a generic "Manager" with no Teams ID.
- **Name normalization:** name comparisons normalized (e.g. `Gourav` ↔ `Gourab`) to tolerate spelling variance.

**Approval guards:**
- *Self-approval blocked:* if `activity.from.name` == requester → `Error: You are not authorized to approve or reject your own access request.`
- *Double-processing blocked:* if request status already `approved`/`rejected` → `Error: Request #ID has already been processed and is APPROVED.`

---

## Bot Framework SDK path (flag-gated)

The bot ships **two** inbound paths for `POST /api/messages`, selected by `USE_BOT_FRAMEWORK_SDK`:
- **`false` (default)** — the original raw-REST path: `validate_bot_framework_authorization` +
  `process_activity(dict)`; replies/cards via manual Bot Connector REST. Unchanged, proven.
- **`true`** — Bot Framework SDK (`botbuilder-core` 4.17, classic `BotFrameworkAdapter` — FastAPI
  compatible, no aiohttp): the adapter authenticates the request, `AccessMateBot(ActivityHandler).on_turn`
  publishes the `TurnContext` (`app/bot/turn_state.current_turn`) and calls the same
  `process_activity(activity.serialize())`. `teams_service.send_reply/send_card/send_typing/update_card`
  detect the active TurnContext and send through the SDK; **proactive DMs stay on REST** (they target a
  different conversation, so they use the adapter-independent Bot Connector calls).

Components: `app/bot/adapter.py` (lazy adapter + bot singletons, `on_turn_error`),
`app/bot/accessmate_bot.py` (SDK↔handler bridge), `app/bot/turn_state.py` (TurnContext contextvar).
The bridge means the ~1500-line handler and all business logic are reused verbatim under either path.

**Enabling**: set `USE_BOT_FRAMEWORK_SDK=true`, ensure `TEAMS_APP_ID`/`TEAMS_APP_PASSWORD` are the bot's
app credentials, restart. **Needs live-Teams verification** before production — the SDK send/update paths
compile and import cleanly but have not been exercised against a real tenant here. Rollback = flip the flag.

## Conversation State & Interaction Security

Production-grade session lifecycle + concurrency control layered onto the existing raw-webhook bot
(single instance, SQLite; no external lock service).

```
Teams activity ──> /api/messages ──> process_activity()
                                         │
                                         ▼
                              session_manager.begin_activity()   ← THE GATE
             ┌───────────────────────────┼───────────────────────────┐
     retry dedup (activity id)   one-time token (card_token)   session state + hijack
             │                           │                           │
             └────────── allow ──────────┴─── block (typed reply) ───┘
                                         │
                     final action → acquire_lock (CAS → PROCESSING)
                     → "Processing…" → business logic → RESPONDING
                     → send response → complete() (COMPLETED)
```

State machine (enforced centrally; illegal transitions rejected):
```
NEW → ACTIVE → WAITING_FOR_BUTTON → PROCESSING → RESPONDING → COMPLETED
                     ▲                    │
                     └── unlock_keep_alive┘ (exception recovery)
   any non-terminal ─────────────────────────────────────────► EXPIRED (idle timeout)
```

**Components** (`app/services/`)
- `session_manager.py` — session model + state machine + **compare-and-swap** locking
  (`UPDATE … WHERE state IN (ACTIVE,WAITING_FOR_BUTTON) AND (locked=0 OR locked_at<stale)`), idempotency
  ledger (`bot_idempotency`), idle expiry, and the `begin_activity` security gate.
- `metrics_service.py` — thread-safe counters + processing-time aggregate → `GET /api/metrics` (bearer).
- `cards.stamp_card_token()` — injects one `card_token` per rendered card into every `Action.Submit`
  (applied centrally in `teams_service.send_card_to_teams`/`send_proactive_card`); `cards.locked_card()`
  renders the read-only "✓ You selected …" replacement.
- `db_service` — extended `bot_conversation_sessions` (session_id, user_id, tenant_id, state, locked,
  processing, locked_at, expires_at, …) + new `bot_idempotency` table; both upgraded idempotently.

**Guarantees**: one active session per conversation; greeting-to-start; **one-time buttons** (token) and
**Teams-retry dedup** (activity id); conversation **locked during processing** (parallel input →
"still processing"); **completion locks the session** (further input → "type 'Hi'"); **idle timeout →
EXPIRED**; **exception → unlock-keep-alive** (retryable, never wedged); **crash → stale lock reclaimed**
after `PROCESSING_TIMEOUT_SECONDS`; card locked in place after a decisive click. Approver/manager actions
(`mp_approve`/`approve`/…) are session-agnostic — idempotency + one-time tokens only.

**Config** (`.env`): `SESSION_TIMEOUT_MINUTES`, `PROCESSING_TIMEOUT_SECONDS`, `MAX_PROCESSING_SECONDS`,
`DEDUP_WINDOW_SECONDS`, `GREETING_KEYWORDS`.

**Tests**: `pytest -q` → `tests/test_session_manager.py` (double/rapid click, Teams retry, parallel-block,
CAS single-winner incl. threads, illegal transition, timeout→EXPIRED, exception recovery,
restart-after-complete, approver bypass). 13 tests.

**Remaining risks / limitations**: single-instance only — SQLite CAS is correct within one process but not
across replicas; SQLite serializes writes (fine at bot volumes); Teams card-update (`update_card_in_teams`)
is best-effort (falls back to a new locked card); the idempotency ledger currently has no TTL prune job
(rows accumulate — add a periodic delete beyond `DEDUP_WINDOW_SECONDS`); `datetime.utcnow()` used (naive UTC).

**Further hardening**: Redis/Cosmos distributed lock + shared session store for HA; optimistic concurrency
(row version) instead of CAS; OpenTelemetry traces spanning bot↔JAR↔midPoint; per-user rate limiting;
durable audit-log sink; move business logic out of `process_activity` into injected services (DI).

## Logging

Structured JSON, request-correlated, rotating (10 MB / 10 backups). Config in `app/logging_config.py`.

**Python bot**
- `logs/teams-bot.log` (all), `logs/teams-bot-errors.log` (errors).
- Each request tagged with a `request_id`, method, path, status, duration.

**Java JAR** (`src/main/resources/logback-spring.xml`)
- `logs/midpoint-connector.log`, `-errors.log`, `-api.log`.
- Spring profiles: `dev` = DEBUG, `prod` = WARN.

Useful queries:
```bash
grep '"level":"ERROR"' logs/teams-bot.log
grep "<request-id>" logs/teams-bot.log logs/midpoint-connector.log   # cross-service correlation
cat logs/teams-bot.log | jq 'select(.level=="ERROR")'
```
Do **not** log secrets or PII. Ship logs to a central sink (ELK/Datadog/CloudWatch) and alert on ERROR in production.

---

## Testing

Order: **health → per-service endpoints → Python↔Java integration → end-to-end via Teams.**

### 1. Start services
```powershell
# Terminal A — Java connector
cd midpoint-bot-connector; mvn clean package; java -jar target/midpoint-bot-connector-1.0.0.jar   # :8088

# Terminal B — Python bot
pip install -r requirements.txt
python -m app.main                                                                                 # :8000

# Terminal C — expose to Teams
ngrok http 8000
```

### 2. Health
```powershell
Invoke-WebRequest http://localhost:8000/api/health -UseBasicParsing        # {"status":"healthy","version":"0.1.0"}
curl.exe http://localhost:8088/actuator/health                             # {"status":"UP"}
```

### 3. Python bot (AUTH_MODE=token → use TEAMS_APP_PASSWORD as bearer)
```powershell
$tok = "<TEAMS_APP_PASSWORD>"
$json = '{"type":"message","from":{"id":"u2","name":"Jane Doe"},"text":"access to AWS","conversation":{"id":"c2","tenantId":"<tenant>"},"serviceUrl":"https://smba.trafficmanager.net/in/","recipient":{"id":"<bot-id>","name":"AccessMate"}}'
Invoke-WebRequest "http://localhost:8000/api/messages" -Method POST -Headers @{Authorization="Bearer $tok"} -ContentType "application/json" -Body $json -UseBasicParsing

# status + notification
Invoke-WebRequest "http://localhost:8000/api/requests/jane@company.com/status" -Headers @{Authorization="Bearer $tok"} -UseBasicParsing
```

### 4. Java JAR
```powershell
$jtok = "<MIDPOINT_API_TOKEN>"
curl.exe "http://localhost:8088/api/v1/access-catalog" -H "Authorization: Bearer $jtok"

$body = '{"transactionId":"REQ-001","requester":{"midpointUserOid":"uuid-1","email":"john@company.com"},"provisioning":{"targetType":"resource","targetName":"AWS Production","role":"Developer"}}'
curl.exe "http://localhost:8088/api/v1/requests/permanent" -X POST -H "Authorization: Bearer $jtok" -H "Content-Type: application/json" -d $body
```

### 5. End-to-end
Send an access message → watch `logs/teams-bot.log` (intent + JAR call) → `logs/midpoint-connector-api.log` (provisioning) → approve via the manager card → requester gets the provisioning result and the card locks.

### Checklist
- [ ] `request_id` present and matches across Python ↔ Java logs
- [ ] Intent detected correctly
- [ ] Self-approval blocked; re-approval blocked
- [ ] `401` on bad token, `413` on oversized payload, `422` on invalid notification body
- [ ] Catalog syncs (or falls back) when JAR reachable/unreachable

### Notes
- Notifications are currently **stubs** — no real email is sent.
- `accessUntil` must be within 30 days.
- Emails must match the MidPoint user directory.

---

## Deployment

**Dev:** `python -m app.main` + `ngrok http 8000`; set the ngrok host in `manifest.json` `validDomains` and the Bot Framework messaging endpoint.

**Prod:** run under uvicorn behind a TLS reverse proxy:
```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# nginx: https://bot.yourdomain.com → :8000 ; validDomains=["bot.yourdomain.com"]
```
Keep `VALIDATE_BOT_AUTH=true`, `ALLOW_UNAUTHENTICATED_DEV=false`, `MIDPOINT_TLS_VERIFY=true`. No Azure Bot Service required — the bot integrates via the Teams manifest and can run on any VM / container / K8s.
