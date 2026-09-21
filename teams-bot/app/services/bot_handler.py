import json
import logging
import re
import time
import uuid
from datetime import date, timedelta
# pyrefly: ignore [missing-import]
from fastapi import HTTPException
from app.config import settings
from app.services.db_service import get_db_connection, get_session_state, set_session_state
from app.services.teams_service import (
    create_proactive_conversation,
    send_proactive_card,
    send_reply_to_teams,
    send_proactive_message,
    send_card_to_teams,
    update_card_in_teams,
    get_teams_member_profile,
    get_conversation_members,
    send_typing_activity,
)
from app.services.graph_service import get_user_manager_profile
from app.services.mcp_service import get_tool_schema, resolve_access_tool_name
from app.services.llm import generate_response
from app.services.cards import greeting_card, access_request_form, confirmation_card, requests_card, manager_queue_card, safe_card_text, wizard_type_card, wizard_target_card, wizard_access_card, midpoint_decision_locked_card, requestable_type_card, requestable_target_card, requestable_confirm_card
from app.services.provisioning_service import fetch_user_applications, fetch_user_groups, fetch_application_roles, fetch_requestable_access, fetch_user_profile, fetch_user_manager
from app.services.catalog_service import get_access_catalog
from app.services.audit_service import record_audit_event
from app.services import session_manager, metrics_service
from app.services.cards import locked_card, midpoint_requests_card

logger = logging.getLogger("teams_bot.bot_handler")

# Requester self-service actions governed by the conversation-session lifecycle.
_SESSION_ACTIONS = {
    "open_access_form", "req_pick_type", "submit_requestable", "confirm_requestable",
    "submit_access_form", "confirm_access_form", "wizard_pick_type", "wizard_pick_target",
    "submit_application_request", "decline_access_form",
    "cancel_requestable",  # cancel from the review/confirm card (distinct from cancel_request)
}
# Decisive final actions: take the PROCESSING lock, then complete the session.
_FINAL_ACTIONS = {"confirm_requestable", "confirm_access_form"}
# Manager/approver actions live in a different conversation (no requester session);
# idempotency + one-time tokens apply, but never the requester-session gating.
_APPROVAL_ACTIONS = {
    "approve", "approve_temporary", "reject", "more_info",
    "mp_approve", "mp_approve_temporary", "mp_reject", "revoke_temporary",
}

_user_facing_schema_cache = None

_GREETING_PATTERN = re.compile(r"^(?:hi+|hey+|hello+|help|good\s+(?:morning|afternoon|evening))\W*$", re.IGNORECASE)
_FORM_FIELD_LIMITS = {"target_type": 40, "target_name": 120, "role": 120, "reason": 1000, "duration": 20}
_FIELD_LABELS = {"target_type": "access type", "target_name": "target name", "role": "access level", "reason": "business justification", "duration": "access duration"}
_DESCRIPTION_LIMIT = 2000


def _first_name(activity: dict) -> str:
    """Treat Teams as the identity authority; never use user-supplied card text for identity."""
    display_name = str(activity.get("from", {}).get("name") or "there").strip()
    return re.sub(r"[^A-Za-z'-]", "", display_name.split()[0])[:60] or "there"


def _resolve_requester_email(activity: dict) -> str | None:
    """Resolve the requester's email from the local directory (by Teams id, then display name)."""
    teams_id = str(activity.get("from", {}).get("id") or "")
    name = str(activity.get("from", {}).get("name") or "")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for column, value in (("teams_user_id", teams_id), ("username", name)):
            if not value:
                continue
            cursor.execute(f"SELECT email FROM bot_user_directory WHERE {column} = %s AND email IS NOT NULL LIMIT 1", (value,))
            row = cursor.fetchone()
            if row and row[0]:
                return str(row[0])
    finally:
        cursor.close()
        conn.close()
    return None


_MOCK_TEAMS_ID_PREFIXES = ("29:approver-", "29:requester-")


async def _requester_email(activity: dict) -> str | None:
    """The requester's real login email (matches midPoint), from the Teams profile, else directory."""
    service_url = activity.get("serviceUrl")
    conversation_id = activity.get("conversation", {}).get("id")
    user_id = activity.get("from", {}).get("id")
    if service_url and conversation_id and user_id:
        try:
            profile = await get_teams_member_profile(service_url, conversation_id, user_id)
            if profile:
                email = profile.get("email") or profile.get("userPrincipalName")
                if email and "@" in email:
                    return str(email)
        except Exception:
            logger.info("Teams member profile email unavailable; using directory fallback")
    return _resolve_requester_email(activity)


async def _check_eligibility(email: str | None) -> tuple[bool, dict | None, str]:
    """Requester must exist in midPoint AND hold the mandatory 'End user' role. (ok, profile, reason)."""
    if not email:
        return False, None, "no_email"
    profile = await fetch_user_profile(email)
    if not profile:
        return False, None, "not_in_midpoint"
    roles = [str(r).strip().lower() for r in (profile.get("roles") or [])]
    if "end user" not in roles:
        return False, profile, "no_enduser"
    return True, profile, "ok"


def _ineligible_message(reason: str, email: str | None) -> str:
    if reason == "no_email":
        return "I couldn't determine your work email, so I can't verify your access. Please contact your administrator."
    if reason == "not_in_midpoint":
        return (f"You're not registered in the identity system"
                f"{f' (no user found for {email})' if email else ''}, so you can't request access yet. "
                "Please contact your administrator to get an account.")
    if reason == "no_enduser":
        return ("Your identity exists, but the mandatory **End user** role isn't assigned to you. "
                "Ask your administrator to grant it before you can request access.")
    return "You're not eligible to request access right now. Please contact your administrator."


def _remember_user(emails: list[str | None], teams_id: str | None) -> None:
    """Map a person's email(s) → real Teams id in the local directory so that, once they have
    used the bot, they can be DM'd as someone else's manager. We store BOTH the Teams login
    email and the midPoint emailAddress (they may differ) so manager lookup by either resolves.
    `username` is set to the email to avoid the table's UNIQUE(username) collisions."""
    if not teams_id or teams_id.startswith(_MOCK_TEAMS_ID_PREFIXES):
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for email in {e.strip().lower() for e in emails if e and "@" in e}:
            cursor.execute("UPDATE bot_user_directory SET teams_user_id=%s WHERE lower(email)=lower(%s)",
                           (teams_id, email))
            if cursor.rowcount == 0:
                cursor.execute("INSERT OR IGNORE INTO bot_user_directory (username, email, teams_user_id) VALUES (%s, %s, %s)",
                               (email, email, teams_id))
        conn.commit()
    except Exception as exc:
        logger.warning("Could not remember user %s: %s", emails, exc)
    finally:
        cursor.close()
        conn.close()


def _teams_id_for_email(email: str | None) -> str | None:
    """Resolve a real (non-placeholder) Teams user id from the local directory by email."""
    if not email:
        return None
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT teams_user_id FROM bot_user_directory WHERE lower(email) = lower(%s) AND teams_user_id IS NOT NULL LIMIT 1", (email,))
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    tid = row[0] if row and row[0] else None
    if tid and tid.startswith(_MOCK_TEAMS_ID_PREFIXES):
        return None
    return tid


