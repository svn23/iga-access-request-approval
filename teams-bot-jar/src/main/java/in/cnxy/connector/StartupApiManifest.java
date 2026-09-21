package in.cnxy.connector;

import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/** Prints the connector contract during every successful startup. */
@Component
class StartupApiManifest {
    private static final Logger log = LoggerFactory.getLogger(StartupApiManifest.class);

    @PostConstruct
    void printManifest() {
        log.info("=== AccessMate MidPoint Connector API Manifest ===");
        log.info("GET  /api/v1/access-catalog             - current synchronized MidPoint roles/services for bot dropdowns");
        log.info("GET  /api/v1/catalog/requestable        - live requestable roles menu (typed, real data; optional ?type=)");
        log.info("GET  /api/v1/users/lookup?email=        - logged-in user's midPoint profile + assigned role names (eligibility)");
        log.info("GET  /api/v1/users/manager?email=       - requester's line manager from midPoint (org:manager)");
        log.info("POST /api/v1/requests/permanent        - submit a permanent access request");
        log.info("POST /api/v1/requests/temporary        - submit a temporary access request");
        log.info("POST /api/v1/requests/{userEmailId}/approve - approve a request and provision access");
        log.info("POST /api/v1/requests/{userEmailId}/reject  - reject a request");
        log.info("POST /api/v1/requests/{userEmailId}/temporary-access - grant temporary access with expiry");
        log.info("POST /api/v1/notifications/manager     - prepare a manager notification receipt");
        log.info("POST /api/v1/notifications/user        - prepare a user notification receipt");
        log.info("GET  /api/v1/requests/{email}/status   - monitor request status by requester email");
        log.info("POST /api/v1/notifications/send        - prepare a notification receipt");
        log.info("POST /api/v1/provisioning              - legacy alias for request submission");
        log.info("POST /api/v1/revocations               - legacy alias for revocation");
        log.info("POST /notify                           - inbound midPoint lifecycle event (X-Api-Key auth) -> forwarded to Teams bot");
        log.info("Authentication: Authorization: Bearer <BOT_CONNECTOR_API_TOKEN> for /api/v1/**; X-Api-Key for /notify.");
        log.info("Catalog synchronization: every 30 minutes; unavailable MidPoint retains the last known catalog.");
        log.info("================================================");
    }
}
