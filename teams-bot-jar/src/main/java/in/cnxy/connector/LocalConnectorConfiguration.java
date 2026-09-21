package in.cnxy.connector;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.tomlj.Toml;
import org.tomlj.TomlParseResult;

import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Loads all deployment config and secrets from an external {@code configuration.toml}
 * (path overridable via the {@code CONNECTOR_CONFIG_FILE} env var; default {@code ./configuration.toml}).
 * The file is gitignored — copy {@code configuration.example.toml} and fill in real values.
 */
@Configuration
public class LocalConnectorConfiguration {

    @Bean
    ConnectorProperties connectorProperties() {
        String configPath = System.getenv().getOrDefault("CONNECTOR_CONFIG_FILE", "configuration.toml");
        Path path = Path.of(configPath).toAbsolutePath();
        if (!Files.isRegularFile(path)) {
            throw new IllegalStateException("Config file not found: " + path
                    + " — copy configuration.example.toml to configuration.toml (or set CONNECTOR_CONFIG_FILE).");
        }
        TomlParseResult toml;
        try {
            toml = Toml.parse(path);
        } catch (Exception e) {
            throw new IllegalStateException("Could not read " + path + ": " + e.getMessage(), e);
        }
        if (toml.hasErrors()) {
            throw new IllegalStateException("Invalid TOML in " + path + ": " + toml.errors());
        }

        String baseUrl = require(toml, "midpoint.base_url");
        String username = require(toml, "midpoint.username");
        String password = require(toml, "midpoint.password");
        String apiToken = require(toml, "connector.api_token");
        String notifyToken = require(toml, "connector.notify_token");
        int syncMinutes = intOr(toml, "connector.catalog_sync_minutes", 30);
        int connectTimeout = intOr(toml, "connector.connect_timeout_seconds", 5);
        int rateLimitPerMinute = intOr(toml, "connector.rate_limit_per_minute", 120);
        String botNotifyUrl = require(toml, "bot.notify_url");
        String botNotifyToken = require(toml, "bot.notify_token");

        if (apiToken.length() < 32 || apiToken.startsWith("REPLACE_")) {
            throw new IllegalStateException("connector.api_token must be a real 32+ character secret.");
        }
        if (notifyToken.length() < 32 || notifyToken.startsWith("REPLACE_")) {
            throw new IllegalStateException("connector.notify_token must be a real 32+ character secret.");
        }
        URI midpointUri = URI.create(baseUrl);
        boolean loopbackHttp = "http".equalsIgnoreCase(midpointUri.getScheme())
                && ("localhost".equalsIgnoreCase(midpointUri.getHost()) || "127.0.0.1".equals(midpointUri.getHost()));
        if ((!"https".equalsIgnoreCase(midpointUri.getScheme()) && !loopbackHttp) || password.startsWith("REPLACE_")) {
            throw new IllegalStateException("Configure an HTTPS midpoint.base_url (HTTP only on localhost/127.0.0.1) and a real password.");
        }
        return new ConnectorProperties(apiToken, baseUrl, username, password, syncMinutes, connectTimeout,
                notifyToken, botNotifyUrl, botNotifyToken, rateLimitPerMinute);
    }

    private static String require(TomlParseResult toml, String key) {
        String value = toml.getString(key);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("Missing required config key '" + key + "' in configuration.toml");
        }
        return value;
    }

    private static int intOr(TomlParseResult toml, String key, int fallback) {
        Long value = toml.getLong(key);
        return value == null ? fallback : value.intValue();
    }
}
