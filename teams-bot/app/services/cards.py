"""Adaptive Card factories for the AccessMate Teams experience.

A small design system keeps every card consistent and enterprise-ready:
a branded header, accent-coloured titles, subtle separators, progress cues,
and a single primary action per step. Adaptive Cards cannot run CSS/JS
animations; loading motion is provided natively by the Teams typing indicator
(see teams_service.send_typing_activity).
"""
import uuid
from typing import Any, Dict

_BRAND = "AccessMate"
_LOGO = "🔐"


def stamp_card_token(card_content: Dict[str, Any]) -> str:
    """Embed one fresh one-time token into every Action.Submit of a card (same token per render).

    Teams merges an action's `data` into the submit's `activity.value`, so the token round-trips
    back on click and `session_manager.consume_token` makes the whole card single-use.
    """
    token = uuid.uuid4().hex

    def _stamp(actions: Any) -> None:
        for action in actions or []:
            if isinstance(action, dict) and action.get("type") == "Action.Submit":
                data = action.setdefault("data", {})
                if isinstance(data, dict):
                    data["card_token"] = token

    _stamp(card_content.get("actions"))
    for element in card_content.get("body", []):
        if isinstance(element, dict) and element.get("type") == "ActionSet":
            _stamp(element.get("actions"))
    return token


def locked_card(title: str, selected_label: str, status: str = "Processed") -> Dict[str, Any]:
    """A read-only replacement card (no actions) shown in place after a button is clicked."""
    return _card(body=[
        _header(_BRAND, title),
        {"type": "Container", "style": "emphasis", "bleed": True, "spacing": "Medium", "items": [
            {"type": "TextBlock", "text": f"✓ You selected: **{safe_card_text(selected_label)}**", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": f"Status: {safe_card_text(status)} · buttons disabled", "isSubtle": True, "wrap": True, "spacing": "None"},
        ]},
    ])


def safe_card_text(value: str) -> str:
    """Prevent user-controlled Adaptive Card Markdown from changing card rendering."""
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")


def _header(title: str, subtitle: str = "", progress: str = "") -> Dict[str, Any]:
    """Branded header band: logo + title + optional subtitle, with an optional progress chip."""
    title_column: list[dict] = [
        {"type": "TextBlock", "text": f"{title}", "weight": "Bolder", "size": "Large", "color": "Accent", "wrap": True, "spacing": "None"},
    ]
    if subtitle:
        title_column.append({"type": "TextBlock", "text": subtitle, "isSubtle": True, "wrap": True, "spacing": "None"})
    columns = [
        {"type": "Column", "width": "auto", "verticalContentAlignment": "Center", "items": [
            {"type": "TextBlock", "text": _LOGO, "size": "ExtraLarge", "spacing": "None"},
        ]},
        {"type": "Column", "width": "stretch", "verticalContentAlignment": "Center", "items": title_column},
    ]
    if progress:
        columns.append({"type": "Column", "width": "auto", "verticalContentAlignment": "Center", "items": [
            {"type": "TextBlock", "text": progress, "isSubtle": True, "size": "Small", "horizontalAlignment": "Right", "wrap": False},
        ]})
    return {
        "type": "Container", "style": "emphasis", "bleed": True, "spacing": "None",
        "items": [{"type": "ColumnSet", "columns": columns}],
    }


def _card(body: list[dict], actions: list[dict] | None = None, version: str = "1.4") -> Dict[str, Any]:
    card: Dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": version,
        "body": body,
    }
    if actions:
        card["actions"] = actions
    card["msteams"] = {"width": "Full"}
    return card


def _field(component: dict) -> dict:
    component.setdefault("spacing", "Medium")
    return component


