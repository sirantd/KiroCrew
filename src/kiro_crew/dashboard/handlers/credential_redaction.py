"""Dashboard handlers for the owner's credential-redaction switch.

Two routes, both OWNER-gated, and the PUT is the ONLY writer of the
``credential_redaction.json`` keystone (see ``security.redaction_switch`` for
what the switch does and does not govern).

Why owner-gated rather than merely authenticated: the switch turns off the
credential pass in the owner's dashboard file viewer, the one surface it
governs. A Slack-allow-listed non-owner running ``!dashboard`` authenticates
with ``app == ""`` and ``sub != owner_id``, so plain token auth admits them; the
owner predicate does not. Same shape as the flagged-file delivery consent
handler, and for the same reason.

Known limit, stated rather than papered over: the owner gate is an IDENTITY
check, not a proof that a human clicked. An agent driving the owner's
authenticated browser through computer use could flip the switch. What that
buys it is bounded to the owner's own file viewer -- exfiltration-URL redaction
and every request-blocking gate stay on regardless (``redaction_switch`` module
docstring) -- and every change is SEL-audited, so the flip leaves a record even
though the redaction does not.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.security import redaction_switch

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_INVALID_JSON = "invalid_json"
_CODE_INVALID_BODY = "invalid_body"
_CODE_WRITE_FAILED = "credential_redaction_write_failed"

_SOURCE = "credential-redaction"


def _audit(*, outcome: str, caller: str, detail: str = "") -> None:
    """Record a switch change or a refused caller in the SEL. Never raises."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=f"credential_redaction.{outcome}",
            outcome=outcome,
            source=_SOURCE,
            resources=detail[:200],
        )
    except Exception:  # pragma: no cover - audit must never break the gate
        logger.debug("could not write the credential-redaction audit event", exc_info=True)


async def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse anyone but the dashboard OWNER; audited off the event loop."""
    if is_owner_dashboard_request(request):
        return None
    logger.warning(
        "refused %s: the credential-redaction switch is a dashboard owner action (app=%s)",
        operation,
        request.get("app"),
    )
    await asyncio.to_thread(
        _audit, outcome="denied", caller="gateway", detail=f"{operation}: non-owner caller refused"
    )
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


async def api_credential_redaction_get(request: web.Request) -> web.Response:
    """GET /api/security/credential-redaction -- the switch as recorded."""
    denied = await _deny_non_owner(request, "credential_redaction.read")
    if denied:
        return denied
    state = await asyncio.to_thread(redaction_switch.read_state)
    return web.json_response(state.to_dict())


async def api_credential_redaction_put(request: web.Request) -> web.Response:
    """PUT /api/security/credential-redaction -- record ``{"enabled": bool}``.

    ``enabled`` must be a JSON boolean; anything else is a 400 and nothing is
    written. The record carries the time of the change so the card can say when
    redaction was switched off.
    """
    denied = await _deny_non_owner(request, "credential_redaction.write")
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        return web.json_response(
            {"error": "enabled must be a boolean", "code": _CODE_INVALID_BODY}, status=400
        )
    changed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _write_and_audit() -> redaction_switch.RedactionState:
        # ONE unit of work on the worker thread: the write and its audit record.
        # ``asyncio.to_thread`` cannot cancel a running thread, so if this
        # handler is cancelled (client gone, gateway stopping) while the write
        # is in flight, the thread still completes -- and must still record the
        # audit, or the authorization would change without the promised trace.
        try:
            state = redaction_switch.set_enabled(enabled, changed_at=changed_at)
        except OSError as exc:
            _audit(outcome="write_failed", caller="owner", detail=type(exc).__name__)
            raise
        _audit(
            outcome="enabled" if enabled else "disabled",
            caller="owner",
            detail=f"changed_at={changed_at}",
        )
        return state

    try:
        state = await asyncio.to_thread(_write_and_audit)
    except OSError as exc:
        logger.error("could not write the credential-redaction switch: %s", exc)
        return web.json_response(
            {"error": "could not record the switch", "code": _CODE_WRITE_FAILED}, status=500
        )
    return web.json_response(state.to_dict())
