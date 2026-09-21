package in.cnxy.connector;

import jakarta.validation.Valid;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import org.springframework.web.bind.annotation.*;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/v1")
class ApiController {
    final CatalogService catalog;
    final MidpointClient midpoint;

    ApiController(CatalogService c, MidpointClient m) {
        catalog = c;
        midpoint = m;
    }

    @GetMapping("/access-catalog")
    Map<String, Object> catalog() {
        return catalog.response();
    }

    @GetMapping("/catalog/orgs")
    Map<String, Object> userOrgs(@RequestParam @Email String email) {
        return Map.of("items", midpoint.orgsForUser(email));
    }

    @GetMapping("/catalog/applications")
    Map<String, Object> userApplications(@RequestParam @Email String email) {
        return Map.of("items", midpoint.applicationsForUser(email));
    }

    @GetMapping("/catalog/roles")
    Map<String, Object> applicationRoles(@RequestParam String applicationOid) {
        return Map.of("items", midpoint.rolesForApplication(applicationOid));
    }

    @GetMapping("/catalog/requestable")
    Map<String, Object> requestableAccess(@RequestParam(required = false) String type) {
        var items = midpoint.requestableAccess();
        if (type != null && !type.isBlank()) {
            String want = type.trim().toLowerCase();
            items = items.stream().filter(i -> i.type().equalsIgnoreCase(want)).toList();
        }
        return Map.of("items", items);
    }

    @GetMapping("/users/lookup")
    Map<String, Object> userLookup(@RequestParam @Email String email) {
        Map<String, Object> body = new java.util.HashMap<>();
        try {
            body.put("user", midpoint.userProfile(email)); // {user:null} → no such midPoint user
        } catch (IllegalArgumentException e) {
            body.put("user", null);
        }
        return body;
    }

    @GetMapping("/users/manager")
    Map<String, Object> userManager(@RequestParam @Email String email) {
        Map<String, Object> body = new java.util.HashMap<>();
        try {
            body.put("manager", midpoint.managerOf(email)); // null when no org manager (or no such user)
        } catch (IllegalArgumentException e) {
            body.put("manager", null);
        }
        return body;
    }

    @GetMapping("/users/pending-approvals")
    Map<String, Object> pendingApprovals(@RequestParam @Email String email) {
        Map<String, Object> body = new java.util.HashMap<>();
        try {
            body.put("count", midpoint.pendingApprovalsFor(email)); // open work items assigned to this user
        } catch (RuntimeException e) {
            body.put("count", 0); // best-effort: the greeting must render even if midPoint is briefly unreachable
        }
        return body;
    }

    @GetMapping("/users/requests")
    Map<String, Object> userRequests(@RequestParam @Email String email) {
        Map<String, Object> body = new java.util.HashMap<>();
        try {
            body.put("requests", midpoint.requestsFor(email)); // midPoint approval cases = source of truth
        } catch (IllegalArgumentException e) {
            body.put("requests", java.util.List.of());
        }
        return body;
    }

    @PostMapping("/requests/permanent")
    Map<String, Object> createPermanent(@Valid @RequestBody AccessRequest r) {
        return createRequest(r, null);
    }

    @PostMapping("/requests/temporary")
    Map<String, Object> createTemporary(@Valid @RequestBody AccessRequest r) {
        if (r.approval() == null || r.approval().accessUntil() == null || r.approval().accessUntil().isBlank()) {
            throw new IllegalArgumentException("approval.accessUntil is required for temporary requests");
        }
        return createRequest(r, r.approval().accessUntil());
    }

    @PostMapping("/requests/{userEmailId}/approve")
    Map<String, Object> approve(@PathVariable String userEmailId, @Valid @RequestBody DecisionRequest r) {
        return decision(userEmailId, r, "approved");
    }

    @PostMapping("/requests/{userEmailId}/reject")
    Map<String, Object> reject(@PathVariable String userEmailId, @Valid @RequestBody DecisionRequest r) {
        return decision(userEmailId, r, "rejected");
    }

    @PostMapping("/requests/{userEmailId}/temporary-access")
    Map<String, Object> temporaryAccess(@PathVariable String userEmailId, @Valid @RequestBody TemporaryAccessRequest r) {
        String accessUntil = r.accessUntil();
        if (accessUntil == null || accessUntil.isBlank()) {
            throw new IllegalArgumentException("accessUntil is required");
        }
        LocalDate date = LocalDate.parse(accessUntil);
        if (!date.isAfter(LocalDate.now()) || date.isAfter(LocalDate.now().plusDays(30))) {
            throw new IllegalArgumentException("accessUntil must be within 30 days");
        }
        return decision(userEmailId, new DecisionRequest(r.transactionId(), r.midpointUserOid(), r.email(), r.targetName(), r.targetType(), accessUntil, r.comment(), r.caseOid(), r.workItemId()), "approved");
    }

    @PostMapping("/notifications/manager")
    Map<String, Object> notifyManager(@Valid @RequestBody NotificationRequest r) {
        return notification("manager", r);
    }

    @PostMapping("/notifications/user")
    Map<String, Object> notifyUser(@Valid @RequestBody NotificationRequest r) {
        return notification("user", r);
    }

