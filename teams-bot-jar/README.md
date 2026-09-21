# MidPoint Bot Connector

**Version:** 1.0.0

A Spring Boot (Java 21) REST connector that sits between a Python Teams bot backend and a **MidPoint** identity-governance server. It exposes a small bearer-authenticated API for browsing an access catalog and submitting access-request / approval / provisioning / revocation actions, translating each into MidPoint REST calls.

> ⚠️ **MidPoint compatibility:** Developed and tested on **MidPoint 4.10.3 and above**. It may or may not be compatible with any version lower than 4.10.3.

## Features

- **Access catalog** — periodically syncs MidPoint roles/services (every 30 min) into an in-memory cache for bot dropdowns; retains the last known catalog if MidPoint is unreachable.
- **Dynamic catalog traversal** — resolve a user's orgs → applications → application roles by email.
- **Access requests** — permanent and temporary (with `accessUntil` expiry, capped at 30 days).
- **Approval workflow** — approve / reject / grant temporary access endpoints.
- **Revocation** — remove a role assignment.
- **Notification receipts** — build manager/user notification payloads.
- **Bearer-token auth** — constant-time token comparison; every `/api/v1/**` endpoint is protected.
- **Structured JSON request/response logging** with per-request IDs.

## Requirements

- Java 21 (LTS)
- Maven 3.9+
- A reachable MidPoint 4.10.3+ server and a least-privileged service account

## Build

```bash
mvn clean package
```

Produces `target/midpoint-bot-connector-1.0.0.jar`.

## Configure

Configuration lives in **`configuration.toml`** (gitignored; see `configuration.example.toml` to create one). Set environment variable `CONNECTOR_CONFIG_FILE` to override the default path (`./configuration.toml`).

**Sections:**
- `[midpoint]` — `base_url` (HTTPS in prod; HTTP allowed only for localhost), `username`, `password` (least-privileged service account).
- `[connector]` — `api_token` (32+ char bearer secret), `notify_token`, `catalog_sync_minutes` (default 30), `connect_timeout_seconds`.
- `[bot]` — `notify_url`, `notify_token`.

> Do not commit real secrets to source control. Service account needs only: read roles/services, search users, modify/assign/unassign assignments.

See **[CLAUDE.md](CLAUDE.md) § Config** for startup validation details.

## Run

```bash
java -jar target/midpoint-bot-connector-1.0.0.jar
# or during development:
mvn spring-boot:run
```

Server listens on `PORT` (default **8088**). Health check (unauthenticated): `GET /actuator/health`.

## Using the API

Every endpoint except `/actuator/health` requires:

```
Authorization: Bearer <BOT_CONNECTOR_API_TOKEN>
```

Core endpoints:

- `GET  /api/v1/access-catalog`
- `POST /api/v1/requests/permanent`
- `POST /api/v1/requests/temporary`
- `POST /api/v1/requests/{userEmailId}/approve|reject|temporary-access`
- `POST /api/v1/revocations`

See **[DEV.md](DEV.md)** for the full endpoint reference, request shapes, MidPoint REST details, architecture, and testing guide.
