# Developer Guide — MidPoint Bot Connector

**Version:** 1.0.0
**Stack:** Spring Boot 3.3.12, Java 21, Maven
**Package:** `in.cnxy.connector`

> ⚠️ **MidPoint compatibility:** Developed and tested on **MidPoint 4.10.3 and above**. It may or may not be compatible with any version lower than 4.10.3. The REST payload shapes below (`object.object[]` nesting, `objectModification/itemDelta` deltas, `c:RoleType` targetRef types) are validated against 4.10.3+.

---

## 1. Where this fits

Full provisioning flow:

```
Teams bot (Python FastAPI, :8000)
      │  POST /api/messages  (Teams activities, access-request card flow)
      ▼
midpoint-bot-connector (Spring Boot JAR, :8088)   ← this project
      │  /api/v1/*  (Bearer token auth)
      ▼
MidPoint IGA (:8080)  /ws/rest/*  (HTTP Basic auth)
```

- **Teams bot** receives Teams activities, serves the access-request card flow, and calls this connector for catalog + provisioning. The bot's `MIDPOINT_API_TOKEN` must equal this connector's `BOT_CONNECTOR_API_TOKEN`.
- **This connector** is the safe adapter: authenticates the bot with a static Bearer token, then talks to MidPoint REST with Basic auth. Only role names present in the synced catalog are accepted for writes.
- **MidPoint** is the identity-governance backend. Roles/services form the access catalog; user role assignments are the provisioned grants.

---

## 2. Build, run, test

```bash
mvn clean package                                   # build fat JAR -> target/midpoint-bot-connector-1.0.0.jar
mvn spring-boot:run                                 # run locally
java -jar target/midpoint-bot-connector-1.0.0.jar   # run built JAR
```

- Server port: `PORT` env (default **8088**).
- Health check (public): `GET /actuator/health`.
- No automated test suite exists yet (`src/test` is absent), so `mvn test` is a no-op. When adding tests, use the bundled `spring-boot-starter-test` (JUnit 5); run a single class with `mvn -Dtest=ClassName test`.

### Where / how to test manually

1. **Postman** — import `postman/accessmate-midpoint-connector.postman_collection.json`. Set the collection's bearer token variable to your `BOT_CONNECTOR_API_TOKEN`.
2. **curl** — examples:

   ```bash
   TOKEN=<BOT_CONNECTOR_API_TOKEN>
   BASE=http://localhost:8088

   # Catalog
   curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/v1/access-catalog

   # Permanent request
   curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"transactionId":"t-1","requester":{"email":"user@example.com"},"provisioning":{"targetType":"role","targetName":"App Reader"}}' \
     $BASE/api/v1/requests/permanent

   # Temporary request
   curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"transactionId":"t-2","requester":{"email":"user@example.com"},"provisioning":{"targetType":"role","targetName":"App Reader"},"approval":{"accessUntil":"2026-08-15"}}' \
     $BASE/api/v1/requests/temporary

   # Revoke
   curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"transactionId":"t-3","email":"user@example.com","targetName":"App Reader","targetType":"role"}' \
     $BASE/api/v1/revocations
   ```

3. **Prerequisites for a green run**: MidPoint reachable at the configured base URL, the service account has read-role/read-service + user-search + assignment-modify authorizations, and the target role name exists in MidPoint (the catalog must have synced it).

**Verified end-to-end (reference smoke test):** catalog pulls real MidPoint roles; `POST /requests/permanent` → 200 role assigned; `POST /requests/temporary` → 200 assigned with `validTo`; `POST /revocations` → 200 removed; valid token works, bad/absent token rejected.

---

## 3. Configuration — important gotcha

Secrets and deployment config live in an external **`configuration.toml`** (gitignored), loaded at startup by **`LocalConnectorConfiguration.java`** via `tomlj`. Path override: `CONNECTOR_CONFIG_FILE` env var; default `./configuration.toml` (resolved against the process working directory — run the JAR from the project root, or set the env var). Copy `configuration.example.toml` → `configuration.toml` and fill in values. Sections:

- `[midpoint]` — `base_url`, `username`, `password` (the REST service identity).
- `[connector]` — `api_token` (bot→JAR bearer), `notify_token` (midPoint→JAR `/notify` X-Api-Key), `catalog_sync_minutes`, `connect_timeout_seconds`.
- `[bot]` — `notify_url`, `notify_token` (JAR→bot bearer).

Startup fail-fast validation (`IllegalStateException`):

