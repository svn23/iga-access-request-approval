package in.cnxy.connector;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

import java.util.List;

/**
 * Forwards a normalized notification to the Teams bot's proactive-message endpoint.
 * The bot resolves the recipient by email and DMs them.
 */
@Service
class BotNotifier {
    private static final Logger log = LoggerFactory.getLogger(BotNotifier.class);
    private final RestClient rest;
    private final ObjectMapper json;
    private final String path;

    BotNotifier(ConnectorProperties p, ObjectMapper json) {
        this.json = json;
        this.path = java.net.URI.create(p.botNotifyUrl()).getRawPath();
        String origin = p.botNotifyUrl().replaceAll(java.util.regex.Pattern.quote(path) + "$", "");
        this.rest = RestClient.builder()
                .baseUrl(origin.isBlank() ? p.botNotifyUrl() : origin)
                .defaultHeaders(h -> {
                    if (p.botNotifyToken() != null && !p.botNotifyToken().isBlank()) {
                        h.setBearerAuth(p.botNotifyToken());
                    }
                    h.setAccept(List.of(MediaType.APPLICATION_JSON));
                })
                .build();
    }

    /** POST the message to the bot. Throws on transport/HTTP error so the caller can signal a retry. */
    void send(NotifyMessage m) {
        ObjectNode body = json.createObjectNode();
        body.put("event", m.event());
        body.put("recipientRole", m.recipientRole());
        body.put("recipientEmail", m.recipientEmail());
        body.put("title", m.title());
        body.put("message", m.message());
        body.put("status", m.status());
        body.put("caseOid", m.caseOid());
        if (m.workItemId() != null) body.put("workItemId", m.workItemId());
        if (m.requesterEmail() != null) body.put("requesterEmail", m.requesterEmail());
        if (m.requesterName() != null) body.put("requesterName", m.requesterName());
        if (m.requestedItem() != null) body.put("requestedItem", m.requestedItem());
        if (m.accessDuration() != null) body.put("accessDuration", m.accessDuration());
        if (m.targetType() != null) body.put("targetType", m.targetType());
        rest.post().uri(path.isBlank() ? "/" : path)
                .contentType(MediaType.APPLICATION_JSON)
                .body(body)
                .retrieve()
                .toBodilessEntity();
        log.info("Forwarded {} for case {} to bot recipient {}", m.event(), m.caseOid(), m.recipientEmail());
    }
}

/** Normalized, bot-facing message built from a midPoint notification. */
record NotifyMessage(String event, String recipientRole, String recipientEmail, String title, String message,
                     String status, String caseOid, String workItemId, String requesterEmail,
                     String requesterName, String requestedItem, String accessDuration, String targetType) {}
