package in.cnxy.connector;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Lightweight in-memory token-bucket rate limiter, applied per client key (the caller IP; the bot
 * and midPoint each present as one key). Protects the connector — and, downstream, midPoint — from
 * bursts or a misbehaving/looping caller. A rejected request gets HTTP 429 with a {@code Retry-After}
 * hint and is never forwarded to midPoint.
 *
 * <p>Single-instance only (like the dedupe/catalog caches). P10 replaces this with a shared limiter
 * (Redis) when the connector scales horizontally; until then this is the safety valve.
 *
 * <p>Config: {@code connector.rate_limit_per_minute} (0 disables). Default 120 req/min/key with a
 * burst capacity of the same, refilled continuously.
 */
class RateLimitFilter extends OncePerRequestFilter {
    private static final Logger log = LoggerFactory.getLogger(RateLimitFilter.class);

    private final double capacity;         // max tokens (burst)
    private final double refillPerNano;    // tokens added per nanosecond
    private final boolean enabled;
    private final Map<String, Bucket> buckets = new ConcurrentHashMap<>();
    private final AtomicLong lastSweep = new AtomicLong(System.nanoTime());

    RateLimitFilter(int perMinute) {
        this.enabled = perMinute > 0;
        this.capacity = Math.max(perMinute, 1);
        this.refillPerNano = perMinute / 60_000_000_000.0; // perMinute tokens across 60s in nanos
    }

    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain)
            throws ServletException, IOException {
        if (!enabled) {
            chain.doFilter(req, res);
            return;
        }
        String key = clientKey(req);
        if (tryConsume(key)) {
            chain.doFilter(req, res);
        } else {
            log.warn("Rate limit exceeded for {} on {} {}", key, req.getMethod(), req.getServletPath());
            res.setStatus(429);
            res.setHeader("Retry-After", "1");
            res.setContentType("application/json");
            res.getWriter().write("{\"status\":\"rate_limited\",\"message\":\"Too many requests\"}");
        }
    }

    private boolean tryConsume(String key) {
        long now = System.nanoTime();
        sweepIfDue(now);
        Bucket b = buckets.computeIfAbsent(key, k -> new Bucket(capacity, now));
        synchronized (b) {
            double refill = (now - b.lastRefillNanos) * refillPerNano;
            b.tokens = Math.min(capacity, b.tokens + refill);
            b.lastRefillNanos = now;
            if (b.tokens >= 1.0) {
                b.tokens -= 1.0;
                return true;
            }
            return false;
        }
    }

    /** Evict idle buckets roughly every 5 minutes so the map can't grow unbounded. */
    private void sweepIfDue(long now) {
        long last = lastSweep.get();
        if (now - last < 300_000_000_000L) return;
        if (!lastSweep.compareAndSet(last, now)) return;
        buckets.entrySet().removeIf(e -> {
            synchronized (e.getValue()) {
                return (now - e.getValue().lastRefillNanos) > 300_000_000_000L;
            }
        });
    }

    private static String clientKey(HttpServletRequest req) {
        String fwd = req.getHeader("X-Forwarded-For");
        if (fwd != null && !fwd.isBlank()) {
            int comma = fwd.indexOf(',');
            return (comma > 0 ? fwd.substring(0, comma) : fwd).trim();
        }
        String ip = req.getRemoteAddr();
        return ip == null ? "unknown" : ip;
    }

    private static final class Bucket {
        double tokens;
        long lastRefillNanos;
        Bucket(double tokens, long now) { this.tokens = tokens; this.lastRefillNanos = now; }
    }
}