- `configuration.toml` must exist, parse, and contain every required key.
- `connector.api_token` / `connector.notify_token` must be ≥ 32 chars and not start with `REPLACE_`.
- `midpoint.base_url` must be HTTPS, except `localhost`/`127.0.0.1` may use HTTP for local development (mirrored in both `LocalConnectorConfiguration` and `MidpointClient`).

> **`application.yml`** still maps `connector.*` to env vars but **nothing binds those keys to a bean** — inert; the TOML is the only live source.

> ⚠️ `configuration.toml` holds real credentials and is gitignored — never commit it; commit only `configuration.example.toml`. Current dev value uses `administrator`/`Sovan@23` because the intended `Service Account_Bot` login returns 401 from midPoint (fix the account in midPoint, then just edit the TOML — no rebuild of the creds needed, but a restart is).

---

## 4. Architecture

Single flat package `in.cnxy.connector`:

| Class | Role |
|-------|------|
| `ConnectorApplication` | Spring Boot entrypoint; `@EnableScheduling`. |
| `ApiController` | All `/api/v1` endpoints. Nested `record` DTOs (Jakarta validation) at the bottom of the file. |
| `CatalogService` | `@Scheduled` sync of **requestable** MidPoint roles+services into a `volatile` in-memory cache; `findByName` resolves a catalog item's `roleOid`. |
| `MidpointClient` | Thin `RestClient` (Basic auth) wrapper over MidPoint `/ws/rest/*`; builds JSON PATCH deltas; dynamic catalog traversal; approval-workflow case/work-item calls. |
| `NotifyController` | Inbound `POST /notify` — midPoint lifecycle events (`X-Api-Key` auth). Dedupes, normalizes, forwards to the bot. |
| `BotNotifier` | Forwards a normalized `NotifyMessage` to the Teams bot's proactive endpoint (Bearer `botNotifyToken`). |
| `SecurityConfig` | Stateless bearer auth via `OncePerRequestFilter` (`BearerFilter`). `/actuator/health` + `/notify` are skipped (see `shouldNotFilter`). |
| `RequestResponseLogger` + `WebConfig` | HandlerInterceptor structured JSON logging for `/api/v1/**`. |
| `ConnectorProperties` | Config record. |
| `LocalConnectorConfiguration` | Provides the live `ConnectorProperties` bean (see §3). |
| `StartupApiManifest` | Logs endpoint contract on boot. |

Records `CatalogItem` / `CatalogOption` live at the bottom of `MidpointClient.java`.

**Shared controller flow for writes:** `requireTransactionId` → `resolveUserOid` (use supplied OID, else look up by email) → `catalog.findByName` → `normalizeTargetType` → MidPoint mutation → `receipt(...)` map. Temporary/`accessUntil` dates must be after today and within 30 days.

**Approve/reject flow:** `decision()` calls `MidpointClient.findOpenWorkItem(userOid, targetOid)`. If a pending approval work item exists → `completeWorkItem(...)` with the approve/reject outcome URI. If none exists (target has no approval policy, so the submit PATCH auto-executed) → fall back to direct `assign` (approve) / `unassign` (reject). The receipt `mode` field reports `workItem` vs `direct`.

**Catalog behaviour:** sync interval = `catalogSyncMinutes` (default 30 min). Catalog is discovered via `requestable = true` search (roles + services). On sync failure it **retains the last known catalog** and retries next interval. Only names present in the synced catalog are accepted for writes.

**Defensive JSON:** MidPoint may return a node as an array, a single object, or missing — the `iter(...)` / `add(...)` helpers handle all three shapes.

> ⚠️ `StartupApiManifest` logs a few endpoints (e.g. `/requests/{email}/status`, `/notifications/send`) that are **not** implemented in `ApiController`. Treat the controller, not the manifest, as authoritative.

---

## 5. Connector API reference (`/api/v1`, Bearer auth)

All endpoints except `/actuator/health` require `Authorization: Bearer <BOT_CONNECTOR_API_TOKEN>`.

### Catalog (read)

