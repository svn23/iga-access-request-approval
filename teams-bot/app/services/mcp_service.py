# pyrefly: ignore [missing-import]
import httpx
import uuid
import logging
from typing import Dict, List, Optional
from app.config import settings

logger = logging.getLogger("teams_bot.mcp_service")

# In-memory cache for MCP tool schemas
_mcp_tool_schemas: Dict[str, dict] = {}

# Default fallback schema for 'request_access' if MCP server is offline
FALLBACK_SCHEMAS = {
    "request_access": {
        "type": "object",
        "properties": {
            "system": {
                "type": "string",
                "description": "The target system or resource name (e.g. AWS, Zoho, Postgres)"
            },
            "role": {
                "type": "string",
                "description": "The requested role or access level (e.g. Admin, Reader, Standard Access)"
            },
            "reason": {
                "type": "string",
                "description": "The justification/reason for the access request"
            }
        },
        "required": ["system", "role", "reason"]
    }
}

async def fetch_mcp_tools() -> None:
    """
    Attempts to fetch tools dynamically from the configured MCP server.
    Registers the session first via the standard initialize endpoint.
    """
    if not settings.mcp_url:
        logger.info("No MCP_URL configured. Using fallback schemas.")
        return

    session_id = str(uuid.uuid4())
    headers = {
        "mcp-session-id": session_id,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            # 1. Initialize session
            init_payload = {
                "jsonrpc": "2.0",
                "method": "initialize",
                "id": 0,
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "teams-bot-client",
                        "version": "1.0.0"
                    }
                }
            }
            logger.info(f"Initializing MCP session for tools query: {settings.mcp_url}")
            init_res = await client.post(settings.mcp_url, json=init_payload, headers=headers, timeout=4.0)
            if init_res.status_code != 200:
                logger.warning(f"MCP initialization failed during tools fetch: {init_res.status_code}")
                return

            # 2. Query tools/list
            list_payload = {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "id": 1,
                "params": {}
            }
            list_res = await client.post(settings.mcp_url, json=list_payload, headers=headers, timeout=4.0)
            if list_res.status_code == 200:
                data = list_res.json()
                tools = data.get("result", {}).get("tools", [])
                for tool in tools:
                    name = tool.get("name")
                    schema = tool.get("inputSchema")
                    if name and schema:
                        _mcp_tool_schemas[name] = schema
                try:
                    from app.services import bot_handler
                    bot_handler._user_facing_schema_cache = None
                except Exception:
                    pass
                logger.info(f"Successfully loaded {len(tools)} tool schemas from MCP server.")
            else:
                logger.warning(f"MCP tools/list failed with status: {list_res.status_code}")
        except Exception as e:
            logger.warning(f"Failed to fetch MCP tools dynamically ({e}). Using offline fallbacks.")

def get_tool_schema(tool_name: str) -> dict:
    """
    Returns the schema for a given tool name, falling back to local configurations if offline.
    """
    if tool_name in _mcp_tool_schemas:
        return _mcp_tool_schemas[tool_name]
    return FALLBACK_SCHEMAS.get(tool_name, {
        "type": "object",
        "properties": {},
        "required": []
    })

def resolve_access_tool_name() -> str:
    """
    Dynamically identifies the access request tool name from the registered tools.
    Falls back to 'request_access' if not found.
    """
    for name in _mcp_tool_schemas.keys():
        if "access" in name.lower() or "provision" in name.lower():
            return name
    return "request_access"