def _icon_header(title: str, subtitle: str) -> Dict[str, Any]:
    """Branded header using an Adaptive Cards 1.5 Icon (falls back to the emoji logo on older hosts)."""
    return {
        "type": "ColumnSet",
        "columns": [
            {"type": "Column", "width": "auto", "verticalContentAlignment": "Center", "items": [
                {"type": "Container", "style": "accent", "spacing": "None", "roundedCorners": True, "items": [
                    {"type": "Icon", "name": "LockClosed", "size": "Medium", "color": "Accent",
                     "fallback": {"type": "TextBlock", "text": _LOGO, "size": "ExtraLarge", "spacing": "None"}},
                ]},
            ]},
            {"type": "Column", "width": "stretch", "verticalContentAlignment": "Center", "items": [
                {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "spacing": "None"},
                {"type": "TextBlock", "text": subtitle, "isSubtle": True, "size": "Small", "wrap": True, "spacing": "None"},
            ]},
        ],
    }


def greeting_card(first_name: str, pending_approvals_count: int = 0, last_sync_minutes: int | None = None) -> Dict[str, Any]:
    """Welcome card. Shows a pending-approvals banner + count only when the user has open approvals
    (midPoint-sourced); the sync footer renders only when a freshness figure is supplied."""
    count = max(int(pending_approvals_count or 0), 0)
    name = safe_card_text(first_name)

    body: list[dict] = [
        _icon_header(_BRAND, "Secure access requests & approvals"),
        {"type": "TextBlock", "text": f"Hi **{name}**! Welcome back.\n\nWhat would you like to do today?",
         "wrap": True, "spacing": "Medium"},
    ]
    if count > 0:
        body.append({"type": "Container", "style": "attention", "spacing": "Medium", "roundedCorners": True, "items": [
            {"type": "TextBlock", "text": f"🔔 **{count}** approval(s) waiting for your review", "wrap": True, "spacing": "None"},
        ]})
    if last_sync_minutes is not None:
        body.append({"type": "TextBlock", "text": f"Synced with MidPoint · {int(last_sync_minutes)} min ago",
                     "isSubtle": True, "size": "Small", "wrap": True, "spacing": "Medium"})

    actions = [
        {"type": "Action.Submit", "title": "＋  New access request", "style": "positive", "data": {"action": "open_access_form"}},
        {"type": "Action.Submit", "title": "🗂  My requests", "data": {"action": "my_requests"}},
    ]
    approvals_title = f"✅  View all approvals ({count})" if count > 0 else "✅  My approvals"
    actions.append({"type": "Action.Submit", "title": approvals_title, "data": {"action": "manager_queue"}})
    actions.append({"type": "Action.Submit", "title": "Cancel", "data": {"action": "decline_access_form"}})

    return _card(body=body, actions=actions, version="1.5")


def access_request_form(catalog: list[dict[str, Any]]) -> Dict[str, Any]:
    """Static catalog-driven form (retained as a fallback for non-dynamic deployments)."""
    systems = [{"title": item["system"], "value": item["system"]} for item in catalog]
    roles = sorted({role for item in catalog for role in item.get("roles", [])})
    return _card(
        body=[
            _header(_BRAND, "Raise an access request"),
            _field({"type": "Input.ChoiceSet", "id": "target_type", "label": "What do you need access to?", "choices": [{"title": "Application / System", "value": "resource"}, {"title": "Group", "value": "group"}, {"title": "Role", "value": "role"}], "value": "resource", "isRequired": True, "errorMessage": "Select the access target type."}),
            _field({"type": "Input.ChoiceSet", "id": "target_name", "label": "Target name", "choices": systems, "isRequired": True, "errorMessage": "Select the application, group, or role name."}),
            _field({"type": "Input.ChoiceSet", "id": "role", "label": "Requested access level", "choices": [{"title": role, "value": role} for role in roles], "isRequired": True, "errorMessage": "Select the access level you need."}),
            _field({"type": "Input.Text", "id": "reason", "label": "Business justification", "isMultiline": True, "isRequired": True, "errorMessage": "A business justification is required."}),
            _field({"type": "Input.Text", "id": "description", "label": "Additional description (optional)", "isMultiline": True, "maxLength": 2000, "placeholder": "Optional context for your approver"}),
            _field({"type": "Input.ChoiceSet", "id": "duration", "label": "Access duration", "value": "permanent", "choices": [{"title": "Temporary (30 days)", "value": "30"}, {"title": "Temporary (90 days)", "value": "90"}, {"title": "Permanent", "value": "permanent"}]}),
        ],
        actions=[{"type": "Action.Submit", "title": "Submit for approval", "style": "positive", "data": {"action": "submit_access_form"}}],
    )