| Method & Path | Query | Returns / Notes |
|---|---|---|
| `GET /access-catalog` | — | `{items:[{system:"MidPoint", roles:[...], risk:"medium"}], syncedAt, source:"midPoint"}`. Distinct role names from the synced cache. |
| `GET /catalog/orgs` | `email` | `{items:[{name, oid}]}` — org memberships (assignment targetRef of type `OrgType`) for the user resolved by email. |
| `GET /catalog/applications` | `email` | `{items:[{name, oid}]}` — Service objects assigned to any org the user belongs to. |
| `GET /catalog/roles` | `applicationOid` | `{items:[{name, oid}]}` — roles assigned to the given application service (its access levels). |
| `GET /catalog/requestable` | `type` (optional) | `{items:[{type, midpointType, name, oid, description, risk}]}` — every RoleType/ServiceType flagged `requestable=true` (the "choose what to request" menu). Optional `?type=role\|service` filters to one category. `midpointType` ∈ `c:RoleType\|c:ServiceType`. |
| `GET /users/lookup` | `email` | `{user:{oid, name, fullName, email, assignmentCount}}` — resolve the logged-in user's midPoint profile (used when a Teams session starts). |

### Requests / provisioning (write)

| Method & Path | Body | Action |
|---|---|---|
| `POST /requests/permanent` | `AccessRequest` | Assign role, no expiry. |
| `POST /requests/temporary` | `AccessRequest` (requires `approval.accessUntil`) | Assign role with expiry → MidPoint `activation.validTo`. |
| `POST /requests/{userEmailId}/approve` | `DecisionRequest` | Approve → complete pending approval work item (outcome `approve`); falls back to direct assign if no open case. |
| `POST /requests/{userEmailId}/reject` | `DecisionRequest` | Reject → complete pending approval work item (outcome `reject`); falls back to direct unassign if no open case. |
| `POST /requests/{userEmailId}/temporary-access` | `TemporaryAccessRequest` (requires `accessUntil`) | Approve with expiry. |
| `POST /provisioning` | `AccessRequest` | Legacy alias for request submission (optional `approval.accessUntil`). |
| `POST /revocations` | `RevokeRequest` | Remove a role assignment. |

### Notifications (receipt only — no message is actually sent)

| Method & Path | Body | Action |
|---|---|---|
| `POST /notifications/manager` | `NotificationRequest` | Build a manager notification receipt. |
| `POST /notifications/user` | `NotificationRequest` | Build a user notification receipt. |

### Request body shapes (nested records in `ApiController`)

```jsonc
// AccessRequest
{
  "transactionId": "string (required, <=80 chars)",
  "requester": { "midpointUserOid": "string?", "email": "user@example.com" },
  "provisioning": { "targetType": "role|resource|group", "targetName": "string" },
  "approval": { "accessUntil": "YYYY-MM-DD" }   // required only for /requests/temporary
}

// DecisionRequest (approve/reject)
{ "transactionId":"...", "midpointUserOid":"?", "email":"?", "targetName":"...", "targetType":"role|resource|group?", "accessUntil":"YYYY-MM-DD?", "comment":"?" }

// TemporaryAccessRequest
{ "transactionId":"...", "midpointUserOid":"?", "email":"?", "targetName":"...", "targetType":"?", "accessUntil":"YYYY-MM-DD (required)", "comment":"?" }

// RevokeRequest
{ "transactionId":"...", "midpointUserOid":"?", "email":"?", "targetName":"...", "targetType":"role|resource|group?" }

// NotificationRequest
{ "transactionId":"...", "recipientEmail":"user@example.com", "subject":"...", "message":"..." }
```

Rules:
- `targetType` ∈ `role | resource | group` (default `role`); mapped to MidPoint `c:RoleType | c:ResourceType | c:OrgType`.
- `targetName` is matched against the synced catalog by role name (`CatalogService.findByName`); unknown names are rejected.
- User is resolved by `midpointUserOid` if given, else by email lookup.
- `accessUntil` must be after today and within 30 days.

### Standard response ("receipt")

```jsonc
{ "status":"accepted", "message":"...", "transactionId":"...", "details":{ /* null values omitted */ } }
```

---

## 6. MidPoint REST facts (used by `MidpointClient`)

