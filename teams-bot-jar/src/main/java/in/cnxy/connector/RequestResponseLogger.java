package in.cnxy.connector;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.HandlerInterceptor;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.util.UUID;

/**
 * Logs all incoming requests and outgoing responses with structured JSON format
 */
@Component
public class RequestResponseLogger implements HandlerInterceptor {
    private static final Logger logger = LoggerFactory.getLogger("in.cnxy.connector.api");
    private static final String REQUEST_ID = "X-Request-ID";

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) {
        String requestId = request.getHeader(REQUEST_ID);
        if (requestId == null) {
            requestId = UUID.randomUUID().toString();
        }
        request.setAttribute(REQUEST_ID, requestId);

        long startTime = System.currentTimeMillis();
        request.setAttribute("startTime", startTime);

        logger.info(
            "Incoming {} {} from {}",
            request.getMethod(),
            request.getRequestURI(),
            request.getRemoteAddr(),
            new LogContext()
                .withRequestId(requestId)
                .withMethod(request.getMethod())
                .withPath(request.getRequestURI())
                .withContentType(request.getContentType())
        );

        return true;
    }

    @Override
    public void afterCompletion(HttpServletRequest request, HttpServletResponse response, Object handler, Exception ex) {
        String requestId = (String) request.getAttribute(REQUEST_ID);
        Long startTime = (Long) request.getAttribute("startTime");

        long duration = startTime != null ? System.currentTimeMillis() - startTime : 0;

        if (ex != null) {
            logger.error(
                "Error {} {} -> {}: {}",
                request.getMethod(),
                request.getRequestURI(),
                response.getStatus(),
                ex.getMessage(),
                new LogContext()
                    .withRequestId(requestId)
                    .withStatus(response.getStatus())
                    .withDuration(duration + "ms")
                    .withError(ex.getClass().getName())
            );
        } else {
            logger.info(
                "Response {} {} -> {}",
                request.getMethod(),
                request.getRequestURI(),
                response.getStatus(),
                new LogContext()
                    .withRequestId(requestId)
                    .withStatus(response.getStatus())
                    .withDuration(duration + "ms")
            );
        }
    }

    /**
     * Helper class for structured logging context
     */
    static class LogContext {
        private String requestId;
        private String method;
        private String path;
        private String contentType;
        private int status;
        private String duration;
        private String error;

        LogContext withRequestId(String requestId) {
            this.requestId = requestId;
            return this;
        }

        LogContext withMethod(String method) {
            this.method = method;
            return this;
        }

        LogContext withPath(String path) {
            this.path = path;
            return this;
        }

        LogContext withContentType(String contentType) {
            this.contentType = contentType;
            return this;
        }

        LogContext withStatus(int status) {
            this.status = status;
            return this;
        }

        LogContext withDuration(String duration) {
            this.duration = duration;
            return this;
        }

        LogContext withError(String error) {
            this.error = error;
            return this;
        }

        @Override
        public String toString() {
            return String.format(
                "{requestId=%s, method=%s, path=%s, contentType=%s, status=%d, duration=%s, error=%s}",
                requestId, method, path, contentType, status, duration, error
            );
        }
    }
}