def wizard_type_card() -> Dict[str, Any]:
    """Step 1 of the dynamic request wizard: choose what kind of access."""
    return _card(
        body=[
            _header(_BRAND, "Raise an access request", "Step 1 of 3"),
            {"type": "TextBlock", "text": "What do you need access to?", "weight": "Bolder", "wrap": True, "spacing": "Medium"},
            _field({"type": "Input.ChoiceSet", "id": "target_type", "style": "expanded", "value": "resource", "isRequired": True, "errorMessage": "Select an access type.", "choices": [
                {"title": "Application / System", "value": "resource"},
                {"title": "Group", "value": "group"},
                {"title": "Role", "value": "role"},
            ]}),
        ],
        actions=[{"type": "Action.Submit", "title": "Next  →", "style": "positive", "data": {"action": "wizard_pick_type"}}],
    )


def wizard_target_card(target_type: str, options: list[dict], type_label: str) -> Dict[str, Any]:
    """Step 2: choose the specific target, from options fetched live from MidPoint."""
    choices = [{"title": o["name"], "value": f"{o['oid']}|{o['name']}"} for o in options]
    body = [_header(_BRAND, "Raise an access request", "Step 2 of 3")]
    if choices:
        body.append({"type": "TextBlock", "text": f"Choose the {type_label} you need access to.", "weight": "Bolder", "wrap": True, "spacing": "Medium"})
        body.append(_field({"type": "Input.ChoiceSet", "id": "target_choice", "label": type_label.capitalize(), "isRequired": True, "errorMessage": f"Select a {type_label}.", "choices": choices}))
    else:
        body.append({"type": "Container", "style": "warning", "spacing": "Medium", "items": [
            {"type": "TextBlock", "text": f"No {type_label} is available to you right now.", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": "Ask your administrator to grant access in your organisation, then try again.", "isSubtle": True, "wrap": True, "spacing": "None"},
        ]})
    actions = [{"type": "Action.Submit", "title": "←  Back", "data": {"action": "open_access_form"}}]
    if choices:
        actions.append({"type": "Action.Submit", "title": "Next  →", "style": "positive", "data": {"action": "wizard_pick_target", "target_type": target_type}})
    return _card(body=body, actions=actions)


def wizard_access_card(target_type: str, target_name: str, roles: list[dict]) -> Dict[str, Any]:
    """Step 3: for an application, choose the access level plus justification and duration."""
    role_choices = [{"title": r["name"], "value": r["name"]} for r in roles]
    body = [
        _header(_BRAND, "Raise an access request", "Step 3 of 3"),
        {"type": "ColumnSet", "spacing": "Medium", "columns": [
            {"type": "Column", "width": "auto", "verticalContentAlignment": "Center", "items": [{"type": "TextBlock", "text": "📦", "size": "Large", "spacing": "None"}]},
            {"type": "Column", "width": "stretch", "verticalContentAlignment": "Center", "items": [
                {"type": "TextBlock", "text": safe_card_text(target_name), "weight": "Bolder", "size": "Medium", "wrap": True, "spacing": "None"},
                {"type": "TextBlock", "text": "Application", "isSubtle": True, "size": "Small", "spacing": "None"},
            ]},
        ]},
    ]
    if role_choices:
        body.append(_field({"type": "Input.ChoiceSet", "id": "role", "label": "Requested access level", "isRequired": True, "errorMessage": "Select an access level.", "choices": role_choices}))
    else:
        body.append({"type": "Container", "style": "warning", "spacing": "Medium", "items": [
            {"type": "TextBlock", "text": "This application has no access levels configured.", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": "Ask your administrator to add application roles.", "isSubtle": True, "wrap": True, "spacing": "None"},
        ]})
    body += [
        _field({"type": "Input.Text", "id": "reason", "label": "Business justification", "isMultiline": True, "isRequired": True, "errorMessage": "A business justification is required."}),
        _field({"type": "Input.Text", "id": "description", "label": "Additional description (optional)", "isMultiline": True, "maxLength": 2000, "placeholder": "Optional context for your approver"}),
        _field({"type": "Input.ChoiceSet", "id": "duration", "label": "Access duration", "value": "permanent", "choices": [
            {"title": "Temporary — 30 days", "value": "30"}, {"title": "Temporary — 90 days", "value": "90"}, {"title": "Permanent", "value": "permanent"}]}),
    ]
    actions = [{"type": "Action.Submit", "title": "←  Back", "data": {"action": "open_access_form"}}]
    if role_choices:
        actions.append({"type": "Action.Submit", "title": "Submit for approval", "style": "positive", "data": {"action": "submit_application_request", "app_name": target_name}})
    return _card(body=body, actions=actions)


_TYPE_ICON = {"role": "🧩", "service": "🧰", "group": "👥", "resource": "📦"}

# Access categories the first dropdown offers. Each lights up a midPoint object type; the step-2
# target dropdown fills dynamically with live requestable items of the chosen type.
REQUESTABLE_CATEGORIES = [("Roles", "role"), ("Services", "service")]


def requestable_type_card(categories: list[tuple[str, str]] | None = None) -> Dict[str, Any]:
    """Step 1: choose the access type (Roles / Services). The target list is fetched live for the
    choice in step 2 — no free text anywhere; the user only ever picks from midPoint's catalog."""
    cats = categories or REQUESTABLE_CATEGORIES
    choices = [{"title": label, "value": value} for label, value in cats]
    default = cats[0][1] if cats else "role"
    return _card(
        version="1.5",
        body=[
            _icon_header(_BRAND, "Request access · Step 1 of 2"),
            _field({"type": "Input.ChoiceSet", "id": "access_type", "label": "What type of access are you requesting?",
                    "value": default, "style": "expanded" if len(cats) <= 4 else "compact", "isRequired": True,
                    "errorMessage": "Select an access type.", "choices": choices}),
        ],
        actions=[
            {"type": "Action.Submit", "title": "Next  →", "style": "positive", "data": {"action": "req_pick_type"}},
            {"type": "Action.Submit", "title": "Cancel", "style": "destructive", "associatedInputs": "none", "data": {"action": "decline_access_form"}},
        ],
    )


def requestable_target_card(access_type: str, type_label: str, items: list[dict[str, Any]]) -> Dict[str, Any]:
    """Step 2: pick the target from a dropdown of live midPoint requestable items (no typing), add an
    optional description, and choose duration. Duration honours the connector's 30-day cap:
    Permanent, 30 Days, or a Custom end date within 30 days.
    """
    icon = _TYPE_ICON.get(access_type, "•")
    choices = [{"title": f"{icon}  {it['name']}", "value": f"{access_type}|{it['oid']}|{it['name']}"}
               for it in items if it.get("name") and it.get("oid")]
    body: list[dict] = [_icon_header(_BRAND, "Request access · Step 2 of 2")]
    if choices:
        body.append(_field({"type": "Input.ChoiceSet", "id": "access_choice", "label": f"{type_label.capitalize()} name",
                            "placeholder": f"Select a {type_label}", "isRequired": True,
                            "errorMessage": f"Select a {type_label}.", "choices": choices}))
        body.append(_field({"type": "Input.Text", "id": "additionalDescription", "label": "Additional description (optional)",
                            "isMultiline": True, "placeholder": "Optional context for your approver"}))
        body.append(_field({"type": "Input.ChoiceSet", "id": "accessDuration", "label": "Access duration", "value": "permanent", "choices": [
            {"title": "Permanent", "value": "permanent"},
            {"title": "30 Days", "value": "30_days"},
            {"title": "Custom (end date, within 30 days)", "value": "custom"}]}))
        body.append(_field({"type": "Input.Date", "id": "customEndDate",
                            "label": "Custom end date (only if 'Custom' selected; within 30 days)"}))
    else:
        body.append({"type": "Container", "style": "warning", "spacing": "Medium", "roundedCorners": True, "items": [
            {"type": "TextBlock", "text": f"No requestable {type_label} is available to you right now.", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": "Ask your administrator to flag one as requestable, then try again.", "isSubtle": True, "wrap": True, "spacing": "None"}]})
    actions = [{"type": "Action.Submit", "title": "←  Back", "data": {"action": "open_access_form"}}]
    if choices:
        actions.append({"type": "Action.Submit", "title": "Review request", "style": "positive", "data": {"action": "submit_requestable", "target_type": access_type}})
    return _card(body=body, actions=actions, version="1.5")


def requestable_confirm_card(data: Dict[str, str]) -> Dict[str, Any]:
    """Review a requestable selection before it is submitted for approval.

    Single-sentence summary over the real fields (our model has one requestable object — the role
    or service IS the grant — so there's no separate target system to name). Confirm submits via the
    existing `confirm_requestable` handler (carries target_oid / access_until_override through
    `**data`); Cancel ends the session (`cancel_requestable`, distinct from the pending-request
    `cancel_request` action, which needs a request_id).
    """
    itype = str(data.get("target_type") or "role")
    type_label = {"role": "Role", "service": "Service", "group": "Group", "resource": "Application"}.get(itype, itype.capitalize())
    name = safe_card_text(str(data.get("target_name") or "the selected access"))
    duration = safe_card_text(str(data.get("duration") or "Permanent"))
    sentence = f"You are requesting **{name}** (access type **{type_label}**) for **{duration}**."
    return _card(
        version="1.5",
        body=[
            _icon_header(_BRAND, "Review your request"),
            {"type": "Container", "style": "emphasis", "spacing": "Medium", "roundedCorners": True, "items": [
                {"type": "TextBlock", "text": sentence, "wrap": True, "spacing": "None"}]},
        ],
        actions=[
            {"type": "Action.Submit", "title": "Confirm", "style": "positive", "data": {"action": "confirm_requestable", **data}},
            {"type": "Action.Submit", "title": "Cancel", "style": "destructive", "associatedInputs": "none", "data": {"action": "cancel_requestable"}},
        ],
    )


def confirmation_card(data: Dict[str, str]) -> Dict[str, Any]:
    facts = [{"title": label, "value": safe_card_text(data[key])} for label, key in [("Target", "target_name"), ("Access level", "role"), ("Duration", "duration"), ("Justification", "reason")] if data.get(key)]
    if data.get("description"):
        facts.append({"title": "Details", "value": safe_card_text(data["description"])})
    return _card(
        body=[
            _header(_BRAND, "Review your request"),
            {"type": "TextBlock", "text": "Please confirm the details below before submitting for approval.", "isSubtle": True, "wrap": True, "spacing": "Medium"},
            {"type": "Container", "style": "emphasis", "spacing": "Medium", "items": [{"type": "FactSet", "facts": facts}]},
        ],
        actions=[
            {"type": "Action.Submit", "title": "Submit for approval", "style": "positive", "data": {"action": "confirm_access_form", **data}},
            {"type": "Action.Submit", "title": "Edit", "data": {"action": "open_access_form"}},
        ],
    )


_STATUS_TAG = {
    "pending": "🟡 PENDING", "approved": "🟢 APPROVED", "rejected": "🔴 REJECTED",
    "cancelled": "⚪ CANCELLED", "revoked": "⚪ REVOKED", "more_info_required": "🟠 MORE INFO",
    "gathering_params": "🟡 IN PROGRESS",
}


def requests_card(rows: list[tuple]) -> Dict[str, Any]:
    body: list[dict] = [_header(_BRAND, "My access requests")]
    actions: list[dict] = []
    if not rows:
        body.append({"type": "TextBlock", "text": "You have no requests yet.", "isSubtle": True, "wrap": True, "spacing": "Medium"})
    for request_id, resource, role, status in rows:
        tag = _STATUS_TAG.get(status, status.upper())
        body.append({"type": "Container", "separator": True, "spacing": "Medium", "items": [
            {"type": "ColumnSet", "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": f"#{request_id} · {safe_card_text(str(resource))}", "weight": "Bolder", "wrap": True, "spacing": "None"},
                    {"type": "TextBlock", "text": safe_card_text(str(role)), "isSubtle": True, "size": "Small", "spacing": "None"},
                ]},
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center", "items": [
                    {"type": "TextBlock", "text": tag, "size": "Small", "horizontalAlignment": "Right"},
                ]},
            ]},
        ]})
        if status in {"pending", "gathering_params", "more_info_required"}:
            actions.append({"type": "Action.Submit", "title": f"Cancel #{request_id}", "data": {"action": "cancel_request", "request_id": request_id}})
    return _card(body=body, actions=actions)