    @PostMapping("/provisioning")
    Map<String, Object> provision(@Valid @RequestBody AccessRequest r) {
        return createRequest(r, r.approval() == null ? null : r.approval().accessUntil());
    }

    @PostMapping("/revocations")
    Map<String, Object> revoke(@Valid @RequestBody RevokeRequest r) {
        String transactionId = requireTransactionId(r.transactionId());
        String oid = resolveUserOid(r.midpointUserOid(), r.email());
        CatalogItem item = catalog.findByName(r.targetName());
        String targetType = normalizeTargetType(r.targetType());
        midpoint.unassign(oid, targetType, item.roleOid());
        return receipt(transactionId, "accepted", "MidPoint assignment revocation submitted", Map.of("targetType", targetType, "targetName", r.targetName()));
    }

    private Map<String, Object> createRequest(AccessRequest r, String accessUntil) {
        String transactionId = requireTransactionId(r.transactionId());
        String targetType = normalizeTargetType(r.provisioning().targetType());
        String targetName = r.provisioning().targetName();
        // Prefer the exact oid when the caller already resolved it (e.g. picked from the
        // requestable menu); fall back to catalog name resolution otherwise.
        String targetOid = r.provisioning().targetOid();
        if (targetOid == null || targetOid.isBlank()) {
            targetOid = catalog.findByName(targetName).roleOid();
        }
        String oid = resolveUserOid(r.requester().midpointUserOid(), r.requester().email());

        if (accessUntil != null) {
            LocalDate date = LocalDate.parse(accessUntil);
            if (!date.isAfter(LocalDate.now()) || date.isAfter(LocalDate.now().plusDays(30))) {
                throw new IllegalArgumentException("accessUntil must be within 30 days");
            }
        }

        // Dedupe (idempotency): if an approval case for this exact (user, target) is already open,
        // do not create a duplicate — return the existing one so the caller can re-notify instead.
        var existing = midpoint.resolvePendingApproval(oid, targetOid);
        if (existing.isPresent()) {
            var pa = existing.get();
            return receipt(transactionId, "already_pending", "A pending approval request already exists for this access", details(
                    "targetType", targetType,
                    "targetName", targetName,
                    "targetOid", targetOid,
                    "accessUntil", accessUntil,
                    "caseOid", pa.caseOid(),
                    "workItemId", pa.workItemId(),
                    "approvers", pa.approverEmails()
            ));
        }

        midpoint.assign(oid, targetType, targetOid, accessUntil);
        // MID-9493: the assign PATCH doesn't return the approval case OID. When the target has an
        // approval policy, a case + work item now exist — locate them (with a short retry, since the
        // case may not be queryable the instant the PATCH returns) so the caller (Teams bot) can
        // drive the manager approval card directly (the midPoint notifier that would push this is a
        // known non-firing blocker). Absent after retries → auto-executed (no approval policy) → no
        // caseOid, and the caller treats the request as already granted. `approvers` are the actual
        // work-item assignees midPoint routed the approval to.
        var pending = midpoint.resolvePendingApprovalWithRetry(oid, targetOid, 3, 500);
        return receipt(transactionId, "accepted", "Provisioning request accepted by MidPoint", details(
                "targetType", targetType,
                "targetName", targetName,
                "targetOid", targetOid,
                "accessUntil", accessUntil,
                "caseOid", pending.map(PendingApproval::caseOid).orElse(null),
                "workItemId", pending.map(PendingApproval::workItemId).orElse(null),
                "approvers", pending.map(PendingApproval::approverEmails).orElse(null)
        ));
    }

