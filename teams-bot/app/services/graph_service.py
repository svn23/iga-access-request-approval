import os
import logging
from typing import Optional, Dict
import httpx
from app.config import settings

logger = logging.getLogger("teams_bot.graph")

_GRAPH = "https://graph.microsoft.com/v1.0"


async def _graph_get(client: httpx.AsyncClient, url: str, token: str) -> Optional[httpx.Response]:
    try:
        return await client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=12.0)
    except Exception as e:  # noqa: BLE001
        logger.warning("Graph GET failed (%s): %s", url, e)
        return None


async def resolve_aad_user(email: str) -> Optional[Dict]:
    """Resolve an Entra (Azure AD) user by email/UPN. Returns {id, userPrincipalName, displayName, mail} or None.

    midPoint owns the email; this only maps it to the platform identity for delivery.
    """
    if not email:
        return None
    token = await get_graph_token()
    if not token:
        return None
    # A user's mail may differ from UPN; try direct id first, then a filtered lookup on mail/otherMails.
    async with httpx.AsyncClient() as client:
        r = await _graph_get(client, f"{_GRAPH}/users/{email}?$select=id,userPrincipalName,displayName,mail", token)
        if r is not None and r.status_code == 200:
            return r.json()
        safe = email.replace("'", "''")
        flt = f"mail eq '{safe}' or userPrincipalName eq '{safe}' or otherMails/any(m:m eq '{safe}')"
        r = await _graph_get(client, f"{_GRAPH}/users?$filter={flt}&$select=id,userPrincipalName,displayName,mail&$top=1", token)
        if r is not None and r.status_code == 200:
            vals = r.json().get("value") or []
            if vals:
                return vals[0]
    logger.warning("Graph could not resolve an Entra user for %s.", email)
    return None


async def ensure_installed_conversation(aad_user_id: str) -> Optional[str]:
    """Ensure the bot's Teams app is installed for the user, then return their 1:1 chat id (a proactive
    conversation id usable with the Bot Connector). Enables cold notifications — no prior interaction.

    Needs TEAMS_CATALOG_APP_ID and Graph app perms (TeamsAppInstallation.ReadWriteForUser.All).
    """
    catalog_app_id = settings.teams_catalog_app_id
    if not aad_user_id or not catalog_app_id:
        return None
    token = await get_graph_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
    base = f"{_GRAPH}/users/{aad_user_id}/teamwork/installedApps"
    async with httpx.AsyncClient() as client:
        # 1) Find an existing installation of our app.
        install_id = None
        r = await _graph_get(client, f"{base}?$expand=teamsAppDefinition&$filter=teamsApp/id eq '{catalog_app_id}'", token)
        if r is not None and r.status_code == 200:
            vals = r.json().get("value") or []
            if vals:
                install_id = vals[0].get("id")
        # 2) Install if absent (idempotent; 409 = already installed).
        if not install_id:
            try:
                body = {"teamsApp@odata.bind": f"{_GRAPH}/appCatalogs/teamsApps/{catalog_app_id}"}
                ir = await client.post(base, json=body, headers=headers, timeout=15.0)
                if ir.status_code not in (200, 201, 202, 409):
                    logger.error("Graph app-install for %s failed: %s - %s", aad_user_id, ir.status_code, ir.text[:300])
                    return None
            except Exception as e:  # noqa: BLE001
                logger.error("Graph app-install exception for %s: %s", aad_user_id, e)
                return None
            r = await _graph_get(client, f"{base}?$filter=teamsApp/id eq '{catalog_app_id}'", token)
            if r is not None and r.status_code == 200:
                vals = r.json().get("value") or []
                if vals:
                    install_id = vals[0].get("id")
        if not install_id:
            logger.error("Could not determine installedApps id for user %s.", aad_user_id)
            return None
        # 3) Get the 1:1 chat backing that installation — its id is the proactive conversation id.
        r = await _graph_get(client, f"{base}/{install_id}/chat?$select=id", token)
        if r is not None and r.status_code == 200:
            chat_id = r.json().get("id")
            if chat_id:
                return chat_id
        logger.error("Could not obtain chat id for user %s installation.", aad_user_id)
    return None


async def resolve_proactive_conversation(email: str) -> Optional[str]:
    """Full path: email → Entra user → ensure install → 1:1 chat id. None if unavailable."""
    if not settings.graph_proactive_enabled:
        return None
    user = await resolve_aad_user(email)
    if not user or not user.get("id"):
        return None
    return await ensure_installed_conversation(user["id"])

async def get_graph_token() -> Optional[str]:
    """
    Obtains a Microsoft Graph API access token using client credentials flow.
    """
    # Graph needs the REAL directory tenant. TEAMS_APP_TENANT_ID is "botframework.com" for the
    # multi-tenant bot token, which is invalid for Graph — prefer GRAPH_TENANT_ID, and ignore any
    # non-directory value (botframework.com / common) so a misconfig fails loudly instead of silently.
    tenant_id = settings.graph_tenant_id or os.environ.get("GRAPH_TENANT_ID")
    if not tenant_id:
        candidate = settings.teams_app_tenant_id or os.environ.get("TEAMS_APP_TENANT_ID")
        if candidate and candidate not in ("botframework.com", "common", "organizations"):
            tenant_id = candidate
    client_id = settings.teams_app_id or os.environ.get("TEAMS_APP_ID")
    client_secret = settings.teams_app_password or os.environ.get("TEAMS_APP_PASSWORD")

    if not tenant_id or not client_id or not client_secret:
        logger.error("Graph token needs GRAPH_TENANT_ID (real directory tenant) + app id/secret; missing or non-directory tenant.")
        return None
        
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, data=payload, headers=headers, timeout=10.0)
            if res.status_code == 200:
                return res.json().get("access_token")
            else:
                logger.error(f"Graph OAuth2 token acquisition failed: {res.status_code} - {res.text}")
                return None
    except Exception as e:
        logger.error(f"Graph OAuth2 token request exception: {e}")
        return None

async def get_user_manager_profile(user_aad_object_id: str) -> Optional[Dict]:
    """
    Queries Microsoft Graph API to retrieve the manager's Entra ID profile.
    """
    token = await get_graph_token()
    if not token:
        logger.warning("Could not execute Graph lookup: missing access token.")
        return None
        
    url = f"https://graph.microsoft.com/v1.0/users/{user_aad_object_id}/manager"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers, timeout=10.0)
            if res.status_code == 200:
                return res.json()
            elif res.status_code == 404:
                logger.warning(f"User {user_aad_object_id} does not have a manager configured in Entra ID.")
                return None
            else:
                logger.error(f"Graph API manager lookup failed: {res.status_code} - {res.text}")
                return None
    except Exception as e:
        logger.error(f"Graph API manager lookup exception: {e}")
        return None