def _validated_form_value(value: object, field: str) -> str:
    label = _FIELD_LABELS.get(field, field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Please provide the {label}.")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError(f"The {label} contains unsupported characters.")
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        raise ValueError(f"Please provide the {label}.")
    if len(cleaned) > _FORM_FIELD_LIMITS[field]:
        raise ValueError(f"The {label} is too long (max {_FORM_FIELD_LIMITS[field]} characters).")
    return cleaned


def _optional_description(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > _DESCRIPTION_LIMIT:
        raise ValueError("description is invalid.")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError("description contains unsupported control characters.")
    return " ".join(value.split()).strip()


def _risk_level(catalog: list[dict], target_name: str, role: str, duration: str) -> str:
    item = next((entry for entry in catalog if entry.get("system") == target_name), {})
    configured = str(item.get("risk", "medium")).lower()
    if any(word in role.lower() for word in ("admin", "owner", "privileged", "root")) or duration == "permanent":
        return "high"
    return configured if configured in {"low", "medium", "high"} else "medium"


async def _submit_access_form(activity: dict, value: dict) -> dict:
    """Persist a form request using a server-owned requester identity, then route it."""
    try:
        target_type = _validated_form_value(value.get("target_type", "resource"), "target_type")
        target_name = _validated_form_value(value.get("target_name"), "target_name")
        role = _validated_form_value(value.get("role"), "role")
        reason = _validated_form_value(value.get("reason"), "reason")
        duration = _validated_form_value(value.get("duration", "permanent"), "duration")
        description = _optional_description(value.get("description"))
    except ValueError as exc:
        await send_reply_to_teams(activity, f"I could not submit that request: {exc}")
        return {"status": "error", "message": "Invalid form data."}

    if duration not in {"30", "90", "permanent"}:
        await send_reply_to_teams(activity, "I could not submit that request: invalid access duration.")
        return {"status": "error", "message": "Invalid duration."}
    catalog = await get_access_catalog()
    # Role/group requests come from the dynamic wizard where the target is a real
    # MidPoint object; validate against the flat catalog. Application/resource requests
    # keep the per-system role validation.
    if target_type in {"role", "group"}:
        catalog_roles = {r for item in catalog for r in item.get("roles", [])}
        if target_type == "role" and target_name not in catalog_roles:
            await send_reply_to_teams(activity, "That access level is no longer available. Please start again and pick from the current options.")
            return {"status": "error", "message": "Role is not in the access catalog."}
    else:
        permitted_roles = next((item.get("roles", []) for item in catalog if item.get("system") == target_name), [])
        if role not in permitted_roles:
            await send_reply_to_teams(activity, "That resource and role combination is no longer available. Please reopen the form and select an available option.")
            return {"status": "error", "message": "Resource-role pair is not in the access catalog."}
    risk_level = _risk_level(catalog, target_name, role, duration)
    requester = str(activity.get("from", {}).get("name") or "Employee").strip()[:255]
    requester_teams_id = str(activity.get("from", {}).get("id") or "")[:255]
    conversation_id = str(activity.get("conversation", {}).get("id") or "")[:255]
    service_url = str(activity.get("serviceUrl") or "")[:255]
    # E-mail is intentionally not accepted from the card. A future Entra lookup
    # can populate it from a trusted directory source.
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO bot_access_requests (requester, resource, approver, status, requested_role, requested_target_type, requested_target_name, requester_conversation_id, service_url, requester_teams_id, duration_days, risk_level, mcp_payload) VALUES (%s, %s, 'Pending Resolution', 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (requester, target_name, role, target_type, target_name, conversation_id, service_url, requester_teams_id, duration, risk_level, json.dumps({"target_type": target_type, "target_name": target_name, "role": role, "reason": reason, "description": description, "duration": duration, "risk": risk_level}))
    )
    conn.commit()
    request_id = cursor.lastrowid
    cursor.close()
    conn.close()
    record_audit_event(request_id, requester, "request_submitted", {"resource": target_name, "target_type": target_type, "role": role, "risk": risk_level, "duration": duration})
    await route_card_to_manager(activity, request_id, requester, "Not available", target_name, role)
    return {"status": "success", "request_id": request_id}


async def _submit_requestable(activity: dict, value: dict) -> dict:
    """Persist a request chosen from the live requestable menu, then route it to the manager.

    The selection came from the authoritative midPoint requestable list, so it is trusted
    directly (no per-system role pairing check); the exact object oid is carried through.
    """
    itype = str(value.get("target_type") or "role").strip().lower()
    if itype not in {"role", "service", "resource"}:
        await send_reply_to_teams(activity, "That access type is not supported.")
        return {"status": "error", "message": "Unsupported target type."}
    target_name = str(value.get("target_name") or "").strip()[:120]
    target_oid = str(value.get("target_oid") or "").strip()[:120]
    if not target_name or not target_oid:
        await send_reply_to_teams(activity, "This request is missing its target details. Please start again.")
        return {"status": "error", "message": "Missing target name/oid."}
    # Business justification is no longer collected; keep an empty placeholder for the record.
    reason = ""
    duration_days = {"Permanent": "permanent", "30 days": "30", "90 days": "90"}.get(str(value.get("duration") or "Permanent"), "permanent")
    risk_level = "high" if (duration_days == "permanent" or any(w in target_name.lower() for w in ("admin", "owner", "root", "privileg"))) else "medium"

    requester = str(activity.get("from", {}).get("name") or "Employee").strip()[:255]
    requester_teams_id = str(activity.get("from", {}).get("id") or "")[:255]
    conversation_id = str(activity.get("conversation", {}).get("id") or "")[:255]
    service_url = str(activity.get("serviceUrl") or "")[:255]
    requester_email = await _requester_email(activity)
    # Final eligibility guard (exists in midPoint + holds 'End user').
    ok, _profile, reason = await _check_eligibility(requester_email)
    if not ok:
        await send_reply_to_teams(activity, _ineligible_message(reason, requester_email))
        return {"status": "blocked", "reason": reason}
    payload = {"target_type": itype, "target_name": target_name, "target_oid": target_oid, "role": target_name,
               "reason": reason, "duration": duration_days, "risk": risk_level}

    # midPoint is the single source of truth: the bot does NOT persist the request in its own DB.
    # A correlation ref is generated only for logging/traceability; the authoritative record is the
    # approval case created in midPoint below.
    request_id = f"REQ-{uuid.uuid4().hex[:12]}"
    record_audit_event(None, requester, "request_submitted", {"ref": request_id, "resource": target_name, "target_type": itype, "role": target_name, "risk": risk_level, "duration": duration_days, "target_oid": target_oid})

    if settings.manager_notify_mode == "midpoint":
        # Submit the self-request to midPoint (creates the approval case — the source of truth). The
        # midPoint notifier that would push RAISED to the connector is a known non-firing blocker, so
        # after submit the bot resolves the line manager and DMs the approval card itself (below). The
        # card still completes the real midPoint work item, so approval state stays authoritative.
        if not requester_email or "@" not in requester_email:
            await send_reply_to_teams(activity, "I couldn't determine your work email, so I can't submit this to the identity system.")
            return {"status": "error", "message": "No requester email."}
        submit_payload = {
            "requestId": request_id,
            "requester": {"email": requester_email},
            "provisioning": {"targetType": itype, "targetName": target_name, "targetOid": target_oid},
        }
        # Temporary duration → accessUntil (JAR caps at 30 days, so clamp). An explicit
        # access_until_override (from the single-card Custom end date) wins when present.
        override = str(value.get("access_until_override") or "").strip()
        if override:
            submit_payload["approval"] = {"accessUntil": override}
        elif duration_days in {"30", "90"}:
            days = min(int(duration_days), 30)
            submit_payload["approval"] = {"accessUntil": (date.today() + timedelta(days=days)).isoformat()}
        try:
            from app.services.provisioning_service import provision_with_midpoint
            receipt = await provision_with_midpoint(submit_payload)
            logger.info("midPoint submit ok for request %s: %s", request_id, receipt.get("message") if isinstance(receipt, dict) else receipt)
        except Exception:
            logger.exception("midPoint submit failed for request %s", request_id)
            await send_reply_to_teams(activity, "Sorry — the identity system rejected the submission. Please try again shortly.")
            return {"status": "error", "message": "midPoint submit failed."}
        # Drive the manager approval directly. The midPoint case now exists (source of truth); the
        # notifier that would push it to the middleware is a known non-firing blocker, so the bot
        # resolves the line manager and DMs the approval card itself. midPoint still owns approval
        # state — the card completes the real work item.
        case_oid = str((receipt or {}).get("caseOid") or "").strip()
        work_item_id = str((receipt or {}).get("workItemId") or "").strip()
        if override:
            duration_label = f"Temporary — until {override}"
        elif duration_days in {"30", "90"}:
            duration_label = f"{min(int(duration_days), 30)} days"
        else:
            duration_label = "Permanent"
        item_txt = safe_card_text(target_name)
        # Idempotency: midPoint already has an open approval case for this exact (user, target).
        if str((receipt or {}).get("status") or "").lower() == "already_pending":
            await send_reply_to_teams(activity, f"You already have a pending request for **{item_txt}** awaiting approval — I haven’t created a duplicate.")
            return {"status": "duplicate", "request_id": request_id, "caseOid": case_oid}
        _approvers = (receipt or {}).get("approvers")
        _approvers = _approvers if isinstance(_approvers, list) else None
        notify_status = await _notify_manager_of_request(
            requester_email=requester_email, requester_name=requester,
            requested_item=target_name, target_type=itype, access_duration=duration_label,
            case_oid=case_oid, work_item_id=work_item_id, approvers=_approvers,
        )
        if not case_oid:
            # No approval case → the target had no approval policy and midPoint auto-executed it.
            await send_reply_to_teams(activity, f"Your request for **{item_txt}** has been granted.")
        elif notify_status == "delivered":
            await send_reply_to_teams(activity, f"Your request for **{item_txt}** has been submitted and sent to your manager for approval.")
        elif notify_status == "no_approver":
            record_audit_event(None, requester, "request_no_approver", {"ref": request_id, "caseOid": case_oid, "resource": target_name})
            await send_reply_to_teams(activity, f"Your request for **{item_txt}** was submitted, but no approver is configured for you in the identity system. Please contact your administrator — it will remain pending until an approver is assigned.")
        elif notify_status == "disabled":
            # Bot-direct notify is off (midPoint notifier authoritative) — it delivers the card.
            await send_reply_to_teams(activity, f"Your request for **{item_txt}** has been submitted for approval.")
        else:  # undeliverable
            await send_reply_to_teams(activity, f"Your request for **{item_txt}** was submitted and is pending approval. We couldn’t reach your manager on Teams right now — they can still action it from their approvals, and you’ll be notified of the outcome.")
        return {"status": "success", "request_id": request_id, "caseOid": case_oid, "notify": notify_status}
    else:
        # Legacy bot-driven routing: the bot resolves the manager and DMs the approval card.
        await route_card_to_manager(activity, request_id, requester, requester_email or "Not available", target_name, target_name)
    return {"status": "success", "request_id": request_id}


async def _notify_manager_of_request(*, requester_email: str, requester_name: str, requested_item: str,
                                     target_type: str, access_duration: str,
                                     case_oid: str, work_item_id: str,
                                     approvers: list[str] | None = None) -> str:
    """Resolve the requester's approver(s) and DM the midPoint approval card proactively.

    Bot-driven because the midPoint push notifier is a known non-firing blocker; midPoint still owns
    approval state (the card completes the real work item via caseOid/workItemId).

    Approver resolution order: the actual work-item assignees midPoint routed the case to (`approvers`,
    from the JAR — the authoritative approver set, DM'd to all) → line manager (org:manager) →
    FALLBACK_APPROVER_EMAIL. Self-approval (approver == requester) is dropped. Returns a status the
    caller surfaces: "delivered" | "no_approver" | "undeliverable" | "disabled" | "no_case".
    """
    if not case_oid:
        return "no_case"  # no approval policy → nothing for a manager to approve
    if not settings.bot_direct_notify:
        return "disabled"  # notifier is authoritative; the bot must not double-notify

    req_l = (requester_email or "").lower()
    # 1) Authoritative: the real work-item assignees (minus the requester, if self-assigned).
    recipients = [e for e in (approvers or []) if e and "@" in str(e) and str(e).lower() != req_l]
    # 2) Fall back to the org line manager (unless it is the requester themselves).
    if not recipients:
        manager = await fetch_user_manager(requester_email)
        manager_email = str((manager or {}).get("email") or "").strip()
        if manager_email and "@" in manager_email and manager_email.lower() != req_l:
            recipients = [manager_email]
        elif manager_email and manager_email.lower() == req_l:
            logger.info("Requester %s is their own manager; routing to fallback approver.", requester_email)
    # 3) Fall back to the configured default approver.
    if not recipients:
        fallback = str(settings.fallback_approver_email or "").strip()
        if fallback and "@" in fallback:
            recipients = [fallback]
        else:
            logger.warning("No approver for %s (no assignee / no line manager / no fallback) — case %s left pending.",
                           requester_email, case_oid)
            return "no_approver"

    from app.services.notify_service import handle_notification
    delivered_any = False
    for recipient in dict.fromkeys(recipients):  # de-dupe, preserve order
        payload = {
            "event": "ACCESS_REQUEST_RAISED",
            "caseOid": case_oid, "workItemId": work_item_id,
            "requesterEmail": requester_email, "requesterName": requester_name,
            "requestedItem": requested_item, "targetType": target_type, "accessDuration": access_duration,
            "recipientEmail": recipient,
            "message": f"{requester_name or requester_email} requested {requested_item}. Approve or reject.",
        }
        try:
            result = await handle_notification(payload)
            ok = result.get("status") == "delivered"
            delivered_any = delivered_any or ok
            logger.info("Manager approval card for case %s → %s: %s", case_oid, recipient, result.get("status"))
        except Exception:
            logger.exception("Failed to DM approval card for case %s to %s", case_oid, recipient)
    return "delivered" if delivered_any else "undeliverable"


def _parse_access_request_duration(value: dict) -> tuple[str, str | None, str | None]:
    """Parse the wizard duration → (token, access_until_iso, error).

    token ∈ permanent | 30 | custom. The connector caps temporary access at 30 days, so a custom
    end date must fall between tomorrow and today+30 (start date is not supported by the connector).
    Reads the card field ids `accessDuration` / `customEndDate`.
    """
    choice = str(value.get("accessDuration") or "permanent").strip().lower()
    if choice == "permanent":
        return "permanent", None, None
    if choice == "30_days":
        return "30", (date.today() + timedelta(days=30)).isoformat(), None
    if choice == "custom":
        raw = str(value.get("customEndDate") or "").strip()
        if not raw:
            return "", None, "Pick a custom end date (within 30 days), or choose Permanent / 30 Days."
        try:
            end = date.fromisoformat(raw)
        except ValueError:
            return "", None, "That custom end date isn't valid — use the date picker."
        today = date.today()
        if not (today < end <= today + timedelta(days=30)):
            return "", None, "Custom access must end between tomorrow and 30 days from today (the connector caps temporary access at 30 days)."
        return "custom", end.isoformat(), None
    return "", None, "Please choose a valid access duration."


async def get_user_facing_schema() -> dict:
    """
    Returns the schema of the request_access tool filtered for user-facing parameters.
    If AI disabled, uses static schema. Otherwise dynamically classifies using LLM.
    """
    global _user_facing_schema_cache
    if _user_facing_schema_cache:
        return _user_facing_schema_cache

    # Static default schema when AI disabled
    static_schema = {
        "type": "object",
        "properties": {
            "target_type": {"type": "string", "description": "The kind of access target (resource, group, or role)"},
            "target_name": {"type": "string", "description": "The target system, group, or role name"},
            "role": {"type": "string", "description": "The requested role or access level (e.g. Admin, Reader, Standard Access)"},
            "reason": {"type": "string", "description": "The justification/reason for the access request"}
        },
        "required": ["target_type", "target_name", "role", "reason"]
    }

    if not settings.enable_ai:
        _user_facing_schema_cache = static_schema
        return static_schema

    tool_name = resolve_access_tool_name()
    schema = get_tool_schema(tool_name)
    properties = schema.get("properties", {})
    
    if not properties:
        # Fallback if no schema is registered
        return {
            "type": "object",
            "properties": {
                "target_type": {"type": "string", "description": "The kind of access target (resource, group, or role)"},
                "target_name": {"type": "string", "description": "The target system, group, or role name"},
                "role": {"type": "string", "description": "The requested role or access level (e.g. Admin, Reader, Standard Access)"},
                "reason": {"type": "string", "description": "The justification/reason for the access request"}
            },
            "required": ["target_type", "target_name", "role", "reason"]
        }
        
    # Query LLM to classify user-facing vs system-managed fields dynamically based on descriptions
    from app.services.llm import generate_response
    prompt = (
        "You are an expert system analyzing an API tool schema for an Access Request Bot.\n"
        "Your job is to identify which properties must be collected from the user (requester) "
        "and which properties are system-managed/context-provided (e.g. auto-generated request IDs, "
        "requester profiles, approval status/decisions, metadata, or timestamps).\n\n"
        f"Tool Schema Properties:\n{json.dumps(properties, indent=2)}\n\n"
        "Rules:\n"
        "1. Exclude any system-managed or auto-generated fields (like requestId, requester details, approval signatures/decisions, metadata).\n"
        "2. Include user-input fields (like system, role, reason, justification, duration, ipAddress, etc.).\n"
        "3. If a property is an object containing nested parameters (like 'provisioning' containing 'system' and 'role'), "
        "identify the leaf user inputs (e.g., 'system' and 'role') and include them.\n\n"
        "Respond ONLY with a raw JSON object with the following structure. Do not include markdown code block formatting or explanations:\n"
        "{\n"
        "  \"user_facing_fields\": [\n"
        "    {\n"
        "      \"name\": \"parameter_name\",\n"
        "      \"type\": \"string\",\n"
        "      \"description\": \"Description of what to ask the user\"\n"
        "    }\n"
        "  ]\n"
        "}"
    )
    
    try:
        reply = await generate_response(prompt)
        clean_reply = reply.strip()
        if clean_reply.startswith("```"):
            lines = clean_reply.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_reply = "\n".join(lines).strip()
            
        result = json.loads(clean_reply)
        fields = result.get("user_facing_fields", [])
        
        user_properties = {}
        user_required = []
        for f in fields:
            name = f.get("name")
            if name:
                user_properties[name] = {
                    "type": f.get("type", "string"),
                    "description": f.get("description", "")
                }
                user_required.append(name)
                
        # Always guarantee target_type, target_name, role, and reason are present/required as base requirements
        if "target_type" not in user_properties:
            user_properties["target_type"] = {"type": "string", "description": "The kind of access target (resource, group, or role)"}
            user_required.append("target_type")
        if "target_name" not in user_properties:
            user_properties["target_name"] = {"type": "string", "description": "The target system, group, or role name"}
            user_required.append("target_name")
        if "role" not in user_properties:
            user_properties["role"] = {"type": "string", "description": "The requested role or access level (e.g. Admin, Reader, Standard Access)"}
            user_required.append("role")
        if "reason" not in user_properties:
            user_properties["reason"] = {"type": "string", "description": "The justification/reason for the access request"}
            user_required.append("reason")
            
        _user_facing_schema_cache = {
            "type": "object",
            "properties": user_properties,
            "required": list(set(user_required))
        }
        logger.info(f"Dynamically generated user-facing schema from description classification: {_user_facing_schema_cache}")
        return _user_facing_schema_cache
    except Exception as e:
        logger.warning(f"Failed to dynamically classify user-facing fields using LLM: {e}. Using standard schema.")
        # Safe default
        return {
            "type": "object",
            "properties": {
                "target_type": {"type": "string", "description": "The kind of access target (resource, group, or role)"},
                "target_name": {"type": "string", "description": "The target system, group, or role name"},
                "role": {"type": "string", "description": "The requested role or access level (e.g. Admin, Reader, Standard Access)"},
                "reason": {"type": "string", "description": "The justification/reason for the access request"}
            },
            "required": ["target_type", "target_name", "role", "reason"]
        }

async def route_card_to_manager(activity: dict, request_id: int, sender_name: str, requester_email: str, resource: str, role: str):
    requester_conversation_id = activity.get("conversation", {}).get("id")
    service_url = activity.get("serviceUrl")

    pm_teams_user_id = None
    pm_name = None
    requester_id = activity.get("from", {}).get("id")

    # 0. Authoritative source: the requester's line manager in midPoint (org:manager).
    if requester_email and "@" in requester_email:
        mp_manager = await fetch_user_manager(requester_email)
        if mp_manager:
            pm_name = mp_manager.get("fullName") or mp_manager.get("name") or pm_name
            pm_teams_user_id = _teams_id_for_email(mp_manager.get("email"))
            logger.info("midPoint manager for %s: %s <%s> teams_id=%s", requester_email, pm_name, mp_manager.get("email"), pm_teams_user_id)

    # 1. Try Live Production-Grade Microsoft Graph API / Teams Bot Connector Lookup
    if requester_id and service_url and not pm_teams_user_id:
        try:
            logger.info(f"Production Lookups: Fetching profile for Teams member: {requester_id}")
            req_profile = await get_teams_member_profile(service_url, requester_conversation_id, requester_id)
            
            if req_profile and req_profile.get("objectId"):
                req_aad_id = req_profile["objectId"]
                logger.info(f"Production Lookups: Found Entra objectId: {req_aad_id}. Fetching manager from Graph API...")
                manager_profile = await get_user_manager_profile(req_aad_id)
                
                if manager_profile and manager_profile.get("id"):
                    pm_teams_user_id = manager_profile["id"]
                    pm_name = manager_profile.get("displayName") or manager_profile.get("mail") or "Manager"
                    logger.info(f"Production Lookups: Resolved manager: {pm_name} (Entra ID: {pm_teams_user_id})")
        except Exception as e:
            logger.warning(f"Production Graph lookup failed or bypassed: {e}")
    
    # 2. Fallback to Local Seeded Directory DB — only when no manager has been identified yet
    #    (midPoint's org manager, if found above, is authoritative and must not be overridden).
    if not pm_name and (not pm_teams_user_id or pm_teams_user_id.startswith("29:approver-") or pm_teams_user_id.startswith("29:requester-")):
        pm_teams_user_id = None
        logger.info("No manager yet. Falling back to local bot_user_directory table.")
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT manager_username FROM bot_user_directory WHERE username = %s", (sender_name,))
            row_mgr = cursor.fetchone()
            if row_mgr and row_mgr[0]:
                pm_name = row_mgr[0]
                cursor.execute("SELECT teams_user_id FROM bot_user_directory WHERE username = %s", (pm_name,))
                row_dir = cursor.fetchone()
                if row_dir:
                    pm_teams_user_id = row_dir[0]
            cursor.close()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to lookup fallback database directory: {e}", exc_info=True)
    
    # Treat mock database IDs as unresolved so we fetch their real dynamic ID from the chat
    if pm_teams_user_id and (pm_teams_user_id.startswith("29:approver-") or pm_teams_user_id.startswith("29:requester-")):
        pm_teams_user_id = None

    # 3. Dynamic Fallback: Resolve manager's Teams ID from active conversation members list
    if not pm_teams_user_id and pm_name:
        logger.info(f"Manager name resolved to '{pm_name}' but Teams user ID is missing/mock. Searching conversation members...")
        try:
            members = await get_conversation_members(service_url, requester_conversation_id)
            if members:
                for member in members:
                    member_name = member.get("name", "")
                    # Normalize spellings (e.g. Gourab vs Gourav) by replacing 'v' with 'b'
                    n_pm = pm_name.lower().strip().replace("v", "b")
                    n_mem = member_name.lower().strip().replace("v", "b")
                    if n_pm == n_mem or n_pm in n_mem or n_mem in n_pm:
                        pm_teams_user_id = member.get("id")
                        pm_name = member_name  # Update to exact Teams display name
                        logger.info(f"Dynamically resolved manager {pm_name} teams_user_id: {pm_teams_user_id}")
                        break
        except Exception as e:
            logger.warning(f"Failed to resolve manager Teams ID from conversation members: {e}")
            
    approver = pm_name or "Manager"

    # Update the database record with the actual resolved approver
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE bot_access_requests SET approver = %s WHERE id = %s", (approver, request_id))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to update resolved approver in database: {e}")
    
    # Adaptive Card JSON schema
    request_reason = ""
    request_description = ""
    # The approval card contains user data; sanitize all values before handing
    # them to Adaptive Card Markdown rendering.
    try:
        conn = get_db_connection(); detail_cursor = conn.cursor()
        detail_cursor.execute("SELECT mcp_payload, duration_days, risk_level FROM bot_access_requests WHERE id = %s", (request_id,))
        detail_row = detail_cursor.fetchone(); detail_cursor.close(); conn.close()
        request_details = json.loads(detail_row[0] or "{}") if detail_row else {}
        request_reason = str(request_details.get("reason") or "Not provided")
        request_description = str(request_details.get("description") or "")
        duration = str(detail_row[1] if detail_row else "permanent")
        risk_level = str(detail_row[2] if detail_row else "medium")
    except Exception:
        duration = "permanent"
        risk_level = "medium"
    card_content = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "Access Request Approval Needed",
                "weight": "Bolder",
                "size": "Medium",
                "color": "Accent"
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Requester:", "value": safe_card_text(sender_name)},
                    {"title": "Email:", "value": safe_card_text(requester_email)},
                    {"title": "Resource:", "value": safe_card_text(resource)},
                    {"title": "Requested Role:", "value": safe_card_text(role)},
                    {"title": "Requested duration:", "value": safe_card_text(duration)},
                    {"title": "Risk level:", "value": safe_card_text(risk_level.upper())},
                    {"title": "Business justification:", "value": safe_card_text(request_reason)},
                    {"title": "Additional description:", "value": safe_card_text(request_description or "None")},
                    {"title": "Approver:", "value": safe_card_text(approver)},
                    {"title": "Request ID:", "value": str(request_id)}
                ]
            },
            {
                "type": "TextBlock",
                "text": f"Dear **{safe_card_text(approver)}**, please review this request for **{safe_card_text(resource)}** access (Role: **{safe_card_text(role)}**). High-risk or permanent access should be reviewed carefully.",
                "wrap": True
            }
        ],
        "actions": [
            {
                "type": "Input.Text",
                "id": "approver_comment",
                "label": "Comment (required for rejection or more information)",
                "isMultiline": True
            },
            {
                "type": "Input.Date",
                "id": "temporary_until",
                "label": "Temporary access until (optional; maximum 30 days from today)"
            },
            {
                "type": "Action.Submit",
                "title": "Approve temporary access",
                "style": "positive",
                "data": {
                    "action": "approve_temporary",
                    "request_id": request_id
                }
            },
            {
                "type": "Action.Submit",
                "title": "Approve",
                "style": "positive",
                "data": {
                    "action": "approve",
                    "request_id": request_id
                }
            },
            {
                "type": "Action.Submit",
                "title": "Reject",
                "style": "destructive",
                "data": {
                    "action": "reject",
                    "request_id": request_id
                }
            },
            {
                "type": "Action.Submit",
                "title": "Request more information",
                "data": {
                    "action": "more_info",
                    "request_id": request_id
                }
            }
        ]
    }
    
    # Proactive Routing Decision
    sent_proactively = False
    if pm_teams_user_id:
        bot_id = activity.get("recipient", {}).get("id") or settings.teams_app_id
        bot_name = activity.get("recipient", {}).get("name", settings.bot_name)
        tenant_id = (
            activity.get("channelData", {}).get("tenant", {}).get("id") or 
            activity.get("conversation", {}).get("tenantId") or 
            settings.teams_app_tenant_id
        )
        
        logger.info(f"Attempting to proactively message approver {pm_teams_user_id} on tenant {tenant_id}")
        
        pm_convo_id = await create_proactive_conversation(service_url, bot_id, bot_name, pm_teams_user_id, tenant_id)
        if pm_convo_id:
            logger.info(f"Proactive conversation created: {pm_convo_id}. Dispatching card to PM...")
            card_sent = await send_proactive_card(service_url, pm_convo_id, bot_id, bot_name, card_content)
            if card_sent:
                sent_proactively = True
                # End user must not see who the approver is — keep the confirmation generic.
                confirm_text = f"Your access request **#{request_id}** for **{resource}** (Role: **{role}**) has been submitted for approval."
                await send_reply_to_teams(activity, confirm_text)
                logger.info(f"Proactive card successfully sent to approver {approver}.")
            else:
                logger.warning(f"Failed to send proactive card to conversation ID {pm_convo_id}. Falling back to channel reply.")
        else:
            logger.warning(f"Failed to create proactive conversation with PM. Falling back to channel reply.")

    if not sent_proactively:
        # Do not reveal the manager's identity to the requester.
        err_msg = f"Your access request **#{request_id}** for **{resource}** (Role: **{role}**) has been submitted for approval."
        await send_reply_to_teams(activity, err_msg)
        logger.warning(f"Could not route card proactively to manager '{approver}'. Requester notified generically; approver routing needs directory setup.")

