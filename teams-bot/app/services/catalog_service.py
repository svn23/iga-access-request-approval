"""Access catalog source. MidPoint can replace the small safe fallback at deployment time."""
import logging
import asyncio
import time
from typing import Any
import httpx
from app.config import settings
from app.services.provisioning_service import _validate_midpoint_url

logger = logging.getLogger("teams_bot.catalog")
FALLBACK_CATALOG = [
    {"system": "AWS", "roles": ["ReadOnly", "Developer", "Administrator"], "risk": "high"},
    {"system": "GitHub", "roles": ["Read", "Write", "Maintain"], "risk": "medium"},
    {"system": "VPN", "roles": ["Standard Access"], "risk": "medium"},
    {"system": "Salesforce", "roles": ["Read", "Standard User"], "risk": "medium"},
]

_catalog_cache: list[dict[str, Any]] = []
_cache_updated_at = 0.0
_catalog_lock = asyncio.Lock()

def _cache_is_fresh() -> bool:
    return bool(_catalog_cache) and time.monotonic() - _cache_updated_at < settings.catalog_sync_minutes * 60

async def get_access_catalog() -> list[dict[str, Any]]:
    """Return the last known-good catalog; refresh at most once every 30 minutes."""
    global _catalog_cache, _cache_updated_at
    if _cache_is_fresh():
        return _catalog_cache
    async with _catalog_lock:
        if _cache_is_fresh():
            return _catalog_cache
        if not settings.midpoint_base_url or not settings.midpoint_api_token:
            _catalog_cache, _cache_updated_at = FALLBACK_CATALOG, time.monotonic()
            return _catalog_cache
        try:
            _validate_midpoint_url(settings.midpoint_base_url)
            catalog_url = f"{settings.midpoint_base_url.rstrip('/')}/api/v1/access-catalog"
            async with httpx.AsyncClient(timeout=5.0, verify=settings.midpoint_tls_verify) as client:
                response = await client.get(catalog_url, headers={"Authorization": f"Bearer {settings.midpoint_api_token}"})
                response.raise_for_status()
                data = response.json()
                catalog = data if isinstance(data, list) else data.get("items", [])
            validated = [item for item in catalog if isinstance(item, dict) and isinstance(item.get("system"), str) and isinstance(item.get("roles"), list)]
            if not validated:
                raise ValueError("MidPoint returned no valid catalog items")
            _catalog_cache, _cache_updated_at = validated, time.monotonic()
        except Exception:
            logger.warning("MidPoint catalog refresh failed; retaining last known-safe catalog.")
            if not _catalog_cache:
                _catalog_cache, _cache_updated_at = FALLBACK_CATALOG, time.monotonic()
        return _catalog_cache


async def catalog_sync_loop() -> None:
    """Warm the catalog on startup and refresh it on the configured 30-minute cadence."""
    while True:
        await get_access_catalog()
        await asyncio.sleep(max(settings.catalog_sync_minutes, 1) * 60)
