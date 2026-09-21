import os
from typing import Optional
# pyrefly: ignore [missing-import]
import httpx
from app.config import settings
from app.services.cards import stamp_card_token
from app.bot.turn_state import current_turn

async def _get_bot_token() -> Optional[str]:
    """
    Obtains the Bot Framework OAuth2 token asynchronously using credentials from settings.
    """
    client_id = settings.teams_app_id or os.environ.get("TEAMS_APP_ID")
    client_secret = settings.teams_app_password or os.environ.get("TEAMS_APP_PASSWORD")
    
    if not client_id or not client_secret:
        print("[INFO] Teams bot credentials are not set in .env. Attempting anonymous/offline reply for Emulator.")
        return None
        
    tenant_id = settings.teams_app_tenant_id or os.environ.get("TEAMS_APP_TENANT_ID")
    if tenant_id and tenant_id.strip() and tenant_id not in ("common", "botframework.com"):
        token_url = f"https://login.microsoftonline.com/{tenant_id.strip()}/oauth2/v2.0/token"
    else:
        token_url = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
        
    token_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://api.botframework.com/.default"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            token_res = await client.post(token_url, data=token_payload, headers=token_headers, timeout=10.0)
            if token_res.status_code == 200:
                return token_res.json().get("access_token")
            else:
                print(f"[ERROR] Failed to obtain bot token: {token_res.status_code} - {token_res.text}")
                return None
    except Exception as e:
        print(f"[ERROR] Exception obtaining bot token: {e}")
        return None

async def send_typing_activity(activity: dict) -> bool:
    """Send a Teams typing indicator (native animated dots) as a loading cue."""
    tc = current_turn.get()
    if tc is not None:
        try:
            from botbuilder.schema import Activity, ActivityTypes
            await tc.send_activity(Activity(type=ActivityTypes.typing))
            return True
        except Exception as e:
            print(f"[ERROR] SDK typing failed: {e}")
            return False
    service_url = activity.get("serviceUrl")
    conversation_id = activity.get("conversation", {}).get("id")
    if not service_url or not conversation_id:
        return False
    access_token = await _get_bot_token()
    if not service_url.endswith("/"):
        service_url += "/"
    url = f"{service_url}v3/conversations/{conversation_id}/activities"
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    payload = {
        "type": "typing",
        "from": activity.get("recipient"),
        "conversation": activity.get("conversation"),
        "recipient": activity.get("from"),
    }
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, headers=headers, timeout=10.0)
            return res.status_code in (200, 201, 202)
    except Exception as e:
        print(f"[ERROR] Exception sending typing indicator: {e}")
        return False


async def send_reply_to_teams(activity: dict, text_response: str) -> bool:
    """
    Sends a plain text reply message back to Microsoft Teams asynchronously.
    """
    tc = current_turn.get()
    if tc is not None:
        try:
            from botbuilder.core import MessageFactory
            await tc.send_activity(MessageFactory.text(text_response))
            return True
        except Exception as e:
            print(f"[ERROR] SDK reply failed: {e}")
            return False
    service_url = activity.get("serviceUrl")
    conversation_id = activity.get("conversation", {}).get("id")
    activity_id = activity.get("id")
    
    if not service_url or not conversation_id or not activity_id:
        print("[ERROR] Cannot reply to Teams message: missing serviceUrl, conversation ID, or activity ID.")
        return False

    access_token = await _get_bot_token()

    if not service_url.endswith("/"):
        service_url += "/"
        
    reply_url = f"{service_url}v3/conversations/{conversation_id}/activities/{activity_id}"
    reply_headers = {"Content-Type": "application/json"}
    if access_token:
        reply_headers["Authorization"] = f"Bearer {access_token}"
    
    reply_payload = {
        "type": "message",
        "text": text_response,
        "recipient": activity.get("from"),
        "from": activity.get("recipient"),
        "conversation": activity.get("conversation"),
        "replyToId": activity_id
    }
    
    try:
        async with httpx.AsyncClient() as client:
            reply_res = await client.post(reply_url, json=reply_payload, headers=reply_headers, timeout=10.0)
            if reply_res.status_code in (200, 201, 202):
                print(f"[INFO] Successfully sent bot reply to Teams conversation {conversation_id}.")
                return True
            else:
                print(f"[ERROR] Failed to send bot reply: {reply_res.status_code} - {reply_res.text}")
                return False
    except Exception as e:
        print(f"[ERROR] Exception sending bot reply to Teams: {e}")
        return False