async def _handle_midpoint_decision(activity: dict, value: dict) -> dict:
    """Approve/reject a midPoint-raised request from its interactive card.

    Completes the work item through the JAR, locks the card in place, then DMs the requester the
    outcome directly (the midPoint COMPLETED notifier is a known non-firing blocker, so the bot
    delivers the granted/declined result itself). midPoint remains the source of truth.

    The approval card carries no one-time token and is only locked AFTER a decision actually lands,
    so validation failures (missing reject comment, bad temporary date, transient midPoint error)
    leave the card fully live — the manager fixes the input and clicks the same button again.
    """
    action = value.get("action")
    approve = action in {"mp_approve", "mp_approve_temporary"}
    case_oid = str(value.get("caseOid") or "").strip()
    work_item_id = str(value.get("workItemId") or "").strip()
    requester_email = str(value.get("requesterEmail") or "").strip()
    requested_item = str(value.get("requestedItem") or "").strip()
    target_type = str(value.get("targetType") or "role").strip() or "role"
    requester_name = str(value.get("requesterName") or "").strip()
    comment = str(value.get("approver_comment") or "").strip()[:1000]

    # The work item is completed directly from the caseOid (the JAR resolves the case's open work
    # item, using workItemId verbatim when present). The target name/requesterEmail are only needed
    # by the JAR's legacy name-resolution fallback. So require only the caseOid here; a card that
    # merely lacked requestedItem (the "the requested access" placeholder) is still fully actionable.
    if not case_oid:
        await send_reply_to_teams(activity, "This approval card is missing its case identifier and can no longer be actioned.")
        return {"status": "error", "message": "Incomplete midPoint card payload (no caseOid)."}

    # Server-side self-approval guard: the clicker must not be the requester.
    clicker_name = str(activity.get("from", {}).get("name") or "").strip()
    clicker_email = _resolve_requester_email(activity)
    if (clicker_email and clicker_email.lower() == requester_email.lower()) or (
        requester_name and clicker_name and clicker_name.lower() == requester_name.lower()
    ):
        await send_reply_to_teams(activity, "Error: You are not authorized to approve or reject your own access request.")
        return {"status": "error", "message": "Self-approval is not permitted."}

    if not approve and not comment and settings.require_reject_comment:
        # Not a dead end — the card is still live (no one-time token, not locked). The manager just
        # types a reason and clicks Reject again. A comment is required so the denial is auditable
        # and the requester learns why (toggle off with REQUIRE_REJECT_COMMENT=false).
        await send_reply_to_teams(activity, "A comment is required to reject. Type your reason in the **Comment** box on the approval card above, then click **Reject** again.")
        return {"status": "needs_input", "message": "Rejection comment required."}

    access_until = None
    if action == "mp_approve_temporary":
        try:
            parsed = date.fromisoformat(str(value.get("temporary_until") or ""))
            today = date.today()
            if not today < parsed <= today + timedelta(days=30):
                raise ValueError
            access_until = parsed.isoformat()
        except ValueError:
            await send_reply_to_teams(activity, "Pick a **Temporary access until** date between tomorrow and 30 days from today on the card above, then click **Approve temporary access** again.")
            return {"status": "needs_input", "message": "Invalid temporary-access date."}

    transaction_id = f"MP-{case_oid[:8]}-{work_item_id or '0'}"[:80]
    try:
        from app.services.provisioning_service import midpoint_decision
        receipt = await midpoint_decision(
            email=requester_email, target_name=requested_item, target_type=target_type,
            approve=approve, transaction_id=transaction_id, comment=comment, access_until=access_until,
            case_oid=case_oid, work_item_id=work_item_id,
        )
    except Exception:
        # Transport / 5xx / timeout — genuinely transient. The card stays actionable so the manager
        # can retry (the JAR completion is idempotent: a later click lands on an "already_closed"
        # terminal status, never a double-grant).
        logger.exception("midPoint decision (%s) failed for case %s", action, case_oid)
        await send_reply_to_teams(activity, "Sorry — MidPoint couldn’t record that decision just now. The approval card above is still active — please click the button again in a moment.")
        return {"status": "error", "message": "MidPoint decision call failed."}

    # Terminal, non-transient outcomes: the request was already decided (another approver / an
    # earlier click) or expired/escalated (delayed approval, SLA/deadline policy). Lock the card
    # honestly instead of claiming this click approved/rejected it. Idempotent + concurrency-safe.
    receipt_status = str((receipt or {}).get("status") or "accepted")
    if receipt_status in {"already_closed", "not_found"}:
        msg = ("This request has already been decided or has expired — no action was taken."
               if receipt_status == "already_closed"
               else "This approval request could not be found — it may have been withdrawn.")
        locked = midpoint_decision_locked_card(value, "expired", clicker_name or "Manager",
                                               "", "")
        updated = await update_card_in_teams(
            activity.get("serviceUrl"), activity.get("conversation", {}).get("id"), activity.get("replyToId"),
            locked, bot=activity.get("recipient"), recipient=activity.get("from"),
        )
        if not updated:
            await send_card_to_teams(activity, locked)
        await send_reply_to_teams(activity, msg)
        return {"status": receipt_status, "caseOid": case_oid}

    decision = "approved" if approve else "rejected"
    # Backfill the target name from the JAR receipt (resolved from the case) when the card's button
    # data lacked it, so the locked card, confirmation, and requester outcome all name what was
    # decided instead of showing "the requested access".
    if not requested_item:
        requested_item = str((receipt or {}).get("details", {}).get("targetName") or "").strip()
        if requested_item:
            value = {**value, "requestedItem": requested_item}
    record_audit_event(0, clicker_name or "manager", f"midpoint_request_{decision}", {"caseOid": case_oid, "comment": comment, "access_until": access_until, "target": requested_item})

    # Lock the card in place so it cannot be clicked again ("changeable" card).
    locked = midpoint_decision_locked_card(value, decision, clicker_name or "Manager", comment, access_until or "")
    updated = await update_card_in_teams(
        activity.get("serviceUrl"), activity.get("conversation", {}).get("id"), activity.get("replyToId"),
        locked, bot=activity.get("recipient"), recipient=activity.get("from"),
    )
    if not updated:
        await send_card_to_teams(activity, locked)
    # Descriptive manager confirmation: who requested what, the type, the outcome, and (on reject) why.
    who = safe_card_text(requester_name or requester_email or "the requester")
    item_md = safe_card_text(requested_item or "the requested access")
    type_word = {"role": "role", "service": "service"}.get(target_type, "access")
    verb = "approved" if approve else "declined"
    confirm = f"You have **{verb.upper()}** {who}’s request for the {type_word} **{item_md}**."
    if approve and access_until:
        confirm += f" Temporary access until **{safe_card_text(access_until)}**."
    elif not approve and comment:
        confirm += f" Reason: {safe_card_text(comment)}"
    await send_reply_to_teams(activity, confirm)

    # Notify the requester of the outcome directly. The midPoint COMPLETED notifier that would push
    # this is a known non-firing blocker, so the bot DMs the requester itself (best-effort — the
    # manager's decision is already committed in midPoint regardless of delivery). Skipped when
    # bot_direct_notify is off (the notifier is authoritative → avoids double-notifying).
    if settings.bot_direct_notify and requester_email and "@" in requester_email:
        try:
            from app.services.notify_service import handle_notification
            item_label = requested_item or "your requested access"
            type_label = {"role": "role", "service": "service"}.get(target_type, "access")
            # Escape dynamic parts (item, comment) so the composed message can safely carry **bold**.
            item_md = safe_card_text(item_label)
            message = (f"Your request for the {type_label} **{item_md}** was "
                       f"**{'granted' if approve else 'declined'}**")
            if approve and access_until:
                message += f" (temporary access until {safe_card_text(access_until)})."
            elif not approve and comment:
                message += f". Reason: {safe_card_text(comment)}"
            else:
                message += "."
            result_payload = {
                "event": "ACCESS_REQUEST_COMPLETED",
                "status": "GRANTED" if approve else "REJECTED",
                "caseOid": case_oid, "workItemId": work_item_id,
                "requesterEmail": requester_email, "requesterName": requester_name,
                "requestedItem": item_label, "targetType": target_type,
                "accessUntil": access_until or "", "comment": comment,
                "recipientEmail": requester_email,
                "message": message,
            }
            outcome = await handle_notification(result_payload)
            logger.info("Requester outcome DM for case %s → %s: %s", case_oid, requester_email, outcome.get("status"))
        except Exception:
            logger.exception("Failed to DM requester outcome for case %s", case_oid)

    return {"status": "success", "decision": decision, "caseOid": case_oid}


