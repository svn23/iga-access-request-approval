"""Safe adapter boundary for MidPoint JAR provisioning."""
import logging
from urllib.parse import urlparse, quote
import httpx
from app.config import settings

logger = logging.getLogger("teams_bot.provisioning")


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validate_midpoint_url(url: str) -> None:
    parsed = urlparse(url)
    if not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("MIDPOINT_BASE_URL must be an absolute URL without embedded credentials.")
    # HTTPS required in general; allow plain HTTP only for local-dev loopback hosts.
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return
    raise ValueError("MIDPOINT_BASE_URL must be HTTPS (plain HTTP allowed only for localhost).")


async def _catalog_get(path: str, params: dict) -> list[dict]:
    """GET a JAR dynamic-catalog endpoint and return its `items` list ([] on failure)."""
    if not settings.midpoint_base_url or not settings.midpoint_api_token:
        return []
    _validate_midpoint_url(settings.midpoint_base_url)
    url = f"{settings.midpoint_base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {settings.midpoint_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), verify=settings.midpoint_tls_verify) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        return [i for i in items if isinstance(i, dict) and i.get("name") and i.get("oid")]
    except Exception:
        logger.warning("Dynamic catalog fetch failed for %s", path)
        return []


async def fetch_pending_approvals_count(email: str) -> int:
    """Count of midPoint approval work items awaiting this user (as approver). 0 on any failure.

    midPoint is the source of truth; this is best-effort so the greeting always renders — a slow or
    unreachable JAR yields 0 (no banner) rather than blocking the card.
    """
    if not email or not settings.midpoint_base_url or not settings.midpoint_api_token:
        return 0
    try:
        _validate_midpoint_url(settings.midpoint_base_url)
        url = f"{settings.midpoint_base_url.rstrip('/')}/api/v1/users/pending-approvals"
        headers = {"Authorization": f"Bearer {settings.midpoint_api_token}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0), verify=settings.midpoint_tls_verify) as client:
            response = await client.get(url, params={"email": email}, headers=headers)
            response.raise_for_status()
            data = response.json()
        return int(data.get("count", 0)) if isinstance(data, dict) else 0
    except Exception:
        logger.warning("Pending-approvals count fetch failed for %s", email)
        return 0


async def fetch_user_applications(email: str) -> list[dict]:
    """Applications available to the user's org(s): [{name, oid}]."""
    return await _catalog_get("/api/v1/catalog/applications", {"email": email})


async def fetch_user_groups(email: str) -> list[dict]:
    """Groups/orgs the user belongs to: [{name, oid}]."""
    return await _catalog_get("/api/v1/catalog/orgs", {"email": email})


async def fetch_application_roles(application_oid: str) -> list[dict]:
    """Access levels (roles) for a given application: [{name, oid}]."""
    return await _catalog_get("/api/v1/catalog/roles", {"applicationOid": application_oid})


async def fetch_requestable_access(access_type: str | None = None) -> list[dict]:
    """Live requestable menu from the JAR: [{type, midpointType, name, oid, description, risk}].

    Real midPoint data — RoleType/ServiceType flagged requestable=true. Pass `access_type`
    (e.g. "role") to fetch just that category. Returns [] on failure.
    """
    if not settings.midpoint_base_url or not settings.midpoint_api_token:
        return []
    _validate_midpoint_url(settings.midpoint_base_url)
    url = f"{settings.midpoint_base_url.rstrip('/')}/api/v1/catalog/requestable"
    params = {"type": access_type} if access_type else {}
    headers = {"Authorization": f"Bearer {settings.midpoint_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), verify=settings.midpoint_tls_verify) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        return [i for i in items if isinstance(i, dict) and i.get("name") and i.get("oid") and i.get("type")]
    except Exception:
        logger.warning("Requestable access fetch failed")
        return []


async def fetch_user_profile(email: str) -> dict | None:
    """Resolve the logged-in user's midPoint profile via the JAR (used when a session starts).

    Returns {oid, name, fullName, email, assignmentCount} or None if unavailable/not found.
    """
    if not email or not settings.midpoint_base_url or not settings.midpoint_api_token:
        return None
    _validate_midpoint_url(settings.midpoint_base_url)
    url = f"{settings.midpoint_base_url.rstrip('/')}/api/v1/users/lookup"
    headers = {"Authorization": f"Bearer {settings.midpoint_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), verify=settings.midpoint_tls_verify) as client:
            response = await client.get(url, params={"email": email}, headers=headers)
            response.raise_for_status()
            data = response.json()
        user = data.get("user") if isinstance(data, dict) else None
        return user if isinstance(user, dict) else None
    except Exception:
        logger.warning("User profile lookup failed for %s", email)
        return None


async def fetch_user_manager(email: str) -> dict | None:
    """Resolve the requester's line manager from midPoint (org:manager). {oid,name,fullName,email} or None."""
    if not email or not settings.midpoint_base_url or not settings.midpoint_api_token:
        return None
    _validate_midpoint_url(settings.midpoint_base_url)
    url = f"{settings.midpoint_base_url.rstrip('/')}/api/v1/users/manager"
    headers = {"Authorization": f"Bearer {settings.midpoint_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), verify=settings.midpoint_tls_verify) as client:
            response = await client.get(url, params={"email": email}, headers=headers)
            response.raise_for_status()
            data = response.json()
        mgr = data.get("manager") if isinstance(data, dict) else None
        return mgr if isinstance(mgr, dict) else None
    except Exception:
        logger.warning("Manager lookup failed for %s", email)
        return None


async def fetch_user_requests(email: str) -> list[dict]:
    """The user's access requests straight from midPoint (approval cases) — the source of truth.

    Returns a list of {target, state, outcome, when}; [] when unavailable. The bot stores no
    request state of its own.
    """
    if not email or not settings.midpoint_base_url or not settings.midpoint_api_token:
        return []
    _validate_midpoint_url(settings.midpoint_base_url)
    url = f"{settings.midpoint_base_url.rstrip('/')}/api/v1/users/requests"
    headers = {"Authorization": f"Bearer {settings.midpoint_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), verify=settings.midpoint_tls_verify) as client:
            response = await client.get(url, params={"email": email}, headers=headers)
            response.raise_for_status()
            data = response.json()
        reqs = data.get("requests") if isinstance(data, dict) else None
        return reqs if isinstance(reqs, list) else []
    except Exception:
        logger.warning("Requests lookup failed for %s", email)
        return []


async def provision_with_midpoint(payload: dict) -> dict:
    """Call Java MidPoint APIs. Payload contains requestId, requester, provisioning, approval, metadata.

    Returns the JAR receipt dict, which includes the created approval-case identifiers
    (`caseOid`, `workItemId`, `targetOid`) when the target has an approval policy — the bot uses
    these to drive the manager approval card directly (the midPoint push notifier is a known
    non-firing blocker). Absent when the request auto-executed (no approval policy).
    """
    if not settings.midpoint_base_url or not settings.midpoint_api_token:
        raise RuntimeError("MidPoint provisioning is enabled but its URL or service token is missing.")
    _validate_midpoint_url(settings.midpoint_base_url)

    base_url = settings.midpoint_base_url.rstrip('/')
    requester = payload.get("requester", {})
    provisioning = payload.get("provisioning", {})
    approval = payload.get("approval", {})
    access_until = approval.get("accessUntil")

    if access_until:
        endpoint = f"{base_url}/api/v1/requests/temporary"
    else:
        endpoint = f"{base_url}/api/v1/requests/permanent"

    request_payload = {
        "transactionId": payload.get("requestId", ""),
        "requester": {
            "midpointUserOid": requester.get("midpointUserOid"),
            "email": requester.get("email", "")
        },
        "provisioning": {
            "targetType": provisioning.get("targetType", "resource"),
            "targetName": provisioning.get("targetName", ""),
            # Exact object oid when known (from the requestable menu) so the JAR grants
            # precisely that role/service/group instead of resolving by name.
            "targetOid": provisioning.get("targetOid"),
        },
        "approval": {
            "accessUntil": access_until
        } if access_until else None
    }

    if not request_payload["approval"]:
        request_payload.pop("approval")

    headers = {"Authorization": f"Bearer {settings.midpoint_api_token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), verify=settings.midpoint_tls_verify) as client:
        response = await client.post(endpoint, json=request_payload, headers=headers)
        response.raise_for_status()
        body = response.json() if response.content else {}

    receipt = body if isinstance(body, dict) else {}
    # The JAR nests case identifiers + approvers under "details"; flatten them to the top level so
    # callers read them uniformly alongside the top-level "status" (accepted | already_pending).
    details = receipt.get("details") if isinstance(receipt.get("details"), dict) else {}
    for k in ("caseOid", "workItemId", "targetOid", "targetName", "targetType", "approvers"):
        if k not in receipt and k in details:
            receipt[k] = details[k]
    return receipt


async def midpoint_decision(*, email: str, target_name: str, target_type: str, approve: bool,
                            transaction_id: str, comment: str | None = None, access_until: str | None = None,
                            case_oid: str | None = None, work_item_id: str | None = None) -> dict:
    """Complete a midPoint approval work item through the JAR (approve/reject).

    Calls POST /api/v1/requests/{email}/approve|reject. When caseOid + workItemId are supplied
    (always the case for a Teams approval card, which carries them from the RAISED notification)
    the JAR completes that work item directly — no user/target/catalog resolution, so approval
    works even when the notification carried no requestedItem. Otherwise the JAR falls back to
    locating the open work item by (user, target) and completing it, or a direct assign/unassign.
    Returns the JAR receipt dict.
    """
    if not settings.midpoint_base_url or not settings.midpoint_api_token:
        raise RuntimeError("MidPoint integration is enabled but its URL or service token is missing.")
    _validate_midpoint_url(settings.midpoint_base_url)
    base_url = settings.midpoint_base_url.rstrip('/')
    verb = "approve" if approve else "reject"
    # For the email path segment, fall back to a placeholder when the notification omitted the
    # requester email — the JAR ignores it on the direct caseOid/workItemId completion path.
    email_seg = email or "unknown@unknown"
    endpoint = f"{base_url}/api/v1/requests/{quote(email_seg, safe='')}/{verb}"
    payload = {
        "transactionId": transaction_id,
        "email": email or None,
        "targetName": target_name or None,
        "targetType": target_type or "role",
        "comment": comment or "",
    }
    if case_oid:
        payload["caseOid"] = case_oid
    if work_item_id:
        payload["workItemId"] = work_item_id
    if access_until:
        payload["accessUntil"] = access_until
    headers = {"Authorization": f"Bearer {settings.midpoint_api_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), verify=settings.midpoint_tls_verify) as client:
        response = await client.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        return response.json() if response.content else {}