def _mp_request_status(state: str, outcome: str) -> str:
    """Map a midPoint case (state + outcome URI) to a human status tag."""
    if (state or "").lower() == "open":
        return "PENDING"
    o = (outcome or "").lower()
    if "approve" in o:
        return "APPROVED"
    if "reject" in o:
        return "REJECTED"
    return "COMPLETED"


def midpoint_requests_card(items: list[dict]) -> Dict[str, Any]:
    """Render "my access requests" from midPoint cases (the source of truth). No local state."""
    body: list[dict] = [_header(_BRAND, "My access requests")]
    if not items:
        body.append({"type": "TextBlock", "text": "You have no requests in the identity system yet.", "isSubtle": True, "wrap": True, "spacing": "Medium"})
    for it in items:
        target = safe_card_text(str(it.get("target") or "Access request"))
        tag = _mp_request_status(str(it.get("state") or ""), str(it.get("outcome") or ""))
        body.append({"type": "Container", "separator": True, "spacing": "Medium", "items": [
            {"type": "ColumnSet", "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": target, "weight": "Bolder", "wrap": True, "spacing": "None"},
                ]},
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center", "items": [
                    {"type": "TextBlock", "text": tag, "size": "Small", "horizontalAlignment": "Right"},
                ]},
            ]},
        ]})
    return _card(body=body, actions=[])


