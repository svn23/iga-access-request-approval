import os
from typing import Optional
# pyrefly: ignore [missing-import]
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Teams Bot configurations
    teams_app_id: Optional[str] = None
    teams_app_password: Optional[str] = None
    teams_app_tenant_id: Optional[str] = None

    # App settings
    port: int = 8000
    host: str = "0.0.0.0"

    # SQLite configuration
    sqlite_db_path: str = "data/accessmate.sqlite3"

    # LLM configurations
    enable_ai: bool = False
    llm_provider: str = "gemini"
    llm_model: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    openai_api_base: Optional[str] = None
    mcp_url: Optional[str] = None

    # Branding and provisioning integration.
    bot_name: str = "AccessMate"
    midpoint_api_url: Optional[str] = None
    midpoint_api_token: Optional[str] = None
    midpoint_base_url: Optional[str] = None
    # Fallback Bot Framework serviceUrl for proactive DMs triggered by inbound
    # midPoint notifications (no incoming activity to read it from). Region-stable,
    # e.g. https://smba.trafficmanager.net/in/ . The most recent per-user serviceUrl
    # seen in bot_access_requests is preferred when available.
    teams_service_url: Optional[str] = None
    catalog_sync_minutes: int = 30
    midpoint_tls_verify: bool = True
    provisioning_mode: str = "midpoint"  # mcp | midpoint | disabled
    max_activity_bytes: int = 65536
    validate_bot_auth: bool = True
    allow_unauthenticated_dev: bool = False
    auth_mode: str = "jwt"  # jwt | token (token mode uses TEAMS_APP_PASSWORD for testing)

    # Conversation state / interaction-security controls (see app/services/session_manager.py).
    # When true, /api/messages runs through the Bot Framework SDK (BotFrameworkAdapter +
    # AccessMateBot); replies/cards/updates go through TurnContext. When false (default),
    # the proven raw-REST path is used. Proactive DMs always use the REST helpers.
    use_bot_framework_sdk: bool = False

    # Who alerts the line manager on a raised request:
    #   "midpoint" — the bot submits the request to midPoint (creates the approval case) and midPoint's
    #                notifiers POST to the connector /notify, which DMs the manager. The bot does NOT
    #                resolve or DM the manager itself. (Architecture: midPoint-driven.)
    #   "bot"      — legacy: the bot resolves the manager and DMs the approval card directly.
    manager_notify_mode: str = "midpoint"

    # Require a comment when a manager rejects (audit trail + the requester learns why). True keeps
    # the card live and asks for a reason; set False to allow one-click rejects with no comment.
    require_reject_comment: bool = True

    # Fallback approver DM'd when a requester has no line manager in midPoint (org:manager unset),
    # or when the resolved manager is the requester themselves (self-approval). Blank → no fallback:
    # the requester is told no approver is configured and the request waits for admin action.
    fallback_approver_email: Optional[str] = None
    # Master switch for bot-driven approval DMs. True (default) → the bot resolves the manager and
    # DMs the approval card, and DMs the requester the outcome (needed while the midPoint push
    # notifier is a non-firing blocker). Set False once midPoint's notifier delivers reliably, so the
    # notifier path is the sole source and there is no double-notification.
    bot_direct_notify: bool = True

    # --- Graph-based proactive delivery (production, zero-touch) ---------------------------------
    # When true, a recipient whose real Teams id is unknown is resolved via Microsoft Graph:
    # email → Entra user → proactively install the bot app for that user → obtain the 1:1 chat id,
    # so managers/requesters are notified without ever having messaged the bot first.
    # Requires application Graph permissions (admin-consented):
    #   User.Read.All, TeamsAppInstallation.ReadWriteForUser.All, AppCatalog.Read.All
    # and TEAMS_CATALOG_APP_ID = the bot's app id in the org's Teams app catalog (NOT the bot/client id).
    graph_proactive_enabled: bool = False
    teams_catalog_app_id: Optional[str] = None
    # Graph token must target the REAL directory tenant (e.g. 97496121-…), NOT "botframework.com"
    # that TEAMS_APP_TENANT_ID uses for the multi-tenant bot token. Same app id/secret, real tenant.
    graph_tenant_id: Optional[str] = None
    # midPoint is the source of truth: when it sends a payload, its data overwrites the bot's local
    # cache (names/manager/email links). Delivery ids the bot learns from Teams are still kept.
    midpoint_authoritative: bool = True

    session_timeout_minutes: int = 30          # idle session expiry
    processing_timeout_seconds: int = 60        # a PROCESSING lock older than this is reclaimable (crash recovery)
    max_processing_seconds: int = 120           # hard ceiling for a single business operation
    dedup_window_seconds: int = 300             # how long idempotency keys are retained/considered
    greeting_keywords: str = "hi,hello,hey,start,hii"  # comma-separated session-start triggers

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