- Base: `<MIDPOINT_BASE_URL>/ws/rest`, HTTP Basic auth.
- **Discover requestable roles / services:** `POST /roles/search`, `POST /services/search` (both `?options=resolveNames`) with body `{"query":{"filter":{"text":"requestable = true"}}}` → `{object:{object:[ {@type:c:RoleType, oid, name, displayName, ...} ]}}`. A single result is an object, not a 1-element array — parser reads `root.path("object").path("object")` and handles array/object/missing. `catalog()` (cache) and `requestableAccess()` (typed menu) both use these.
- **targetType mapping** (`midPointType`): `role→c:RoleType`, `service→c:ServiceType`, `resource→c:ResourceType` (`group→c:OrgType` still mapped for the legacy per-user endpoints, not exposed in the requestable menu). Requests may pass an exact `provisioning.targetOid` to bypass name resolution (used by the requestable menu).
- **User search by email:** `POST /users/search` with an equal filter on `emailAddress`.
- **Assign:** `PATCH /users/{oid}` body:
  ```json
  {"objectModification":{"itemDelta":{"modificationType":"add","path":"assignment",
    "value":{"targetRef":{"oid":"<targetOid>","type":"c:RoleType"}}}}}
  ```
  Temporary adds `value.activation.validTo`. When the target has an approval policy this PATCH is accepted (204) but creates an approval Case whose OID is **not** returned synchronously (MID-9493) — locate it via case search.
- **Revoke:** same delta with `modificationType:"delete"`.
- **Find approval case / work item:** `POST /cases/search?options=resolveNames` with `{"query":{"filter":{"text":"state = \"open\""}}}`, then match client-side on `objectRef` (requesting user) + `targetRef` (requested target). `refOid(...)` reads `oid` or the `t:oid` form emitted by `resolveNames`.
- **Complete work item (approve/reject):** `POST /cases/{caseOid}/workItems/{workItemId}/complete` body:
  ```json
  {"output":{"@type":"c:AbstractWorkItemOutputType","comment":"...",
    "outcome":"http://midpoint.evolveum.com/xml/ns/public/model/approval/outcome#approve"}}
  ```
  Outcome URI ends `#approve` or `#reject`; returns 204.
- **Get case (verification):** `GET /cases/{oid}?options=resolveNames`.
- Dynamic catalog traversal uses `assignment/targetRef` ref-filter searches on `/users/{oid}`, `/services/search`, `/roles/search`, `/orgs/{oid}`.

---

## 6a. Inbound notifications (midPoint → JAR → Teams bot)

Approve/reject over REST is one direction; the other is midPoint **pushing** lifecycle events to the JAR so the bot can DM people. Flow:

```
midPoint event → notifier (builds JSON) → customTransport (HTTP POST) → POST /notify (this JAR) → BotNotifier → Teams bot
```

Two events, two recipients:
- `ACCESS_REQUEST_RAISED` → DM the **manager** (approve/reject prompt). midPoint `simpleWorkItemNotifier`.
- `ACCESS_REQUEST_COMPLETED` → DM the **requester** (granted/rejected). midPoint `simpleWorkflowNotifier`.

### `POST /notify` (this JAR)

- **Auth:** `X-Api-Key: <MIDPOINT_NOTIFY_TOKEN>` (constant-time compared; distinct from the bot Bearer token). Not the `/api/v1` Bearer — `/notify` is skipped by `BearerFilter` and permitted in `SecurityConfig`.
- **Inbound body** (produced by the midPoint notifier `bodyExpression`):
  ```jsonc
  { "event":"ACCESS_REQUEST_RAISED|ACCESS_REQUEST_COMPLETED", "caseOid":"<required>",
    "workItemId":1, "stageNumber":1, "requesterEmail":"...", "requesterName":"...",
    "requestedItem":"Database Admin role", "managerEmail":"...", "status":"GRANTED|REJECTED",
    "accessDuration":"Permanent" }
  ```
  `event` + `caseOid` required. For RAISED include `managerEmail` (the JAR does not re-resolve the manager). `requestedItem` (target role/service name) and `accessDuration` (e.g. "Permanent", "30 days") enrich the approval card; **see `docs/MIDPOINT_NOTIFIER_CONFIG.md`** for the Groovy `bodyExpression` to extract these from the case. `status` only read for COMPLETED (`GRANTED`/`APPROVED`/`SUCCESS` → granted, else rejected).
- **Idempotency:** dedupe key = `caseOid | workItemId | status`, bounded LRU (5000). A midPoint retry returns `{status:"duplicate"}` without re-forwarding. The key is recorded **only after** a successful bot forward, so a failed forward can be retried.
- **Responses (always 200 unless auth/forward fails):** `forwarded` | `duplicate` | `ignored` (unknown event) | `no_recipient`. Bad/missing `X-Api-Key` → 401. Bot unreachable → **502** (so midPoint/operators see the failure; nothing is deduped).

### Outbound to the bot (`BotNotifier` → `BOT_NOTIFY_URL`)