    private Map<String, Object> decision(String userEmailId, DecisionRequest r, String status) {
        String resolvedTransactionId = requireTransactionId(r.transactionId());
        boolean approved = "approved".equals(status);
        String accessUntil = r.accessUntil();
        if (accessUntil != null && !accessUntil.isBlank()) {
            LocalDate date = LocalDate.parse(accessUntil);
            if (!date.isAfter(LocalDate.now()) || date.isAfter(LocalDate.now().plusDays(30))) {
                throw new IllegalArgumentException("accessUntil must be within 30 days");
            }
        }

        // Preferred path: the caller (Teams approval card) already knows the case from the RAISED
        // notification. Complete the work item directly — no user/target/catalog resolution — so
        // approval works even when the notification carried no requestedItem/targetName (the bug
        // where the card showed "the requested access" and could no longer be actioned).
        //
        // Resolve the case's live state FIRST so delayed/concurrent decisions are honest and
        // idempotent: a manager may click hours/days later after the case was already decided by
        // another approver, escalated, or auto-closed by an SLA policy. Those return a terminal
        // status (200, not 500) so the bot locks the card with a truthful message instead of a
        // misleading "try again". Only a still-open work item is actually completed.
        if (r.caseOid() != null && !r.caseOid().isBlank()) {
            CaseWorkItem lookup = midpoint.resolveActionable(r.caseOid(), r.workItemId());
            switch (lookup.status()) {
                case "closed" -> {
                    return receipt(resolvedTransactionId, "already_closed",
                            "This request has already been decided or has expired", details(
                                    "userEmailId", userEmailId, "caseOid", r.caseOid()));
                }
                case "missing" -> {
                    return receipt(resolvedTransactionId, "not_found",
                            "No matching approval request was found", details(
                                    "userEmailId", userEmailId, "caseOid", r.caseOid()));
                }
                default -> {
                    WorkItemRef ref = lookup.ref();
                    midpoint.completeWorkItem(ref, approved, r.comment());
                    // Prefer the caller's targetName; fall back to the name resolved from the case so
                    // the requester's outcome card always says what was approved/rejected.
                    String resolvedName = (r.targetName() != null && !r.targetName().isBlank())
                            ? r.targetName() : lookup.targetName();
                    return receipt(resolvedTransactionId, "accepted", "Request " + status, details(
                            "userEmailId", userEmailId,
                            "decision", status,
                            "mode", "workItem",
                            "caseOid", ref.caseOid(),
                            "workItemId", ref.workItemId(),
                            "targetName", resolvedName,
                            "accessUntil", accessUntil
                    ));
                }
            }
        }

        // Fallback path (legacy callers without case identifiers): resolve by user + target name,
        // search open cases, else assign/unassign directly. Requires a resolvable targetName.
        if (r.targetName() == null || r.targetName().isBlank()) {
            throw new IllegalArgumentException("Either caseOid+workItemId or targetName is required to record a decision");
        }
        String oid = resolveUserOid(r.midpointUserOid(), r.email());
        String targetType = normalizeTargetType(r.targetType());
        CatalogItem item = catalog.findByName(r.targetName());
        String path = midpoint.findOpenWorkItem(oid, item.roleOid())
                .map(wi -> {
                    midpoint.completeWorkItem(wi, approved, r.comment());
                    return "workItem";
                })
                .orElseGet(() -> {
                    if (approved) {
                        midpoint.assign(oid, targetType, item.roleOid(), accessUntil);
                    } else {
                        midpoint.unassign(oid, targetType, item.roleOid());
                    }
                    return "direct";
                });
        return receipt(resolvedTransactionId, "accepted", "Request " + status, details(
                "userEmailId", userEmailId,
                "decision", status,
                "mode", path,
                "targetType", targetType,
                "targetName", r.targetName(),
                "accessUntil", accessUntil
        ));
    }

    private Map<String, Object> notification(String audience, NotificationRequest r) {
        String transactionId = requireTransactionId(r.transactionId());
        return receipt(transactionId, "accepted", "Notification prepared for " + audience, Map.of(
                "audience", audience,
                "recipient", r.recipientEmail(),
                "subject", r.subject(),
                "message", r.message()
        ));
    }

    private static Map<String, Object> details(Object... keyValues) {
        Map<String, Object> map = new java.util.LinkedHashMap<>();
        for (int i = 0; i + 1 < keyValues.length; i += 2) {
            if (keyValues[i + 1] != null) {
                map.put((String) keyValues[i], keyValues[i + 1]);
            }
        }
        return map;
    }

    private Map<String, Object> receipt(String transactionId, String status, String message, Map<String, Object> details) {
        return Map.of(
                "status", status,
                "message", message,
                "transactionId", transactionId,
                "details", details
        );
    }

    private String resolveUserOid(String midpointUserOid, String email) {
        String oid = midpointUserOid;
        if (oid == null || oid.isBlank()) {
            oid = midpoint.resolveUserOid(email);
        }
        return oid;
    }

    private String normalizeTargetType(String targetType) {
        String normalized = targetType == null ? "role" : targetType.trim().toLowerCase();
        if (!List.of("role", "resource", "group", "service").contains(normalized)) {
            throw new IllegalArgumentException("targetType must be role, resource, group, or service");
        }
        return normalized;
    }

    private String requireTransactionId(String transactionId) {
        if (transactionId == null || transactionId.isBlank()) {
            throw new IllegalArgumentException("transactionId is required");
        }
        if (transactionId.length() > 80) {
            throw new IllegalArgumentException("transactionId is too long");
        }
        return transactionId.trim();
    }

    record AccessRequest(@NotBlank String transactionId, @Valid Requester requester, @Valid Provisioning provisioning, Approval approval) {
    }

    record Requester(String midpointUserOid, @Email String email) {
    }

    record Provisioning(@NotBlank String targetType, @NotBlank String targetName, String targetOid) {
    }

    record Approval(String accessUntil) {
    }

    record DecisionRequest(@NotBlank String transactionId, String midpointUserOid, @Email String email, String targetName, String targetType, String accessUntil, String comment,
                           String caseOid, String workItemId) {
    }

    record TemporaryAccessRequest(@NotBlank String transactionId, String midpointUserOid, @Email String email, String targetName, String targetType, @NotBlank String accessUntil, String comment,
                                  String caseOid, String workItemId) {
    }

    record RevokeRequest(@NotBlank String transactionId, String midpointUserOid, @Email String email, @NotBlank String targetName, String targetType) {
    }

    record NotificationRequest(@NotBlank String transactionId, @Email String recipientEmail, @NotBlank String subject, @NotBlank String message) {
    }
}