async def send_card_to_teams(activity: dict, card_content: dict) -> bool:
    """
    Sends an Adaptive Card reply back to Microsoft Teams asynchronously.
    """
    service_url = activity.get("serviceUrl")
    conversation_id = activity.get("conversation", {}).get("id")
    activity_id = activity.get("id")
    
    stamp_card_token(card_content)  # one-time-button protection
    tc = current_turn.get()
    if tc is not None:
        try:
            from botbuilder.core import MessageFactory, CardFactory
            await tc.send_activity(MessageFactory.attachment(CardFactory.adaptive_card(card_content)))
            return True
        except Exception as e:
            print(f"[ERROR] SDK card send failed: {e}")
            return False

    if not service_url or not conversation_id or not activity_id:
        print("[ERROR] Cannot send card to Teams: missing serviceUrl, conversation ID, or activity ID.")
        return False

    access_token = await _get_bot_token()

    if not service_url.endswith("/"):
        service_url += "/"

    reply_url = f"{service_url}v3/conversations/{conversation_id}/activities/{activity_id}"
    reply_headers = {"Content-Type": "application/json"}
    if access_token:
        reply_headers["Authorization"] = f"Bearer {access_token}"
    
    reply_payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card_content
            }
        ],
        "recipient": activity.get("from"),
        "from": activity.get("recipient"),
        "conversation": activity.get("conversation"),
        "replyToId": activity_id
    }
    
    try:
        async with httpx.AsyncClient() as client:
            reply_res = await client.post(reply_url, json=reply_payload, headers=reply_headers, timeout=10.0)
            if reply_res.status_code in (200, 201, 202):
                print(f"[INFO] Successfully sent bot Adaptive Card to Teams conversation {conversation_id}.")
                return True
            else:
                print(f"[ERROR] Failed to send bot Adaptive Card: {reply_res.status_code} - {reply_res.text}")
                return False
    except Exception as e:
        print(f"[ERROR] Exception sending bot Adaptive Card to Teams: {e}")
        return False

async def update_card_in_teams(service_url: str, conversation_id: str, activity_id: str, card_content: dict, bot: dict | None = None, recipient: dict | None = None) -> bool:
    """Replace an already-posted Adaptive Card in place (Teams activity update / PUT).

    Used to lock an approval card into its decided state after the manager acts, so a
    stale card cannot be clicked twice. Returns False if the update cannot be issued.
    """
    if not activity_id:
        return False
    tc = current_turn.get()
    if tc is not None:
        try:
            from botbuilder.core import CardFactory
            from botbuilder.schema import Activity, ActivityTypes
            await tc.update_activity(Activity(
                type=ActivityTypes.message, id=activity_id,
                conversation=tc.activity.conversation,
                attachments=[CardFactory.adaptive_card(card_content)],
            ))
            return True
        except Exception as e:
            print(f"[ERROR] SDK card update failed: {e}")
            return False
    if not service_url or not conversation_id:
        return False
    access_token = await _get_bot_token()
    if not service_url.endswith("/"):
        service_url += "/"
    url = f"{service_url}v3/conversations/{conversation_id}/activities/{activity_id}"
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    payload = {
        "type": "message",
        "id": activity_id,
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card_content}],
        "conversation": {"id": conversation_id},
    }
    if bot:
        payload["from"] = bot
    if recipient:
        payload["recipient"] = recipient
    try:
        async with httpx.AsyncClient() as client:
            res = await client.put(url, json=payload, headers=headers, timeout=10.0)
            return res.status_code in (200, 201, 202)
    except Exception as e:
        print(f"[ERROR] Exception updating Adaptive Card: {e}")
        return False