def _mp_action_data(payload: Dict[str, Any], action: str) -> Dict[str, Any]:
    """Carry every identifier the approve/reject callback needs to reach midPoint via the JAR."""
    return {
        "action": action,
        "caseOid": str(payload.get("caseOid") or ""),
        "workItemId": str(payload.get("workItemId") or ""),
        "requesterEmail": str(payload.get("requesterEmail") or ""),
        "requesterName": str(payload.get("requesterName") or ""),
        "requestedItem": str(payload.get("requestedItem") or ""),
        "targetType": str(payload.get("targetType") or "role"),
    }


def midpoint_approval_card(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Interactive approval card for a midPoint-raised access request (DM'd to the manager).

    Every visible value is built from the inbound notification. Action buttons carry the midPoint
    case + work-item identifiers (via `_mp_action_data`) so the approve/reject callback completes the
    work item directly through the JAR — independent of the display fields. Facts with no data in the
    current pipeline (business justification, requested-on) render only when the payload supplies
    them, so the card is forward-compatible without fabricating values.
    """
    requester = str(payload.get("requesterName") or payload.get("requesterEmail") or "A user")
    item = str(payload.get("requestedItem") or "").strip()
    if not item:
        item = "(access target missing from request — midPoint notifier may not include requestedItem)"
    duration = str(payload.get("accessDuration") or "").strip() or "Permanent"
    access_type = str(payload.get("targetType") or "role").strip()
    type_label = {"role": "Role", "service": "Service", "group": "Group", "resource": "Application"}.get(access_type, access_type.capitalize())

    facts = [
        {"title": "Requester", "value": safe_card_text(requester)},
        {"title": "Email", "value": safe_card_text(str(payload.get("requesterEmail") or "—"))},
        {"title": "Access Type", "value": safe_card_text(type_label)},
        {"title": "Requested Access", "value": safe_card_text(item)},
        {"title": "Duration", "value": safe_card_text(duration)},
    ]
    justification = str(payload.get("businessJustification") or "").strip()
    if justification:
        facts.append({"title": "Business Justification", "value": safe_card_text(justification)})
    requested_on = str(payload.get("requestedOn") or "").strip()
    if requested_on:
        facts.append({"title": "Requested On", "value": safe_card_text(requested_on)})
    facts.append({"title": "Case ID", "value": safe_card_text(str(payload.get("caseOid") or "—"))})

    return _card(
        version="1.5",
        body=[
            {"type": "TextBlock", "text": _BRAND, "weight": "Bolder", "size": "Large", "spacing": "None"},
            {"type": "TextBlock", "text": "Access request pending approval", "isSubtle": True, "spacing": "None"},
            {"type": "TextBlock", "text": f"**{safe_card_text(requester)}** has requested access to **{safe_card_text(item)}**.", "wrap": True, "spacing": "Medium"},
            {"type": "Container", "style": "emphasis", "spacing": "Medium", "roundedCorners": True, "items": [{"type": "FactSet", "facts": facts}]},
            _field({"type": "Input.Text", "id": "approver_comment", "label": "Comment (required to reject)", "isMultiline": True, "placeholder": "Enter your comments..."}),
            _field({"type": "Input.Date", "id": "temporary_until", "label": "Temporary access until (optional; max 30 days)"}),
        ],
        actions=[
            {"type": "Action.Submit", "title": "Approve", "style": "positive", "data": _mp_action_data(payload, "mp_approve")},
            {"type": "Action.Submit", "title": "Approve temporary access", "data": _mp_action_data(payload, "mp_approve_temporary")},
            {"type": "Action.Submit", "title": "Reject", "style": "destructive", "data": _mp_action_data(payload, "mp_reject")},
        ],
    )


def midpoint_decision_locked_card(payload: Dict[str, Any], decision: str, actor: str, comment: str = "", access_until: str = "") -> Dict[str, Any]:
    """Read-only replacement rendered in place after the manager decides (the 'changeable' card).

    `decision` ∈ approved | rejected | expired. "expired" is a terminal, no-op lock used when the
    request was already decided by another approver or auto-closed (delayed/concurrent approval).
    """
    granted = decision == "approved"
    expired = decision == "expired"
    banner = "🟢 APPROVED" if granted else ("⚪ NO LONGER ACTIONABLE" if expired else "🔴 REJECTED")
    container_style = "good" if granted else ("warning" if expired else "attention")
    requester = str(payload.get("requesterName") or payload.get("requesterEmail") or "A user")
    item = str(payload.get("requestedItem") or "").strip()
    if not item:
        item = "(access target missing from request)"
    duration = str(payload.get("accessDuration") or "").strip() or "Permanent"

    facts = [
        {"title": "Decision:" if not expired else "Status:", "value": banner},
        {"title": "By:", "value": safe_card_text(actor)},
        {"title": "Requester:", "value": safe_card_text(requester)},
        {"title": "Requested access:", "value": safe_card_text(item)},
        {"title": "Duration:", "value": safe_card_text(duration)},
    ]
    if access_until:
        facts.append({"title": "Access until:", "value": safe_card_text(access_until)})
    if comment:
        facts.append({"title": "Comment:", "value": safe_card_text(comment)})
    return _card(body=[
        _header(_BRAND, "Access request decision"),
        {"type": "Container", "style": container_style, "bleed": True, "spacing": "Medium",
         "items": [{"type": "TextBlock", "text": banner, "weight": "Bolder", "size": "Medium", "wrap": True}]},
        {"type": "Container", "style": "emphasis", "spacing": "Medium", "items": [{"type": "FactSet", "facts": facts}]},
    ])


def midpoint_result_card(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Outcome card DM'd to the requester when their access request is decided."""
    status = str(payload.get("status") or "").upper()
    granted = status == "GRANTED"
    banner = "🟢 Access granted" if granted else "🔴 Request declined"
    item = str(payload.get("requestedItem") or "").strip() or "your requested access"
    access_type = str(payload.get("targetType") or "").strip()
    type_label = {"role": "Role", "service": "Service", "group": "Group", "resource": "Application"}.get(access_type, "")
    # body_text is composed safely by the caller (dynamic parts pre-escaped); render it once without
    # re-escaping, so intended **bold** markdown isn't turned into literal asterisks.
    body_text = payload.get("message") or (
        f"Your request for **{safe_card_text(item)}** was **{'granted' if granted else 'declined'}**."
    )
    facts = [{"title": "Requested access", "value": safe_card_text(item)}]
    if type_label:
        facts.append({"title": "Access type", "value": type_label})
    if granted and payload.get("accessUntil"):
        facts.append({"title": "Access until", "value": safe_card_text(str(payload.get("accessUntil")))})
    if not granted and str(payload.get("comment") or "").strip():
        facts.append({"title": "Reason", "value": safe_card_text(str(payload.get("comment")))})
    facts.append({"title": "Case", "value": safe_card_text(str(payload.get("caseOid") or "—"))})
    return _card(
        version="1.5",
        body=[
            _icon_header(_BRAND, "Access request update"),
            {"type": "Container", "style": "good" if granted else "attention", "bleed": True, "spacing": "Medium",
             "roundedCorners": True, "items": [{"type": "TextBlock", "text": banner, "weight": "Bolder", "size": "Medium", "wrap": True}]},
            {"type": "TextBlock", "text": str(body_text), "wrap": True, "spacing": "Medium"},
            {"type": "Container", "style": "emphasis", "spacing": "Medium", "roundedCorners": True,
             "items": [{"type": "FactSet", "facts": facts}]},
        ],
    )


def manager_queue_card(rows: list[tuple]) -> Dict[str, Any]:
    body: list[dict] = [_header(_BRAND, "Approval queue")]
    actions: list[dict] = []
    if not rows:
        body.append({"type": "TextBlock", "text": "Nothing awaiting your review.", "isSubtle": True, "wrap": True, "spacing": "Medium"})
    for request_id, requester, resource, role, risk, status, access_until in rows:
        expiry = f" · expires {access_until}" if access_until else ""
        risk_tag = {"high": "🔴 HIGH", "medium": "🟡 MEDIUM", "low": "🟢 LOW"}.get(str(risk).lower(), str(risk).upper())
        body.append({"type": "Container", "separator": True, "spacing": "Medium", "items": [
            {"type": "ColumnSet", "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": f"#{request_id} · {safe_card_text(str(requester))}", "weight": "Bolder", "wrap": True, "spacing": "None"},
                    {"type": "TextBlock", "text": f"{safe_card_text(str(resource))} / {safe_card_text(str(role))}{expiry}", "isSubtle": True, "size": "Small", "wrap": True, "spacing": "None"},
                ]},
                {"type": "Column", "width": "auto", "verticalContentAlignment": "Center", "items": [
                    {"type": "TextBlock", "text": risk_tag, "size": "Small", "horizontalAlignment": "Right"},
                    {"type": "TextBlock", "text": _STATUS_TAG.get(status, status.upper()), "size": "Small", "horizontalAlignment": "Right", "spacing": "None"},
                ]},
            ]},
        ]})
        if status == "approved" and access_until:
            actions.append({"type": "Action.Submit", "title": f"Revoke #{request_id}", "style": "destructive", "data": {"action": "revoke_temporary", "request_id": request_id}})
    return _card(body=body, actions=actions)