`POST <BOT_NOTIFY_URL>` with `Authorization: Bearer <BOT_NOTIFY_TOKEN>`, normalized body:
```jsonc
{ "event":"...", "recipientRole":"manager|requester", "recipientEmail":"...",
  "title":"...", "message":"...", "status":"PENDING_APPROVAL|GRANTED|REJECTED",
  "caseOid":"...", "workItemId":"1", "requesterEmail":"...", "requesterName":"...",
  "requestedItem":"Database Admin role", "accessDuration":"Permanent" }
```
The bot resolves `recipientEmail` to a Teams user and sends the DM. `title`/`message` are prebuilt human strings; the bot may render its own card from the structured fields instead. `requestedItem` and `accessDuration` are displayed in the approval card for clarity.

### midPoint side (4.10.3) — `systemConfiguration`

Custom transport in `messageTransportConfiguration` does the HTTP POST; two notifiers in `notificationConfiguration/handler` build the JSON and target that transport. The transport ships `message.getBody()` verbatim, so the notifier `bodyExpression` must emit the inbound JSON above (add the `X-Api-Key` header in the transport script). Bind variables (`requestee`, `workItem`, `aCase`, `event`) are 4.x-version-sensitive — confirm against the 4.10.3 notification-variable docs. Scope the notifiers to access-request cases (category/condition) so every unrelated change does not POST to the JAR.

## 6c. Bot-driven approval loop (blocker workaround)

Because midPoint's lifecycle notifier does **not** POST to the middleware (the standing blocker — see §4 / `docs/architecture.md`), the bot drives the manager approval loop itself while midPoint stays the source of truth for approval state:

1. **Submit.** Bot → `POST /api/v1/requests/permanent|temporary`. The JAR assigns, then (MID-9493: the PATCH returns no case OID) locates the created work item via `findOpenWorkItem(user, target)` and returns `caseOid` + `workItemId` + `targetOid` in the receipt `details`.
2. **Notify manager.** Bot resolves the requester's line manager (`GET /api/v1/users/manager`, midPoint `org:manager`) and proactively DMs the `midpoint_approval_card` (Graph zero-touch install → 1:1 chat). The card's buttons carry `caseOid` + `workItemId`.
3. **Decide.** Manager clicks → bot → `POST /api/v1/requests/{email}/approve|reject` with `caseOid`+`workItemId` → JAR `completeWorkItem` (real midPoint work item; idempotent, state-checked — see §6b).
4. **Notify requester.** Bot DMs the requester the granted/declined outcome directly (the COMPLETED notifier is likewise blocked).

No approval case in step 1 (target has no approval policy) → midPoint auto-executed the grant → bot tells the requester "granted", no manager step.

**Fallbacks / exception handling in this loop:**
- **Case-lookup retry** — after submit the JAR polls `resolvePendingApproval` 3× (500ms) before concluding "auto-executed", so a slow case (MID-9493 timing) isn't misreported as granted.
- **Submit-side dedupe** — before assigning, the JAR checks for an already-open case for the same (user, target); if found it returns `already_pending` (no duplicate case) and the bot tells the requester.
- **Real approvers** — the JAR returns the actual work-item `assigneeRef` emails; the bot DMs all of them, not an assumed org manager.
- **Approver resolution order** (bot) — real assignees → `org:manager` → `FALLBACK_APPROVER_EMAIL`. Self-approval (approver == requester) is dropped. No approver at all → requester told "no approver configured", case left pending, audited (`request_no_approver`).
- **Delivery retry** — the proactive send retries 3× with backoff, then a plain-text fallback; a genuine failure surfaces to the requester ("couldn't reach your manager … they can action it from their approvals").
- **`BOT_DIRECT_NOTIFY`** — master switch. **When the midPoint notifier is eventually fixed, set it false** so the notifier is the sole path (steps 2 & 4 become no-ops) and there's no double-notify. The JAR `/notify` path has its own dedupe; the bot-direct path does not.

## 6b. Approval robustness — concurrency, throughput, delayed decisions

Real-world approval is asynchronous and racy: a manager may click **hours or days** after the request, another approver may decide first, Teams may retry, a burst of requests may arrive at once. The decision path is built to stay correct and idempotent under all of these.

**Delayed approval (the "manager approves after 1hr / 1day" case).**
- The approval card lives in the manager's DM indefinitely; its buttons carry `caseOid` (+ `workItemId`) so the click is self-contained — no server-side session needed.
- Approver/manager actions are **exempt from the requester session** (`governs_session=False` in `session_manager.begin_activity`), so the 30-minute `session_timeout_minutes` never blocks a late decision.
- On click the JAR calls `MidpointClient.resolveActionable(caseOid, workItemId)` which fetches the case's **live state** first: `open` → complete the work item; `closed` → the request was already decided or expired; `missing` → the case is gone. Only `open` mutates midPoint.