async def create_proactive_conversation(service_url: str, bot_id: str, bot_name: str, member_id: str, tenant_id: str) -> Optional[str]:
    """
    Creates a new 1:1 conversation with a Teams user proactively.
    Returns the conversation ID if successful, otherwise None.
    """
    if not service_url.endswith("/"):
        service_url += "/"
    url = f"{service_url}v3/conversations"
    access_token = await _get_bot_token()
    
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        
    payload = {
        "bot": {
            "id": bot_id,
            "name": bot_name
        },
        "members": [
            {
                "id": member_id
            }
        ],
        "channelData": {
            "tenantId": tenant_id
        },
        "tenantId": tenant_id,
        "isGroup": False
    }
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, headers=headers, timeout=10.0)
            if res.status_code in (200, 201):
                return res.json().get("id")
            else:
                print(f"[ERROR] Failed to create proactive conversation: {res.status_code} - {res.text}")
                return None
    except Exception as e:
        print(f"[ERROR] Exception creating proactive conversation: {e}")
        return None

async def send_proactive_card(service_url: str, conversation_id: str, bot_id: str, bot_name: str, card_content: dict) -> bool:
    """
    Sends an Adaptive Card to a proactive/new conversation channel.
    """
    if not service_url.endswith("/"):
        service_url += "/"
    url = f"{service_url}v3/conversations/{conversation_id}/activities"
    access_token = await _get_bot_token()
    
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card_content
            }
        ],
        "from": {
            "id": bot_id,
            "name": bot_name
        },
        "conversation": {
            "id": conversation_id
        }
    }
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, headers=headers, timeout=10.0)
            return res.status_code in (200, 201, 202)
    except Exception as e:
        print(f"[ERROR] Exception sending proactive card: {e}")
        return False

async def send_proactive_message(service_url: str, conversation_id: str, bot_id: str, bot_name: str, text: str) -> bool:
    """
    Sends a plain text message to a proactive/new conversation channel.
    """
    if not service_url.endswith("/"):
        service_url += "/"
    url = f"{service_url}v3/conversations/{conversation_id}/activities"
    access_token = await _get_bot_token()
    
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        
    payload = {
        "type": "message",
        "text": text,
        "from": {
            "id": bot_id,
            "name": bot_name
        },
        "conversation": {
            "id": conversation_id
        }
    }
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, headers=headers, timeout=10.0)
            return res.status_code in (200, 201, 202)
    except Exception as e:
        print(f"[ERROR] Exception sending proactive message: {e}")
        return False

async def get_teams_member_profile(service_url: str, conversation_id: str, member_id: str) -> Optional[dict]:
    """
    Retrieves the user's Microsoft Teams/Entra ID profile details from the Bot Connector.
    """
    if not service_url.endswith("/"):
        service_url += "/"
    url = f"{service_url}v3/conversations/{conversation_id}/members/{member_id}"
    access_token = await _get_bot_token()
    
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers, timeout=10.0)
            if res.status_code == 200:
                return res.json()
            else:
                print(f"[ERROR] Failed to fetch Teams member profile: {res.status_code} - {res.text}")
                return None
    except Exception as e:
        print(f"[ERROR] Exception fetching Teams member profile: {e}")
        return None

async def get_conversation_members(service_url: str, conversation_id: str) -> Optional[list]:
    """
    Retrieves all members of a conversation from the Bot Connector.
    """
    if not service_url.endswith("/"):
        service_url += "/"
    url = f"{service_url}v3/conversations/{conversation_id}/members"
    access_token = await _get_bot_token()
    
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers, timeout=10.0)
            if res.status_code == 200:
                return res.json()
            else:
                print(f"[ERROR] Failed to fetch conversation members: {res.status_code} - {res.text}")
                return None
    except Exception as e:
        print(f"[ERROR] Exception fetching conversation members: {e}")
        return None