async def process_activity(activity: dict) -> dict:
    activity_type = activity.get("type")
    
    if activity_type == "message":
        # 1. Handle Adaptive Card Action.Submit button clicks
        value = activity.get("value")
        conversation_id_for_session = str(activity.get("conversation", {}).get("id") or "")

        # --- Conversation-security gate ---------------------------------------------------
        # Single entry point enforcing retry-dedup, one-time buttons, session state, and
        # hijack protection. See app/services/session_manager.py.
        _raw_text = activity.get("text", "") or ""
        _clean_for_gate = re.sub(r'@\S+', '', re.sub(r'<at>.*?</at>', '', _raw_text, flags=re.IGNORECASE)).strip()
        _action = value.get("action") if isinstance(value, dict) else None
        _is_button = bool(_action)
        _is_greeting = (not _is_button) and session_manager.is_greeting(_clean_for_gate)
        # Governed by the requester session: greetings, session buttons, and free text
        # (but never approver/manager actions, which run in a different conversation).
        _governs = (_action not in _APPROVAL_ACTIONS) and (_is_greeting or (_action in _SESSION_ACTIONS) or not _is_button)
        _card_token = value.get("card_token") if _is_button else None
        _decision = session_manager.begin_activity(
            activity, is_greeting=_is_greeting, is_button=_is_button, action=_action,
            card_token=_card_token, governs_session=_governs,
        )
        if not _decision.allow:
            if _decision.reply:
                await send_reply_to_teams(activity, _decision.reply)
            return {"status": "blocked", "reason": _decision.reason}
        if _is_greeting:
            session_manager.start_session(activity)
        if value and isinstance(value, dict) and value.get("action") == "open_access_form":
            # Re-verify eligibility before letting the user start a request.
            email = await _requester_email(activity)
            ok, _profile, reason = await _check_eligibility(email)
            if not ok:
                await send_reply_to_teams(activity, _ineligible_message(reason, email))
                return {"status": "blocked", "reason": reason}
            # Step 1 of the 2-step wizard: choose Roles/Services. Step 2 shows a dropdown of live
            # midPoint requestable items — no free text, the user only ever picks from the catalog.
            await send_card_to_teams(activity, requestable_type_card())
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "req_pick_type":
            # Step 2: fetch live requestable items of the chosen type and show the target dropdown.
            access_type = str(value.get("access_type") or "role").strip().lower()
            type_label = {"role": "role", "service": "service", "group": "group"}.get(access_type, access_type)
            await send_typing_activity(activity)
            items = await fetch_requestable_access(access_type)
            await send_card_to_teams(activity, requestable_target_card(access_type, type_label, items))
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "submit_requestable":
            choice = str(value.get("access_choice") or "")
            itype, _, rest = choice.partition("|")
            oid, _, name = rest.partition("|")
            if not itype or not oid or not name:
                await send_reply_to_teams(activity, "Please choose an access item before continuing.")
                return {"status": "error", "message": "No requestable item selected."}
            # Parse duration (Permanent / 30 Days / Custom end date) — shared with the single-card path.
            _token, access_until, derr = _parse_access_request_duration(value)
            if derr:
                await send_reply_to_teams(activity, derr)
                return {"status": "error", "message": "Invalid duration."}
            human_duration = "Permanent" if access_until is None else f"Temporary — until {access_until}"
            reviewed = {
                "target_type": itype, "target_name": name, "target_oid": oid, "role": name,
                "description": _optional_description(value.get("additionalDescription")),
                "duration": human_duration,
            }
            if access_until:
                reviewed["access_until_override"] = access_until
            await send_card_to_teams(activity, requestable_confirm_card(reviewed))
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "confirm_requestable":
            # Final decisive action: take the conversation lock so no parallel input runs,
            # then complete the session only after the response is delivered.
            correlation_id = _decision.correlation_id
            if not session_manager.acquire_lock(conversation_id_for_session, correlation_id):
                await send_reply_to_teams(activity, session_manager.MSG_PROCESSING_WAIT)
                return {"status": "blocked", "reason": "locked"}
            started = time.monotonic()
            # Lock the clicked card in place (buttons gone) so it can't be re-submitted.
            await update_card_in_teams(activity.get("serviceUrl"), conversation_id_for_session,
                                       activity.get("replyToId"), locked_card("Review your request", "Submit for approval"),
                                       bot=activity.get("recipient"), recipient=activity.get("from"))
            await send_reply_to_teams(activity, "Processing your request...")
            try:
                result = await _submit_requestable(activity, dict(value))
            except Exception:
                logger.exception("confirm_requestable failed", extra={"correlation_id": correlation_id, "conversation_id": conversation_id_for_session})
                metrics_service.increment(metrics_service.EXCEPTIONS)
                session_manager.unlock_keep_alive(conversation_id_for_session)
                await send_reply_to_teams(activity, "Sorry — something went wrong submitting your request. Please try again.")
                return {"status": "error", "message": "submit failed"}
            finally:
                metrics_service.observe_processing_ms((time.monotonic() - started) * 1000)
            if result.get("status") == "success":
                session_manager.to_state(conversation_id_for_session, session_manager.RESPONDING, {session_manager.PROCESSING})
                await send_reply_to_teams(activity, "Your request has been submitted. This session is now closed — say “hi” whenever you’d like to raise another request.")
                session_manager.complete(conversation_id_for_session)
            else:
                # Validation/blocked outcome — keep the session usable for a retry.
                session_manager.unlock_keep_alive(conversation_id_for_session)
            return result
        if value and isinstance(value, dict) and value.get("action") == "cancel_requestable":
            # Cancel from the review card — end the session cleanly (inputs discarded).
            await send_reply_to_teams(activity, "No problem — I’ve cancelled that request. Say “hi” whenever you’d like to start again.")
            session_manager.complete(conversation_id_for_session)
            return {"status": "success", "action": "cancelled"}
        if value and isinstance(value, dict) and value.get("action") == "wizard_pick_type":
            target_type = value.get("target_type", "resource")
            email = _resolve_requester_email(activity)
            if not email:
                await send_reply_to_teams(activity, "I could not find your directory email, so I can't load your available access. Please contact your administrator.")
                return {"status": "error", "message": "No requester email."}
            await send_typing_activity(activity)
            if target_type == "group":
                options, type_label = await fetch_user_groups(email), "group"
            elif target_type == "role":
                catalog = await get_access_catalog()
                options = [{"name": r, "oid": r} for item in catalog for r in item.get("roles", [])]
                type_label = "role"
            else:
                options, type_label = await fetch_user_applications(email), "application"
            await send_card_to_teams(activity, wizard_target_card(target_type, options, type_label))
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "wizard_pick_target":
            target_type = value.get("target_type", "resource")
            choice = str(value.get("target_choice") or "")
            target_oid, _, target_name = choice.partition("|")
            if not target_name:
                await send_reply_to_teams(activity, "Please choose an option before continuing.")
                return {"status": "error", "message": "No target selected."}
            if target_type == "resource":
                await send_typing_activity(activity)
                roles = await fetch_application_roles(target_oid)
                await send_card_to_teams(activity, wizard_access_card(target_type, target_name, roles))
                return {"status": "success"}
            # Group or direct role: the chosen target is itself the grant.
            reviewed = {"target_type": "group" if target_type == "group" else "role", "target_name": target_name, "role": target_name, "reason": "", "duration": "permanent", "description": ""}
            await send_card_to_teams(activity, confirmation_card(reviewed))
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "submit_application_request":
            # Application access is granted via its application role, so the role is the
            # provisioned target. The application name is kept as approver context.
            app_name = str(value.get("app_name") or "")
            try:
                role = _validated_form_value(value.get("role"), "role")
                reason = _validated_form_value(value.get("reason"), "reason")
                duration = _validated_form_value(value.get("duration", "permanent"), "duration")
                description = _optional_description(value.get("description"))
                if duration not in {"30", "90", "permanent"}:
                    raise ValueError("Please choose a valid access duration.")
            except ValueError as exc:
                await send_reply_to_teams(activity, f"I could not prepare that request: {exc}")
                return {"status": "error", "message": "Invalid form data."}
            context = f"Application: {app_name}" + (f" — {description}" if description else "")
            reviewed = {"target_type": "role", "target_name": role, "role": role, "reason": reason,
                        "duration": "Permanent" if duration == "permanent" else f"{duration} days", "description": context}
            await send_card_to_teams(activity, confirmation_card(reviewed))
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "decline_access_form":
            await send_reply_to_teams(activity, "Have a good day! “Small steps every day add up to big change.”")
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "submit_access_form":
            _field_defaults = {"duration": "permanent", "target_type": "resource"}
            try:
                reviewed = {field: _validated_form_value(value.get(field, _field_defaults.get(field)), field) for field in _FORM_FIELD_LIMITS}
                reviewed["description"] = _optional_description(value.get("description"))
                if reviewed["duration"] not in {"30", "90", "permanent"}:
                    raise ValueError("Please choose a valid access duration.")
            except ValueError as exc:
                await send_reply_to_teams(activity, f"I could not prepare that request: {exc}")
                return {"status": "error", "message": "Invalid form data."}
            reviewed["duration"] = "Permanent" if reviewed["duration"] == "permanent" else f"{reviewed['duration']} days"
            await send_card_to_teams(activity, confirmation_card(reviewed))
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "confirm_access_form":
            correlation_id = _decision.correlation_id
            if not session_manager.acquire_lock(conversation_id_for_session, correlation_id):
                await send_reply_to_teams(activity, session_manager.MSG_PROCESSING_WAIT)
                return {"status": "blocked", "reason": "locked"}
            started = time.monotonic()
            await update_card_in_teams(activity.get("serviceUrl"), conversation_id_for_session,
                                       activity.get("replyToId"), locked_card("Review your request", "Submit for approval"),
                                       bot=activity.get("recipient"), recipient=activity.get("from"))
            await send_reply_to_teams(activity, "Processing your request...")
            normalized = dict(value)
            normalized["duration"] = {"Permanent": "permanent", "30 days": "30", "90 days": "90"}.get(str(value.get("duration")), value.get("duration"))
            try:
                result = await _submit_access_form(activity, normalized)
            except Exception:
                logger.exception("confirm_access_form failed")
                metrics_service.increment(metrics_service.EXCEPTIONS)
                session_manager.unlock_keep_alive(conversation_id_for_session)
                await send_reply_to_teams(activity, "Sorry — something went wrong submitting your request. Please try again.")
                return {"status": "error", "message": "submit failed"}
            finally:
                metrics_service.observe_processing_ms((time.monotonic() - started) * 1000)
            if result.get("status") == "success":
                session_manager.to_state(conversation_id_for_session, session_manager.RESPONDING, {session_manager.PROCESSING})
                await send_reply_to_teams(activity, "Your request has been submitted. This session is now closed — say “hi” whenever you’d like to raise another request.")
                session_manager.complete(conversation_id_for_session)
            else:
                session_manager.unlock_keep_alive(conversation_id_for_session)
            return result
        if value and isinstance(value, dict) and value.get("action") == "my_requests":
            # midPoint is the source of truth: read the user's approval cases live, store nothing locally.
            requester_email = await _requester_email(activity)
            from app.services.provisioning_service import fetch_user_requests
            items = await fetch_user_requests(requester_email) if requester_email else []
            await send_card_to_teams(activity, midpoint_requests_card(items))
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "cancel_request":
            request_id = value.get("request_id")
            if not isinstance(request_id, int):
                return {"status": "error", "message": "Invalid request identifier."}
            requester_id = str(activity.get("from", {}).get("id") or "")
            conn = get_db_connection(); cursor = conn.cursor()
            cursor.execute("UPDATE bot_access_requests SET status = 'cancelled' WHERE id = %s AND requester_teams_id = %s AND status IN ('pending', 'gathering_params', 'more_info_required')", (request_id, requester_id))
            changed = cursor.rowcount; conn.commit(); cursor.close(); conn.close()
            await send_reply_to_teams(activity, "Your request has been cancelled." if changed else "This request cannot be cancelled.")
            return {"status": "success" if changed else "error"}
        if value and isinstance(value, dict) and value.get("action") == "manager_queue":
            manager = str(activity.get("from", {}).get("name") or "")[:255]
            conn = get_db_connection(); cursor = conn.cursor()
            cursor.execute("SELECT id, requester, resource, requested_role, risk_level, status, access_until FROM bot_access_requests WHERE approver = %s AND status IN ('pending', 'more_info_required', 'approved') ORDER BY timestamp DESC LIMIT 25", (manager,))
            rows = cursor.fetchall(); cursor.close(); conn.close()
            await send_card_to_teams(activity, manager_queue_card(rows))
            return {"status": "success"}
        if value and isinstance(value, dict) and value.get("action") == "revoke_temporary":
            request_id = value.get("request_id")
            if not isinstance(request_id, int):
                return {"status": "error", "message": "Invalid request identifier."}
            manager = str(activity.get("from", {}).get("name") or "")[:255]
            conn = get_db_connection(); cursor = conn.cursor()
            cursor.execute("UPDATE bot_access_requests SET status = 'revoked' WHERE id = %s AND approver = %s AND status = 'approved' AND access_until IS NOT NULL", (request_id, manager))
            changed = cursor.rowcount; conn.commit(); cursor.close(); conn.close()
            if changed:
                record_audit_event(request_id, manager, "temporary_access_revoked")
            await send_reply_to_teams(activity, "Temporary access has been revoked." if changed else "This temporary access cannot be revoked by you.")
            return {"status": "success" if changed else "error"}
        if value and isinstance(value, dict) and value.get("action") in {"mp_approve", "mp_approve_temporary", "mp_reject"}:
            return await _handle_midpoint_decision(activity, value)
        if value and isinstance(value, dict) and value.get("action") in {"approve", "approve_temporary", "reject", "more_info"} and "request_id" in value:
            action = value.get("action")
            request_id = value.get("request_id")

            if not isinstance(request_id, int) and not (isinstance(request_id, str) and request_id.isdigit()):
                await send_reply_to_teams(activity, "Error: invalid request identifier.")
                return {"status": "error", "message": "Invalid request identifier."}
            request_id = int(request_id)
            
            conn = get_db_connection()
            cursor = conn.cursor()
            
            new_status = {"approve": "approved", "approve_temporary": "approved", "reject": "rejected", "more_info": "more_info_required"}[action]
            
            cursor.execute(
                "SELECT requester, resource, approver, requested_role, requester_email, requester_conversation_id, service_url, status, mcp_payload, risk_level, duration_days, requested_target_type FROM bot_access_requests WHERE id = %s",
                (request_id,)
            )
            row = cursor.fetchone()
            if not row:
                cursor.close()
                conn.close()
                logger.error(f"Access request #{request_id} not found in DB.")
                await send_reply_to_teams(activity, f"Error: Request #{request_id} not found.")
                return {"status": "error", "message": "Request not found."}
                
            requester, resource, approver, requested_role, requester_email, requester_conversation_id, service_url, current_status, prev_mcp_payload_str, request_risk, request_duration, request_target_type = row
            
            # Validation 1: Prevent re-approval/re-rejection of already processed cards
            if current_status in ("approved", "rejected", "cancelled"):
                cursor.close()
                conn.close()
                logger.warning(f"Attempted to process request #{request_id} which is already {current_status}.")
                await send_reply_to_teams(activity, f"Error: Request #{request_id} has already been processed and is **{current_status.upper()}**.")
                return {"status": "error", "message": f"Request already {current_status}."}
            
            # Validation 2: Prevent requester from approving/rejecting their own request
            clicker_name = activity.get("from", {}).get("name", "Employee")
            if clicker_name and requester and clicker_name.lower().strip() == requester.lower().strip():
                cursor.close()
                conn.close()
                logger.warning(f"Requester '{clicker_name}' attempted to approve/reject their own request #{request_id}.")
                await send_reply_to_teams(activity, "Error: You are not authorized to approve or reject your own access request.")
                return {"status": "error", "message": "Self-approval is not permitted."}

            # The approval card is sent only to the resolved manager. Verify the
            # callback actor as a second server-side guard (case-insensitive only
            # because Teams display-name casing is not stable).
            if approver and clicker_name.lower().strip() != approver.lower().strip():
                cursor.close()
                conn.close()
                logger.warning("Unauthorized approval attempt for request %s", request_id)
                await send_reply_to_teams(activity, "Error: you are not the assigned approver for this request.")
                return {"status": "error", "message": "Unauthorized approver."}

            approver_comment = str(value.get("approver_comment") or "").strip()[:1000]
            if action in {"reject", "more_info"} and not approver_comment:
                cursor.close(); conn.close()
                await send_reply_to_teams(activity, "Please add a comment before rejecting or requesting more information.")
                return {"status": "error", "message": "Approver comment required."}
            if action == "approve" and (request_risk == "high" or request_duration == "permanent") and not approver_comment:
                cursor.close(); conn.close()
                await send_reply_to_teams(activity, "Please add an approval comment to confirm this high-risk or permanent access decision.")
                return {"status": "error", "message": "Approval confirmation comment required."}

            access_until = None
            if action == "approve_temporary":
                try:
                    access_until = date.fromisoformat(str(value.get("temporary_until") or ""))
                    today = date.today()
                    if not today < access_until <= today + timedelta(days=30):
                        raise ValueError
                except ValueError:
                    cursor.close(); conn.close()
                    await send_reply_to_teams(activity, "Temporary approval requires a valid date from tomorrow through the next 30 days.")
                    return {"status": "error", "message": "Invalid temporary-access date."}

            if new_status == "more_info_required":
                cursor.execute("UPDATE bot_access_requests SET status = %s, approver_comment = %s WHERE id = %s", (new_status, approver_comment, request_id))
                conn.commit(); cursor.close(); conn.close()
                record_audit_event(request_id, clicker_name, "more_information_requested", {"comment": approver_comment})
                await send_reply_to_teams(activity, "Your request for more information has been sent to the requester.")
                if requester_conversation_id and service_url:
                    await send_proactive_message(service_url, requester_conversation_id, activity.get("recipient", {}).get("id") or settings.teams_app_id, settings.bot_name, f"Your request #{request_id} needs more information before it can be approved: {approver_comment}")
                return {"status": "success"}

            
            # Update status and save MCP payload on approval
            mcp_payload_str = None
            mcp_success_message = None
            if new_status == "approved":
                extra_params = {}
                if prev_mcp_payload_str:
                    try:
                        extra_params = json.loads(prev_mcp_payload_str)
                    except Exception:
                        extra_params = {}
                        
                mcp_payload = {
                    "requestId": f"REQ-{request_id}",
                    "requester": {
                        "midpointUserOid": extra_params.get("requester", {}).get("midpointUserOid"),
                        "name": requester,
                        "email": requester_email
                    },
                    "provisioning": {
                        "targetType": request_target_type or "resource",
                        "targetName": resource,
                        "targetOid": extra_params.get("target_oid"),
                        "role": requested_role,
                        "entitlements": [f"{resource}Access", f"{requested_role}Access"]
                    },
                    "approval": {
                        "approver": approver,
                        "decision": "APPROVED",
                        "timestamp": str(activity.get("timestamp", "")),
                        "accessUntil": access_until.isoformat() if access_until else None,
                    },
                    "metadata": {
                        "source": "Teams IGA Bot",
                        "systemType": "Identity Governance & Administration (IGA)"
                    },
                    **extra_params
                }
                # Filter out target fields from the root of the arguments dict if they are not in the tool schema's properties
                # but we gathered them to populate requester, provisioning.system, and provisioning.role.
                tool_name = resolve_access_tool_name()
                original_schema = get_tool_schema(tool_name)
                orig_props = original_schema.get("properties", {})
                
                # Delete target_type, target_name, role, and reason from the root of mcp_payload if the original tool schema doesn't define them at the root
                if "target_type" not in orig_props:
                    mcp_payload.pop("target_type", None)
                if "target_name" not in orig_props:
                    mcp_payload.pop("target_name", None)
                if "role" not in orig_props:
                    mcp_payload.pop("role", None)
                if "reason" not in orig_props:
                    mcp_payload.pop("reason", None)
                
                mcp_payload_str = json.dumps(mcp_payload)
                logger.info(f"Constructed handover payload for MCP: {mcp_payload_str}")
                
                # Provisioning stays behind a single adapter boundary. The current
                # MCP route remains supported while a future MidPoint JAR can be
                # enabled through PROVISIONING_MODE=midpoint.
                if settings.provisioning_mode == "midpoint":
                    try:
                        from app.services.provisioning_service import provision_with_midpoint
                        _mp_receipt = await provision_with_midpoint(mcp_payload)
                        mcp_success_message = str((_mp_receipt or {}).get("message") or "Provisioning request accepted by MidPoint.") if isinstance(_mp_receipt, dict) else str(_mp_receipt)
                    except Exception:
                        logger.exception("MidPoint provisioning failed for request %s", request_id)
                        mcp_success_message = "The request was approved, but provisioning could not be started. The support team has been notified."
                elif settings.provisioning_mode == "mcp" and settings.mcp_url:
                    # pyrefly: ignore [missing-import]
                    import httpx
                    import uuid
                    try:
                        session_id = str(uuid.uuid4())
                        logger.info(f"Forwarding tool call to MCP server at: {settings.mcp_url} with session ID: {session_id}")
                        
                        mcp_rpc_payload = {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 1,
                            "params": {
                                "name": resolve_access_tool_name(),
                                "arguments": mcp_payload
                            }
                        }
                        
                        headers = {
                            "mcp-session-id": session_id,
                            "Accept": "application/json, text/event-stream",
                            "Content-Type": "application/json"
                        }
                        
                        async with httpx.AsyncClient() as client:
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
                            init_res = await client.post(settings.mcp_url, json=init_payload, headers=headers, timeout=5.0)
                            logger.info(f"MCP initialization response status: {init_res.status_code}")
                            
                            mcp_res = await client.post(settings.mcp_url, json=mcp_rpc_payload, headers=headers, timeout=10.0)
                            if mcp_res.status_code == 200:
                                try:
                                    mcp_data = mcp_res.json()
                                    if "result" in mcp_data and "content" in mcp_data["result"]:
                                        contents = mcp_data["result"]["content"]
                                        mcp_success_message = "\n".join([c.get("text", "") for c in contents if c.get("type") == "text"])
                                except Exception as ex:
                                    logger.warning(f"Failed to parse MCP success response body: {ex}")
                            
                        logger.info(f"MCP server response status: {mcp_res.status_code}, response body: {mcp_res.text}")
                    except Exception as e:
                        logger.error(f"Failed to transmit payload to MCP: {e}", exc_info=True)
            
            cursor.execute(
                "UPDATE bot_access_requests SET status = %s, mcp_payload = %s, approver_comment = %s, access_until = %s WHERE id = %s",
                (new_status, mcp_payload_str, approver_comment or None, access_until, request_id)
            )
            conn.commit()
            cursor.close()
            conn.close()
            record_audit_event(request_id, clicker_name, f"request_{new_status}", {"comment": approver_comment, "access_until": access_until.isoformat() if access_until else None})
            
            reply_text = f"**Request #{request_id}** for **{requester}** to access **{resource}** (Role: **{requested_role}**) has been **{new_status.upper()}** by **{approver}**."
            if access_until:
                reply_text += f" Temporary access is approved until **{access_until.isoformat()}**."
            if new_status == "approved" and mcp_success_message:
                reply_text += f"\n\n**Provisioning Status:** {mcp_success_message}"
            logger.info(f"Access request #{request_id} updated in DB to status: {new_status}")
            
            # Proactive Notification to Requester if metadata exists
            notified_proactively = False
            if requester_conversation_id and service_url:
                bot_id = activity.get("recipient", {}).get("id") or settings.teams_app_id
                bot_name = activity.get("recipient", {}).get("name", settings.bot_name)
                
                await send_reply_to_teams(activity, f"Thank you! Access request #{request_id} decision successfully processed.")
                
                # Requester-facing: do not reveal the approver's identity.
                notify_text = f"Hello **{requester}**, your access request **#{request_id}** to **{resource}** (Role: **{requested_role}**) has been **{new_status.upper()}**."
                if access_until:
                    notify_text += f" Your temporary access expires on **{access_until.isoformat()}**."
                if new_status == "approved" and mcp_success_message:
                    notify_text += f"\n\n**Provisioning Status:** {mcp_success_message}"
                    
                notified_proactively = await send_proactive_message(service_url, requester_conversation_id, bot_id, bot_name, notify_text)
                if notified_proactively:
                    logger.info(f"Proactively notified requester {requester} in conversation {requester_conversation_id}.")
                    
            if not notified_proactively:
                await send_reply_to_teams(activity, reply_text)
                
            return {"status": "success"}

        sender_name = activity.get("from", {}).get("name", "Employee")
        text = activity.get("text", "")
        
        # Process all incoming messages directly without requiring a bot mention prefix/tag

        # Remove bot mentions to get the clean text
        clean_text = text
        clean_text = re.sub(r'<at>.*?</at>', '', clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r'@\S+', '', clean_text)
        clean_text = clean_text.strip()
        
        logger.info(f"Cleaned message text for intent analysis: '{clean_text}'")

        # Deterministic greetings must not depend on the LLM (faster, private,
        # predictable, and resistant to prompt injection).
        if _GREETING_PATTERN.fullmatch(clean_text) or session_manager.is_greeting(clean_text):
            # Session already (re)started by the security gate; just gate on eligibility + greet.
            # Gate every session on midPoint identity: the user must exist AND hold the
            # mandatory 'End user' role. Ineligible users are not allowed to request access.
            await send_typing_activity(activity)
            email = await _requester_email(activity)
            ok, profile, reason = await _check_eligibility(email)
            if not ok:
                logger.info("Greeting blocked for %s: %s", email, reason)
                await send_reply_to_teams(activity, _ineligible_message(reason, email))
                return {"status": "blocked", "reason": reason}
            display_name = str(profile.get("fullName") or _first_name(activity)).split()[0] or _first_name(activity)
            # Remember this user's Teams id under BOTH their Teams email and midPoint email
            # so they can be reached as someone else's manager later, regardless of which the org uses.
            _remember_user([email, profile.get("email")], str(activity.get("from", {}).get("id") or ""))
            logger.info("Eligible session for %s: oid=%s roles=%s", email, profile.get("oid"), profile.get("roles"))
            # Best-effort midPoint-sourced approvals count for the greeting banner (0 → banner hidden).
            try:
                from app.services.provisioning_service import fetch_pending_approvals_count
                pending = await fetch_pending_approvals_count(email)
            except Exception:
                pending = 0
            # last_sync_minutes intentionally omitted: the bot has no real "last synced N min ago"
            # figure (the JAR owns the catalog schedule), and catalog_sync_minutes is the interval,
            # not elapsed time — showing it would fabricate freshness. Wire a real timestamp later.
            await send_card_to_teams(activity, greeting_card(display_name, pending_approvals_count=pending))
            return {"status": "success"}
        if clean_text.lower() in {"my approvals", "my approval queue", "pending approvals"}:
            manager = str(activity.get("from", {}).get("name") or "")[:255]
            conn = get_db_connection(); cursor = conn.cursor()
            cursor.execute("SELECT id, requester, resource, requested_role, risk_level, status, access_until FROM bot_access_requests WHERE approver = %s AND status IN ('pending', 'more_info_required', 'approved') ORDER BY timestamp DESC LIMIT 25", (manager,))
            rows = cursor.fetchall(); cursor.close(); conn.close()
            await send_card_to_teams(activity, manager_queue_card(rows))
            return {"status": "success"}
        
        # 1. Log message to employee_messages table
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO employee_messages (sender, message, status) VALUES (%s, %s, 'unread')",
                (sender_name, clean_text)
            )
            conn.commit()
            cursor.close()
            conn.close()
            logger.info(f"Automatically inserted new Teams message from {sender_name}: '{clean_text}'")
        except Exception as e:
            logger.error(f"Failed to log message to DB: {e}")

        # 2. Check if the message is an access request or a status check using dynamic LLM
        requester_conversation_id = activity.get("conversation", {}).get("id")
        
        active_req = None
        if requester_conversation_id:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, resource, requested_role, requester_email, mcp_payload FROM bot_access_requests WHERE requester_conversation_id = %s AND status = 'gathering_params' ORDER BY id DESC LIMIT 1",
                    (requester_conversation_id,)
                )
                active_req = cursor.fetchone()
                cursor.close()
                conn.close()
            except Exception as e:
                logger.warning(f"Error checking active gathering request: {e}")

        if active_req:
            request_id, prev_resource, prev_role, prev_email, mcp_payload_str = active_req
            logger.info(f"Resuming active parameter gathering request #{request_id}")
            gathered_args = {}
            if mcp_payload_str:
                try:
                    gathered_args = json.loads(mcp_payload_str)
                except Exception:
                    gathered_args = {}
            
            schema = await get_user_facing_schema()
            required_fields = schema.get("required", [])
            properties = schema.get("properties", {})
            
            missing_fields = [f for f in required_fields if not gathered_args.get(f)]

            if missing_fields and settings.enable_ai:
                missing_defs = []
                for mf in missing_fields:
                    desc = properties.get(mf, {}).get("description", "")
                    missing_defs.append(f"- '{mf}': {desc}")

                prompt = (
                    "You are an AI assistant helping to extract missing parameters for an access request from a user's reply.\n"
                    f"User's Reply: \"{clean_text}\"\n\n"
                    "Extract values for the following missing fields. If a field is not mentioned, set its value to null.\n"
                    "Missing Fields:\n" + "\n".join(missing_defs) + "\n\n"
                    "Respond ONLY with a raw JSON object matching the keys above. Do not include markdown code block formatting or explanations."
                )

                try:
                    reply = await generate_response(prompt)
                    clean_reply = reply.strip()
                    if clean_reply.startswith("```"):
                        lines = clean_reply.splitlines()
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines[-1].startswith("```"):
                            lines = lines[:-1]
                        clean_reply = "\n".join(lines).strip()

                    extracted = json.loads(clean_reply)
                    for mf in missing_fields:
                        val = extracted.get(mf)
                        if val:
                            gathered_args[mf] = val
                except Exception as e:
                    logger.warning(f"Failed to extract missing fields: {e}")
            
            missing_fields = [f for f in required_fields if not gathered_args.get(f)]
            
            if missing_fields:
                next_field = missing_fields[0]
                desc = properties.get(next_field, {}).get("description", "")
                
                try:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE bot_access_requests SET mcp_payload = %s WHERE id = %s",
                        (json.dumps(gathered_args), request_id)
                    )
                    conn.commit()
                    cursor.close()
                    conn.close()
                except Exception as e:
                    logger.error(f"Failed to update gathered args: {e}")
                    
                await send_reply_to_teams(activity, f"Please specify the **{next_field}** ({desc}) to complete your request.")
                return {"status": "success"}
            
            else:
                resource = gathered_args.get("target_name")
                role = gathered_args.get("role") or "Standard Access"
                
                try:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE bot_access_requests SET resource = %s, requested_role = %s, status = 'pending', mcp_payload = %s WHERE id = %s",
                        (resource, role, json.dumps(gathered_args), request_id)
                    )
                    conn.commit()
                    cursor.close()
                    conn.close()
                except Exception as e:
                    logger.error(f"Failed to transition request to pending: {e}")
                    
                await route_card_to_manager(activity, request_id, sender_name, prev_email, resource, role)
                return {"status": "success"}
        
        else:
            intent = "general_chat"
            parsed_data = {}

            # Get schema for both AI and non-AI modes
            schema = await get_user_facing_schema()
            properties = schema.get("properties", {})
            required_fields = schema.get("required", [])

            if settings.enable_ai:
                try:
                    prop_defs = []
                    for prop_name, prop_info in properties.items():
                        desc = prop_info.get("description", "")
                        prop_defs.append(f"- '{prop_name}': {desc}")

                    format_keys = []
                    for prop_name in properties.keys():
                        format_keys.append(f"  \"{prop_name}\": \"extracted-{prop_name}-or-null\"")
                    format_str = "{\n  \"intent\": \"create_request\",\n  \"requester_email\": \"extracted-email-or-null\",\n" + ",\n".join(format_keys) + "\n}"

                    parse_prompt = (
                        "You are an AI assistant parsing an access request from a chat message.\n"
                        f"Message: \"{clean_text}\"\n\n"
                        "Extract the intent of the message. The intent can be:\n"
                        "- 'create_request': if the user wants to request/get access to a resource/system.\n"
                        "- 'check_status': if the user wants to check the status of a request.\n"
                        "- 'general_chat': for greetings or other messages.\n\n"
                        "For 'create_request', extract the following properties. If a property is not mentioned, set its value to null:\n" +
                        "\n".join(prop_defs) + "\n"
                        "Also extract 'requester_email' if an email address is mentioned, or null.\n\n"
                        "Respond ONLY with a raw JSON object matching the format below. Do not include markdown code block formatting or explanations.\n"
                        f"Format:\n{format_str}"
                    )
                    parse_reply = await generate_response(parse_prompt)

                    clean_reply = parse_reply.strip()
                    if clean_reply.startswith("```"):
                        lines = clean_reply.splitlines()
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines[-1].startswith("```"):
                            lines = lines[:-1]
                        clean_reply = "\n".join(lines).strip()

                    parsed_data = json.loads(clean_reply)
                    intent = parsed_data.get("intent", "general_chat")
                except Exception as e:
                    logger.warning(f"Intent parsing failed: {e}. Raw response: {parse_reply if 'parse_reply' in locals() else ''}")
            else:
                # Non-AI intent detection: hardcoded rules (check status first to avoid "request" in "check my requests")
                text_lower = clean_text.lower()
                if any(keyword in text_lower for keyword in ["status", "check", "pending", "approved", "rejected"]):
                    intent = "check_status"
                    parsed_data = {"intent": intent}
                elif any(keyword in text_lower for keyword in ["access", "need", "want", "grant", "assign"]):
                    intent = "create_request"
                    # Extract target_name from text (e.g., "access to AWS" -> "AWS")
                    extracted_target = None
                    for sep in [" to ", " for ", " of "]:
                        if sep in text_lower:
                            parts = text_lower.split(sep)
                            if len(parts) > 1:
                                extracted_target = parts[-1].strip().split()[0].capitalize()
                                break
                    parsed_data = {"intent": intent, "target_name": extracted_target} if extracted_target else {"intent": intent}
                else:
                    intent = "general_chat"
                    parsed_data = {"intent": intent}
                
            if intent == "create_request":
                gathered_args = {}
                for prop_name in properties.keys():
                    if parsed_data.get(prop_name):
                        gathered_args[prop_name] = parsed_data[prop_name]

                if not gathered_args.get("target_type"):
                    gathered_args["target_type"] = parsed_data.get("target_type") or "resource"
                if not gathered_args.get("target_name"):
                    gathered_args["target_name"] = parsed_data.get("target_name") or parsed_data.get("system")
                
                missing_fields = [f for f in required_fields if not gathered_args.get(f)]
                
                requester_email = parsed_data.get("requester_email")
                if not requester_email:
                    clean_name = re.sub(r'[^a-zA-Z0-9]', '', sender_name).lower()
                    requester_email = f"{clean_name}@company.com"
                
                conn = get_db_connection()
                cursor = conn.cursor()
                
                if missing_fields:
                    next_field = missing_fields[0]
                    desc = properties.get(next_field, {}).get("description", "")
                    
                    cursor.execute(
                        "INSERT INTO bot_access_requests (requester, resource, approver, status, requested_role, requested_target_type, requested_target_name, requester_email, requester_conversation_id, service_url, mcp_payload) VALUES (%s, %s, 'Pending Resolution', 'gathering_params', %s, %s, %s, %s, %s, %s, %s)",
                        (sender_name, gathered_args.get("target_name", "Unknown"), gathered_args.get("role", "Standard Access"), gathered_args.get("target_type", "resource"), gathered_args.get("target_name", "Unknown"), requester_email, requester_conversation_id, activity.get("serviceUrl"), json.dumps(gathered_args))
                    )
                    conn.commit()
                    request_id = cursor.lastrowid
                    cursor.close()
                    conn.close()
                    
                    logger.info(f"Created access request #{request_id} in 'gathering_params' state. Missing: {missing_fields}")
                    
                    await send_reply_to_teams(activity, f"To submit your request, please specify the **{next_field}** ({desc}).")
                    return {"status": "success"}
                
                else:
                    resource = gathered_args.get("target_name")
                    target_type = gathered_args.get("target_type") or "resource"
                    role = gathered_args.get("role") or "Standard Access"
                    
                    cursor.execute(
                        "INSERT INTO bot_access_requests (requester, resource, approver, status, requested_role, requested_target_type, requested_target_name, requester_email, requester_conversation_id, service_url, mcp_payload) VALUES (%s, %s, 'Pending Resolution', 'pending', %s, %s, %s, %s, %s, %s, %s)",
                        (sender_name, resource, role, target_type, resource, requester_email, requester_conversation_id, activity.get("serviceUrl"), json.dumps(gathered_args))
                    )
                    conn.commit()
                    request_id = cursor.lastrowid
                    cursor.close()
                    conn.close()
                    
                    logger.info(f"Created pending access request #{request_id} immediately.")
                    await route_card_to_manager(activity, request_id, sender_name, requester_email, resource, role)
                    return {"status": "success"}
            
            # 4. Handle Access Request Status Check (Database Query)
            elif intent == "check_status":
                resource = parsed_data.get("resource")
                
                conn = get_db_connection()
                cursor = conn.cursor()
                if resource:
                    cursor.execute(
                        "SELECT id, resource, approver, status FROM bot_access_requests WHERE requester = %s AND resource LIKE %s ORDER BY timestamp DESC LIMIT 3",
                        (sender_name, f"%{resource}%")
                    )
                else:
                    cursor.execute(
                        "SELECT id, resource, approver, status FROM bot_access_requests WHERE requester = %s ORDER BY timestamp DESC LIMIT 3",
                        (sender_name,)
                    )
                rows = cursor.fetchall()
                cursor.close()
                conn.close()
                
                if rows:
                    replies = []
                    for r_id, r_res, r_app, r_status in rows:
                        replies.append(f"- **Request #{r_id}** for **{r_res}** access is **{r_status.upper()}**.")
                    reply_text = f"Hello {sender_name}, here is the status update for your access request(s):\n" + "\n".join(replies)
                else:
                    reply_text = f"Hello {sender_name}, I couldn't find any access request history for you in our database."
                    if resource:
                        reply_text += f" (Searching for resource: '{resource}')"
                
                await send_reply_to_teams(activity, reply_text)
                return {"status": "success"}
                
            # 5. Handle Standard Conversational Queries
            else:
                if settings.enable_ai:
                    try:
                        system_instruction = (
                            f"You are a helpful corporate access assistant bot named {settings.bot_name} in Microsoft Teams. "
                            "Provide clear, professional, and concise answers to the user's query."
                        )
                        llm_response = await generate_response(clean_text, system_instruction=system_instruction)
                    except Exception as e:
                        logger.error(f"LLM response generation failed: {e}", exc_info=True)
                        llm_response = "Sorry, I encountered an issue processing your request."
                else:
                    llm_response = (
                        f"Hello {sender_name}, I'm {settings.bot_name}. I can help you request access to resources. "
                        "Type 'help' or use the form to submit an access request."
                    )

                await send_reply_to_teams(activity, llm_response)
                return {"status": "success"}
                
    return {"status": "unsupported_activity_type", "type": activity_type}
