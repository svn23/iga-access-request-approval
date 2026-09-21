package in.cnxy.connector;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Inbound endpoint midPoint calls (via its custom transport) on access-request lifecycle events.
 * Authenticated with the X-Api-Key header (shared secret, distinct from the bot Bearer token),
 * deduplicated, normalized, then forwarded to the Teams bot.
 */
@RestController
class NotifyController {
    private static final Logger log = LoggerFactory.getLogger(NotifyController.class);
    static final String EVENT_RAISED = "ACCESS_REQUEST_RAISED";
    static final String EVENT_COMPLETED = "ACCESS_REQUEST_COMPLETED";

    private final ConnectorProperties props;
    private final BotNotifier bot;
    // Bounded LRU of already-processed (caseOid|workItemId|status) keys — dedupes midPoint retries.
    private final Map<String, Boolean> seen = Collections.synchronizedMap(new LinkedHashMap<>(256, 0.75f, true) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, Boolean> eldest) {
            return size() > 5000;
        }
    });
    // Off-request-thread forwarding so midPoint's SYNCHRONOUS notifier transport gets an instant ACK
    // (midPoint fires notifiers inside the assign commit; a slow bot→Teams send must not block — and
    // thereby time out — the requester's original submit).
    //
    // Bounded queue + CallerRunsPolicy: under a burst the queue caps at 500; once full, the submit
    // runs on the calling request thread (backpressure) instead of growing memory without limit.
    // This is the single-instance safety valve until P10 moves forwarding to a durable queue.
    private final java.util.concurrent.ExecutorService forwardPool =
            new java.util.concurrent.ThreadPoolExecutor(
                    4, 8, 60L, java.util.concurrent.TimeUnit.SECONDS,
                    new java.util.concurrent.ArrayBlockingQueue<>(500),
                    r -> {
                        Thread t = new Thread(r, "notify-forward");
                        t.setDaemon(true);
                        return t;
                    },
                    new java.util.concurrent.ThreadPoolExecutor.CallerRunsPolicy());

    NotifyController(ConnectorProperties props, BotNotifier bot) {
        this.props = props;
        this.bot = bot;
    }

    /**
     * Legacy/normalized inbound path: caller already speaks the connector's flat schema
     * ({@code event}, {@code requesterEmail}, {@code managerEmail}, {@code requestedItem}).
     */
    @PostMapping("/notify")
    ResponseEntity<Map<String, Object>> notify(@RequestHeader(value = "X-Api-Key", required = false) String apiKey,
                                               @Valid @RequestBody NotifyPayload p) {
        requireApiKey(apiKey);
        return process(p);
    }

    /**
     * The endpoint midPoint's {@code teamsMiddleware} custom transport actually POSTs to.
     * Accepts midPoint's native event schema ({@code eventType}, nested {@code requester}/{@code manager}/
     * {@code requestedObject}) and maps it onto the shared forward pipeline. Auth is X-Api-Key, same secret.
     */
    @PostMapping("/api/v1/midpoint/events")
    ResponseEntity<Map<String, Object>> midpointEvents(@RequestHeader(value = "X-Api-Key", required = false) String apiKey,
                                                       @RequestBody MidpointEvent e) {
        requireApiKey(apiKey);
        String type = e.eventType() == null ? "" : e.eventType().trim().toUpperCase();
        String mapped;
        if ("APPROVAL_PENDING".equals(type) || "APPROVAL_REQUESTED".equals(type)) mapped = EVENT_RAISED;
        else if ("APPROVAL_COMPLETED".equals(type)) mapped = EVENT_COMPLETED;
        else return receipt(HttpStatus.OK, "ignored", type, e.caseOid());
        NotifyPayload p = new NotifyPayload(
                mapped, e.caseOid(), e.workItemId(), null,
                e.requester() == null ? null : e.requester().email(),
                e.requester() == null ? null : e.requester().displayName(),
                e.requestedObject() == null ? null : e.requestedObject().name(),
                e.manager() == null ? null : e.manager().email(),
                e.status(), null, e.requestedObject() == null ? null : e.requestedObject().type());
        return acceptAsync(p);
    }

    /** Entry point for {@link WorkItemPoller}: same dedupe/normalize/forward path as midPoint's own POST. */
    void accept(NotifyPayload p) {
        acceptAsync(p);
    }

    /**
     * Ack midPoint immediately, forward to the bot off-thread. Dedupe + recipient checks run inline
     * (cheap) so the receipt is accurate; only the (potentially slow) bot send is deferred.
     */
    private ResponseEntity<Map<String, Object>> acceptAsync(NotifyPayload p) {
        String event = p.event() == null ? "" : p.event().trim().toUpperCase();
        if (!EVENT_RAISED.equals(event) && !EVENT_COMPLETED.equals(event)) {
            return receipt(HttpStatus.OK, "ignored", event, p.caseOid());
        }
        String key = p.caseOid() + "|" + (p.workItemId() == null ? "-" : p.workItemId()) + "|" + statusOf(p, event);
        if (seen.containsKey(key)) {
            return receipt(HttpStatus.OK, "duplicate", event, p.caseOid());
        }
        NotifyMessage msg = normalize(p, event);
        if (msg.recipientEmail() == null || msg.recipientEmail().isBlank()) {
            log.warn("No recipient email for {} case {} — nothing to forward", event, p.caseOid());
            return receipt(HttpStatus.OK, "no_recipient", event, p.caseOid());
        }
        seen.put(key, Boolean.TRUE); // optimistic; cleared below if the send fails so a retry can re-forward
        forwardPool.submit(() -> {
            try {
                bot.send(msg);
                log.info("Forwarded {} for case {} to bot", event, p.caseOid());
            } catch (RuntimeException ex) {
                seen.remove(key);
                log.error("Async forward of {} for case {} to bot failed: {}", event, p.caseOid(), ex.getMessage());
            }
        });
        return receipt(HttpStatus.ACCEPTED, "accepted", event, p.caseOid());
    }

    /** Shared dedupe → normalize → forward pipeline for both inbound shapes. */
    private ResponseEntity<Map<String, Object>> process(NotifyPayload p) {
        String event = p.event() == null ? "" : p.event().trim().toUpperCase();
        if (!EVENT_RAISED.equals(event) && !EVENT_COMPLETED.equals(event)) {
            return receipt(HttpStatus.OK, "ignored", event, p.caseOid());
        }

        String key = p.caseOid() + "|" + (p.workItemId() == null ? "-" : p.workItemId()) + "|" + statusOf(p, event);
        if (seen.containsKey(key)) {
            return receipt(HttpStatus.OK, "duplicate", event, p.caseOid());
        }

        NotifyMessage msg = normalize(p, event);
        if (msg.recipientEmail() == null || msg.recipientEmail().isBlank()) {
            log.warn("No recipient email for {} case {} — nothing to forward", event, p.caseOid());
            return receipt(HttpStatus.OK, "no_recipient", event, p.caseOid());
        }

        try {
            bot.send(msg);
        } catch (RuntimeException e) {
            // Record nothing on failure so a midPoint retry can re-forward.
            log.error("Forwarding {} for case {} to bot failed: {}", event, p.caseOid(), e.getMessage());
            throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "Bot notification forwarding failed");
        }
        seen.put(key, Boolean.TRUE);
        return receipt(HttpStatus.OK, "forwarded", event, p.caseOid());
    }

    private NotifyMessage normalize(NotifyPayload p, String event) {
        String item = p.requestedItem() == null ? "the requested access" : p.requestedItem();
        String who = p.requesterName() != null ? p.requesterName() : p.requesterEmail();
        String duration = p.accessDuration() == null ? "Permanent" : p.accessDuration();
        String type = p.targetType() == null ? "role" : p.targetType();
        if (EVENT_RAISED.equals(event)) {
            String recipient = p.managerEmail() != null && !p.managerEmail().isBlank() ? p.managerEmail() : null;
            return new NotifyMessage(event, "manager", recipient,
                    "Access request pending approval",
                    (who == null ? "A user" : who) + " requested " + item + ". Approve or reject.",
                    "PENDING_APPROVAL", p.caseOid(), str(p.workItemId()),
                    p.requesterEmail(), p.requesterName(), p.requestedItem(), duration, type);
        }
        String status = statusOf(p, event);
        boolean granted = "GRANTED".equals(status);
        return new NotifyMessage(event, "requester", p.requesterEmail(),
                "Access request " + (granted ? "approved" : "rejected"),
                "Your request for " + item + " was " + (granted ? "granted" : "rejected") + ".",
                status, p.caseOid(), str(p.workItemId()),
                p.requesterEmail(), p.requesterName(), p.requestedItem(), duration, type);
    }

    /** Normalize the completion status; RAISED is always pending. */
    private String statusOf(NotifyPayload p, String event) {
        if (EVENT_RAISED.equals(event)) return "PENDING_APPROVAL";
        String s = p.status() == null ? "" : p.status().trim().toUpperCase();
        if (s.equals("GRANTED") || s.equals("APPROVED") || s.equals("SUCCESS")) return "GRANTED";
        return "REJECTED";
    }

    private void requireApiKey(String apiKey) {
        byte[] expected = props.midpointNotifyToken().getBytes(StandardCharsets.UTF_8);
        byte[] actual = (apiKey == null ? "" : apiKey).getBytes(StandardCharsets.UTF_8);
        if (!MessageDigest.isEqual(expected, actual)) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Invalid or missing X-Api-Key");
        }
    }

    private static String str(Integer v) {
        return v == null ? null : String.valueOf(v);
    }

    private ResponseEntity<Map<String, Object>> receipt(HttpStatus code, String status, String event, String caseOid) {
        return ResponseEntity.status(code).body(Map.of(
                "status", status,
                "event", event == null ? "" : event,
                "caseOid", caseOid == null ? "" : caseOid));
    }

    /** Payload produced by the midPoint notifier bodyExpression (JSON). */
    record NotifyPayload(@NotBlank String event, @NotBlank String caseOid, Integer workItemId, Integer stageNumber,
                         String requesterEmail, String requesterName, String requestedItem,
                         String managerEmail, String status, String accessDuration, String targetType) {
    }

    /**
     * midPoint's native event schema, emitted verbatim by the {@code simpleWorkflowNotifier}
     * bodyExpression. Unknown fields (eventId, eventVersion, timestamp) are ignored.
     */
    @com.fasterxml.jackson.annotation.JsonIgnoreProperties(ignoreUnknown = true)
    record MidpointEvent(String eventType, String caseOid, Integer workItemId,
                         Party requester, Party manager, Requested requestedObject, String status) {
        @com.fasterxml.jackson.annotation.JsonIgnoreProperties(ignoreUnknown = true)
        record Party(String email, String displayName) {}
        @com.fasterxml.jackson.annotation.JsonIgnoreProperties(ignoreUnknown = true)
        record Requested(String name, String type) {}
    }
}
