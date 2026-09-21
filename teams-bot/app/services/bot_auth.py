"""Inbound Bot Framework token validation; no unsigned Teams activities in production."""
import time
from typing import Any
import httpx
import jwt
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm
from app.config import settings

_keys: dict[str, Any] = {}
_keys_expires_at = 0.0
_issuer: str | None = None


async def _get_signing_keys() -> tuple[dict[str, Any], str]:
    global _keys, _keys_expires_at, _issuer
    if _keys and time.monotonic() < _keys_expires_at and _issuer:
        return _keys, _issuer
    async with httpx.AsyncClient(timeout=5.0) as client:
        metadata = (await client.get("https://login.botframework.com/v1/.well-known/openidconfiguration")).json()
        jwks = (await client.get(metadata["jwks_uri"])).json()["keys"]
    _keys = {key["kid"]: RSAAlgorithm.from_jwk(key) for key in jwks if key.get("kid")}
    _issuer = metadata["issuer"]
    _keys_expires_at = time.monotonic() + 3600
    return _keys, _issuer


async def validate_bot_framework_authorization(authorization: str | None) -> None:
    if not settings.validate_bot_auth:
        return
    if settings.allow_unauthenticated_dev:
        return
    if not settings.teams_app_id:
        raise HTTPException(status_code=503, detail="Bot authentication is not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bot Framework authorization token.")
    token = authorization.removeprefix("Bearer ").strip()

    # Token mode: simple bearer token validation (for testing)
    if settings.auth_mode == "token":
        if not settings.teams_app_password:
            raise HTTPException(status_code=503, detail="Bot password not configured.")
        if token != settings.teams_app_password:
            raise HTTPException(status_code=401, detail="Invalid authorization token.")
        return

    # JWT mode: Teams Bot Framework JWT validation (production)
    try:
        header = jwt.get_unverified_header(token)
        key_map, issuer = await _get_signing_keys()
        key = key_map.get(header.get("kid"))
        if not key:
            raise jwt.InvalidTokenError("Unknown signing key")
        jwt.decode(token, key=key, algorithms=["RS256"], audience=settings.teams_app_id, issuer=issuer,
                   options={"require": ["exp", "iat", "aud", "iss"]})
    except (jwt.PyJWTError, KeyError, ValueError, httpx.HTTPError):
        raise HTTPException(status_code=401, detail="Invalid Bot Framework authorization token.")