**Concurrency / double-click / Teams retry.**
- Teams network retries are dropped by activity-id dedup (`seen_activity`).
- Two managers (multi-approver stage) or a rapid double-click: the first completes the work item; every later click re-resolves to `closed` → a terminal, **idempotent** no-op. No double-grant, no 500.
- The JAR returns a typed receipt `status`: `accepted` (decided now) · `already_closed` (decided/expired) · `not_found`. The bot maps the terminal ones to a neutral locked card ("⚪ NO LONGER ACTIONABLE") with an honest message, instead of the misleading "try again" (which is reserved for genuine 5xx/timeout — those keep the card actionable for a real retry).

**Throughput / burst.**
- `/notify` forwarding runs on a **bounded** pool (`ThreadPoolExecutor`, core 4 / max 8 / queue 500, `CallerRunsPolicy`) — a burst applies backpressure on the request thread instead of growing memory without limit.
- `RateLimitFilter` (token bucket, per source IP, `connector.rate_limit_per_minute`, default 120, 0 disables) runs **before** auth so floods are shed cheaply → HTTP 429 + `Retry-After`, never forwarded to midPoint.

**Known limits (single-instance; P10).** Dedupe cache, requestable catalog, rate-limit buckets, and the forward pool are all in-memory — correct for one instance, not shared across replicas. `resolveActionable` costs one extra `GET /cases/{oid}` per decision (acceptable at approval frequency). **Temporary-access caveat:** approving via work-item completion grants the assignment *as originally submitted* — a manager-chosen `accessUntil` at approval time is not retro-applied to the assignment on the work-item path (it only takes effect on the direct assign fallback). Limiting duration at approval time needs an assignment modify, tracked as future work.

## 7. Security model (`SecurityConfig`)

- CSRF, HTTP Basic, and form login are disabled.
- `RateLimitFilter` runs first (before auth) — per-IP token bucket, `connector.rate_limit_per_minute` (default 120, 0 disables); over-limit → 429 + `Retry-After`.
- `BearerFilter` (a `OncePerRequestFilter`) constant-time-compares the incoming token (`MessageDigest.isEqual`) and, on match, sets an authenticated principal with `ROLE_BOT`. It `shouldNotFilter` `/actuator/health` and `/notify`, so those paths never require the Bearer token.
- `anyRequest().authenticated()`; `/actuator/health` and `/notify` are `permitAll`. `/notify` enforces its own `X-Api-Key` check inside `NotifyController` (401 on mismatch).
- A bad/absent `/api/v1` token is rejected (401 from the filter; misconfigured error dispatch can surface as 403 via Spring's `/error` re-dispatch).

---

## 8. History — bugs found & fixed (debugging session 2026-07-20)

Recorded here so the same traps aren't re-introduced:

1. **Catalog parser bug** (`MidpointClient.catalog()/add()`) — original code searched for JSON fields literally named `"role"`/`"service"`, but MidPoint nests under `object.object[]`. Catalog was always empty → every write also failed (`findByName`). Root cause of the whole broken flow. Fixed to read `object.object` (array or single object).
2. **Compile failure** (`ApiController.decision()`) — called `r.transactionId()` but `DecisionRequest`/`TemporaryAccessRequest` lacked the field, so the JAR was a stale build. Fixed by adding `@NotBlank String transactionId` as the first record component.
3. **NPE on every write** — response detail built via `Map.of("accessUntil", accessUntil)`, but `Map.of` rejects null and `accessUntil` is null for permanent requests → HTTP 500 re-dispatched as a misleading 403. Fixed with a null-tolerant `details(Object... kv)` helper (LinkedHashMap, skips nulls).
4. **Boot failure — logback** — `logback-spring.xml` referenced `ch.qos.logback.contrib.json.classic.JsonEncoder`, which doesn't exist in `logback-json-classic:0.1.5` (that version ships `JsonLayout`). Fixed all appenders to `LayoutWrappingEncoder` wrapping `JsonLayout`.

**Bot-side fix (adjacent repo):** `app/services/provisioning_service.py` `_validate_midpoint_url` rejected `http://127.0.0.1:8088`, so the bot silently used a hardcoded catalog. Relaxed to allow plain HTTP for loopback hosts only (mirrors this connector's `MidpointClient` loopback exception).
