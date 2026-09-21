package in.cnxy.connector;

import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;

/**
 * Detects new approval work items and finished cases by polling midPoint's case API and feeds them into the
 * same pipeline as midPoint's own {@code /notify} POST. Unblocks the notification flow when midPoint's
 * notifier/Groovy transport doesn't fire (architecture.md §4). Events derive purely from midPoint state; the
 * connector still decides nothing. Duplicates with the notifier are dropped by {@link NotifyController}'s dedupe.
 *
 * <p>ponytail: baseline is in-memory — cases opened/closed while the connector was down are missed. Persist the
 * snapshot (P10 event store) if that matters.
 */
@Service
class WorkItemPoller {
    private static final Logger log = LoggerFactory.getLogger(WorkItemPoller.class);
    private final MidpointClient midpoint;
    private final NotifyController notify;
    private Map<String, OpenCase> known; // null until the first poll establishes the baseline

    WorkItemPoller(MidpointClient midpoint, NotifyController notify) {
        this.midpoint = midpoint;
        this.notify = notify;
    }

    @Scheduled(initialDelay = 5000, fixedDelayString = "${connector.poll-seconds:15}000")
    synchronized void poll() {
        try {
            Map<String, OpenCase> now = midpoint.openApprovalCases();
            if (known != null) diff(known, now);
            known = now;
        } catch (RuntimeException e) {
            log.warn("Work-item poll failed; keeping previous snapshot and retrying: {}", e.getMessage());
        }
    }

    private void diff(Map<String, OpenCase> before, Map<String, OpenCase> now) {
        for (OpenCase c : now.values()) {
            OpenCase prev = before.get(c.caseOid());
            for (Map.Entry<String, List<String>> wi : c.workItems().entrySet()) {
                if (prev != null && prev.workItems().containsKey(wi.getKey())) continue;
                raised(c, wi.getKey(), wi.getValue());
            }
        }
        for (OpenCase c : before.values()) {
            if (!now.containsKey(c.caseOid())) completed(c);
        }
    }

    private void raised(OpenCase c, String workItemId, List<String> approverOids) {
        // ponytail: first approver only — dedupe key is per work item, so one DM per item; fan-out if multi-approver matters.
        String manager = approverOids.isEmpty() ? null : midpoint.emailForOid(approverOids.get(0));
        notify.accept(new NotifyController.NotifyPayload(NotifyController.EVENT_RAISED, c.caseOid(), intOrNull(workItemId), null,
                midpoint.emailForOid(c.requesterOid()), null, c.target(), manager, "PENDING_APPROVAL", null, null));
    }

    private void completed(OpenCase c) {
        JsonNode raw = midpoint.getCase(c.caseOid());
        JsonNode cs = raw.has("case") ? raw.path("case") : raw;
        String outcome = cs.path("outcome").asText("");
        String status = outcome.endsWith("#approve") || outcome.equalsIgnoreCase("approve") ? "GRANTED" : "REJECTED";
        notify.accept(new NotifyController.NotifyPayload(NotifyController.EVENT_COMPLETED, c.caseOid(), null, null,
                midpoint.emailForOid(c.requesterOid()), null, c.target(), null, status, null, null));
    }

    private static Integer intOrNull(String s) {
        try { return Integer.valueOf(s); } catch (NumberFormatException e) { return null; }
    }
}
