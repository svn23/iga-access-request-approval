package in.cnxy.connector;
public record ConnectorProperties(String apiToken, String midpointBaseUrl, String midpointUsername, String midpointPassword, int catalogSyncMinutes, int connectTimeoutSeconds, String midpointNotifyToken, String botNotifyUrl, String botNotifyToken, int rateLimitPerMinute) {}
