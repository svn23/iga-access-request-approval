# IGA Access Request & Approval

AccessMate — a Microsoft Teams-based access request and approval system for **MidPoint** identity governance. A user requests access through a Teams bot; the request is routed for approval and, once approved, provisioned into MidPoint automatically.

> ⚠️ **Status:** Under active development (v0.1.0). APIs, schemas, and environment variables may change without notice. Not production-hardened — run a security review before any production deployment.
>
> ⚠️ **MidPoint compatibility:** Developed and tested on **MidPoint 4.10.3 and above**. May or may not work on any version lower than 4.10.3.

## Components

| Folder | Language | Role |
|--------|----------|------|
| [`teams-bot`](teams-bot) | Python (FastAPI) | The Teams bot backend. Receives Teams activities, serves the access-request card flow, applies approval logic, and calls the connector for catalog + provisioning. |
| [`teams-bot-jar`](teams-bot-jar) | Java 21 (Spring Boot) | The MidPoint connector. A bearer-authenticated REST adapter that translates bot requests into MidPoint REST calls (catalog sync, assign/revoke roles). |

## Ownership

| Component | Owner | Contact |
|-----------|-------|---------|
| Python Teams bot (`teams-bot`) | Swapnil Das | swapnil@centroxy.com |
| Java MidPoint connector (`teams-bot-jar`) | Arupa Das | arupa@centroxy.com |

## Architecture

```
Microsoft Teams
      │  Teams activities
      ▼
teams-bot  (Python FastAPI, :8000)          POST /api/messages
      │  /api/v1/*  (Bearer token)
      ▼
teams-bot-jar  (Spring Boot connector, :8088)
      │  /ws/rest/*  (HTTP Basic)
      ▼
MidPoint IGA  (:8080)
```

The bot's `MIDPOINT_API_TOKEN` must equal the connector's `BOT_CONNECTOR_API_TOKEN`.

## Request → approval flow

1. **Request (Teams → bot):** the end user chats with the bot, which checks they exist in midPoint with the "End user" role, then lets them pick a role/app from the live requestable catalog (temporary access ≤ 30 days).
2. **Submit (bot → JAR → midPoint):** the bot calls the connector, which assigns the role in midPoint as a non-superuser service account. midPoint's approval policy creates an approval **case + work item** assigned to the requester's line manager. midPoint is the only place the request is stored.
3. **Notify (midPoint → JAR → bot → Teams):** the connector's `WorkItemPoller` (every 15 s, `connector.poll-seconds`) sees the new work item in midPoint and posts a `ACCESS_REQUEST_RAISED` event to the bot, which DMs the manager an Adaptive Card (privately, hidden from the requester). midPoint's own notifier can also fire; duplicates are dropped.
4. **Decide (Teams → bot → JAR → midPoint):** the manager clicks Approve/Reject. The bot calls the connector, which completes the work item in midPoint (self-approval and double-clicks are blocked).
5. **Provision + confirm:** midPoint provisions the access; the poller sees the case close and the requester gets an approved/rejected DM.

Rule of thumb: Teams presents, the connector translates, midPoint decides. See [`teams-bot-jar/docs/architecture.md`](teams-bot-jar/docs/architecture.md) and the ADRs in [`DecisionLog.md`](teams-bot-jar/docs/DecisionLog.md).

## Getting started

Each component runs independently — see its own docs:

- **Python bot:** [`teams-bot/README.md`](teams-bot/README.md) and [`teams-bot/DEV.md`](teams-bot/DEV.md)
- **Java connector:** [`teams-bot-jar/README.md`](teams-bot-jar/README.md) and [`teams-bot-jar/DEV.md`](teams-bot-jar/DEV.md); run its tests with `mvn test`.
- **Optional setting:** `connector.poll-seconds` (Spring property, default 15) sets how often the connector polls midPoint for approval changes.

## Repository layout

```
.
├── teams-bot/       # Python Teams bot (FastAPI)
└── teams-bot-jar/   # Java MidPoint connector (Spring Boot)
```
