# Decision Log (ADRs)

Architecture Decision Records. One per significant decision. Append; never rewrite history — supersede.

---

## ADR-0001 — Teams is presentation-only; midPoint is the source of truth
**Status:** Accepted · **Date:** 2026-07-29

**Context.** An earlier shortcut let the Teams bot resolve managers and drive approvals itself
(`MANAGER_NOTIFY_MODE=bot`). That makes Teams the approval engine.

**Decision.** midPoint owns workflow, approval state, authorization, provisioning, audit, and notification
triggering. Teams is only the user-interaction/presentation layer. The middleware only translates midPoint
events ↔ Teams and completes work items via REST — it never decides approvals or invents notifications.

**Consequences.** All business notifications must originate from midPoint (`WorkItem`/`Workflow` notifiers →
`/notify`). The bot-driven mode is retained as a **development fallback only**, never for production.
Depends on midPoint actually emitting to the middleware (see ADR-0002 and the §4 blocker in `architecture.md`).

---

## ADR-0002 — Middleware authenticates to midPoint as a non-superuser
**Status:** Accepted · **Date:** 2026-07-29

**Context.** Submitting assignments as `administrator` (superuser) **bypasses approval** — the role is
granted directly, no case/work-item, so no manager notification. Proven in testing.

**Decision.** The middleware uses a dedicated **non-superuser** service account (`Service_Account`, granted a
"Bot Connector Service" role with REST + read/add/modify/assign/unassign/completeWorkItem authorizations).
Non-superuser requests are subject to approval → a pending work item is created and routed to the line manager.

**Consequences.** Least privilege; approval is genuinely enforced by midPoint. The account's authz must be
maintained in midPoint; secrets live in `configuration.toml` (later Vault, see P8).

---

## ADR-0003 — Evolve the existing connector; refactor over rewrite
**Status:** Accepted · **Date:** 2026-07-29

**Context.** A working Spring Boot middleware already exists (midPoint REST, `/notify`, work-item complete,
catalog, eligibility). A greenfield rebuild would discard tested, working integration.

**Decision.** Evolve in place across Phases 1–10 (`architecture.md §6`). Refactor, extend, preserve public
APIs unless justified; every phase leaves the app building and running.

**Consequences.** Lower risk, faster to production; some transitional coexistence of old/new structure.

---

## ADR-0004 — Pin stack to Java 21 / Spring Boot 3.3.12 / midPoint 4.10.3
**Status:** Accepted · **Date:** 2026-07-29

**Context.** `pom.xml` briefly set Java 25 with Spring Boot 3.3.12 — will not build (Boot 3.3 supports up to
Java 22; no GA Java 25 support). The live server is midPoint 4.10.3 (not 4.8/4.9).

**Decision.** Java 21 (LTS), Spring Boot 3.3.12 (current), target midPoint 4.10.3. Revisit a Boot 3.5 upgrade
in a later phase if needed.

**Consequences.** Builds today, matches the live environment. Java/Boot upgrade tracked as future work.

---

## ADR-0005 — Zero-touch Teams delivery via Graph proactive install; midPoint stays authoritative
**Status:** Accepted · **Date:** 2026-07-31

**Context.** The bot could only DM a user whose real Teams id it had already learned from a prior
interaction (Bot Framework reveals the `29:` pairwise id only after the user messages the bot).
Seeded/mock ids (`29:requester-*`) fail delivery with `403 "Failed to decrypt pairwise id"`. This
does not scale to a real org where managers may never have opened the bot. Separately, the bot's
local directory could drift from midPoint (the identity source of truth).

**Decision.** Add a Graph-based resolver (`graph_service.resolve_aad_user` +
`ensure_installed_conversation`): email → Entra user → proactively install the bot app for that user
→ obtain the 1:1 chat id, used as the proactive conversation id. Delivery resolution order
(`notify_service._dispatch`): cached proactive ref → Graph install → learned `29:` id; mock ids are
never trusted. A `directory_service` caches delivery handles keyed by email, and every midPoint
payload upserts the identity as authoritative (`directory_source='midpoint'`), clearing stale mock
ids while preserving genuinely-learned delivery ids. Gated by `GRAPH_PROACTIVE_ENABLED` +
`TEAMS_CATALOG_APP_ID`; disabled → falls back to the prior learned-id path (no behaviour change).

**Consequences.** Managers/requesters are notified without ever messaging the bot first, at org
scale and across arbitrary org hierarchies (approver routing is already dynamic via midPoint's
`getManagersOidsExceptUser`). Requires admin-consented Graph app permissions
(`User.Read.All`, `TeamsAppInstallation.ReadWriteForUser.All`, `AppCatalog.Read.All`) and the org
catalog app id. midPoint remains the single source of truth for identity; the bot holds only a
delivery-handle cache.
