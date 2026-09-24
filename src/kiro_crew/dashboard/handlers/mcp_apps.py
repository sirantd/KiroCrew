"""MCP Apps handlers — the UI→gateway tool-callback relay (SEP-1865).

``POST /api/mcp-apps/call`` lets an embedded MCP App iframe invoke one of its
server's *app-visible* tools. The dashboard is a thin relay: it validates the
request shape, performs a cheap local capability pre-check (the spool record
must exist), and forwards a one-shot ``app-call`` control frame over the
gateway's uid-gated unix socket. ALL authorization decisions — spool-token
re-verification, backend routing, ``_meta.ui.visibility`` enforcement, SEL
auditing — happen gateway-side in :mod:`kiro_crew.mcp_gateway.app_call`, so a
bug here cannot grant an app a tool the gateway would refuse.

Body: ``{"spool_id": str, "tool": str, "arguments": {..}}``.
Replies: 200 ``{"result": ...}`` / ``{"error": ...}`` (backend JSON-RPC error),
403 for gateway policy rejections, 404 for unknown/expired spool ids,
503 when gatewayd is unreachable, 504 on timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from kiro_crew import security
from kiro_crew.dashboard.chat_utils import _history_key_for, effective_session_key
from kiro_crew.dashboard.handlers._shared import _is_restricted_session, _read_session_key
from kiro_crew.mcp_apps_render import load_spool
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.rewriter import default_socket_path
from kiro_crew.sel import SecurityEventLog

logger = logging.getLogger(__name__)


def _redact_result(obj):
    """Recursively credential/exfil-URL redact string leaves of a gateway
    result before it is returned to the server-authored iframe (which can reach
    its declared CSP origins). Same discipline as the render-side redaction."""
    if isinstance(obj, str):
        return security.redact(obj)
    if isinstance(obj, list):
        return [_redact_result(x) for x in obj]
    if isinstance(obj, dict):
        # Redact string KEYS too (a credential can appear as a key).
        return {
            (security.redact(k) if isinstance(k, str) else k): _redact_result(v)
            for k, v in obj.items()
        }
    return obj


#: Overall budget for the gateway round-trip. Must exceed the gateway's own
#: combined inner budgets (10s tools/list + 20s tools/call) with real margin
#: for scheduling/socket/validation/governance overhead — an outer budget
#: equal to the inner sum turns legitimate near-limit calls into 504s.
_GATEWAY_TIMEOUT_SECS = 45.0

#: Cap for one reply line from the gateway. Aligned with the gateway's own
#: configurable frame limit (64 MiB) plus wrapper margin — a smaller relay cap
#: turned a completed call carrying large structured/non-text content into a
#: false 503 even though the gateway would have delivered it.
_MAX_REPLY_BYTES = 64 * 1024 * 1024 + 64 * 1024


def _socket_path() -> str:
    """The gateway socket, honoring the ``mcp_gateway.socket_path`` config
    override (empty -> the standard ``$KIROCREW_HOME`` location)."""
    try:
        # circular import: config.loader pulls dashboard modules; keep lazy.
        from kiro_crew.config.loader import KiroCrewConfig

        configured = (KiroCrewConfig.load().mcp_gateway.socket_path or "").strip()
        if configured:
            return configured
    except Exception:  # pragma: no cover — config load must not break the relay
        logger.debug("config load failed; using default gateway socket", exc_info=True)
    return str(default_socket_path())


async def _gateway_app_call(frame: dict) -> dict:
    """Send one ``app-call`` frame to gatewayd; return the reply frame."""
    # limit: asyncio's default StreamReader limit is 64 KiB — readline() on a
    # legitimate reply above that raises instead of relaying. Size the buffer
    # to our own reply cap (+1 so a cap-sized line still parses).
    reader, writer = await transport.connect(_socket_path(), limit=_MAX_REPLY_BYTES + 1)
    try:
        writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
        if not line or len(line) > _MAX_REPLY_BYTES:
            raise ConnectionError("gateway closed without a reply")
        return json.loads(line.decode("utf-8"))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _gate_app_capability_request(
    request: web.Request, spool_id: str, operation: str
) -> tuple[dict | None, str, web.Response | None]:
    """The shared gate ladder for app-capability endpoints.

    Returns ``(record, caller_session, None)`` when the request may proceed, or
    ``(None, caller_session, response)`` carrying the denial to return. Gates,
    in order:

    1. AUTHENTICATED owner (the token-auth middleware's ``request["user"]`` /
       ``request["app"]``, never a client-set header) — app tokens and
       non-owner subjects are refused: an embedded app's capability must only
       ever be exercised by the owner's own dashboard.
    2. Ephemeral gate: incognito/guest sessions must not be able to exercise a
       (leaked or shoulder-surfed) spool capability — the same server-side gate
       memory operations use.
    3. Capability pre-check: the spool record must exist. ``to_thread``:
       records can be multi-MB and this runs on the dashboard event loop.
    4. Session-ownership binding. The record names the session that PRODUCED
       the app — its own key, which is ``dashboard:<slot>`` for a
       dashboard-born session and the channel's own key for one that started
       on a channel — while the honest client echoes the bare slot key.
       Canonicalize the caller's spelling or a legitimate owner is always
       refused as a mismatch. Which canonical form is correct is a property of
       the slot, not of the string: prefixing a channel-born key yields
       ``dashboard:slack:<ts>``, a key no session ever has. So ask the slot
       the caller named what session it runs, the same rule the render side
       binds on, and fall back to the prefix form when no such slot is open.
       Still fail-closed: identity is a single-valued function of the key the
       caller supplied, the accepted set stays two spellings of that one key,
       and the binding read here is written server-side from the session map —
       a caller can only ever resolve to the session it already claims to be.
       Records without a session binding fall through (for ``/call``, the
       gateway's own ladder still applies).

    Every denial is SEL-audited — an unknown/expired spool id or a foreign
    session key reaching an app-capability endpoint is a denied capability
    presentation worth the trail.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    caller_session = _read_session_key(request)

    owner_denied = await require_owner_dashboard_request(request, operation)
    if owner_denied is not None:
        return None, caller_session, owner_denied

    if _is_restricted_session(request.app["state"], request):
        _audit_denied(operation, caller_session, "restricted_session_block")
        return (
            None,
            caller_session,
            web.json_response({"error": "not available in this session"}, status=403),
        )

    record = await asyncio.to_thread(load_spool, spool_id)
    if record is None:
        _audit_denied(operation, caller_session, f"unknown_or_expired_spool spool_id={spool_id}")
        return (
            None,
            caller_session,
            web.json_response({"error": "unknown or expired app"}, status=404),
        )

    record_session = str(record.get("session_key") or "")
    caller_slot = request.app["state"]._slots.get(caller_session) if caller_session else None
    if caller_slot is not None:
        caller_canonical = effective_session_key(caller_slot)
    else:
        caller_canonical = _history_key_for(caller_session) if caller_session else ""
    if record_session and record_session not in (caller_session, caller_canonical):
        _audit_denied(operation, caller_session, f"spool_session_mismatch spool_id={spool_id}")
        return (
            None,
            caller_session,
            web.json_response({"error": "app belongs to another session"}, status=403),
        )

    return record, caller_session, None


def _audit_denied(operation: str, caller_session: str, resources: str) -> None:
    """SEL-audit one denied app-capability presentation (audit never breaks the deny)."""
    try:
        SecurityEventLog().log_api_access(
            caller=caller_session or "unknown",
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=resources,
        )
    except Exception:  # pragma: no cover — audit must not break the deny
        logger.debug("SEL audit for denied %s failed", operation, exc_info=True)


async def api_mcp_apps_call(request: web.Request) -> web.Response:
    """POST /api/mcp-apps/call — relay an app's tools/call to the gateway."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    spool_id = body.get("spool_id")
    tool = body.get("tool")
    arguments = body.get("arguments", {})
    # Callback capability: delivered to the iframe ONLY over the owner-WS
    # render frame; relayed back here in the (owner-authenticated, non-model-
    # visible) POST body and forwarded to the gateway, which authorizes on it.
    callback_secret = body.get("callback_secret")
    if not isinstance(spool_id, str) or not spool_id:
        return web.json_response({"error": "missing spool_id"}, status=400)
    if not isinstance(tool, str) or not tool:
        return web.json_response({"error": "missing tool"}, status=400)
    if not isinstance(arguments, dict):
        return web.json_response({"error": "arguments must be an object"}, status=400)

    # AUTHENTICATED owner gate (server-side, load-bearing): the caller's
    # identity comes from the token-auth middleware (`request["user"]` /
    # `request["app"]`), not from any client-set header. App tokens and
    # non-owner subjects are refused — an embedded app's capability must only
    # ever be exercised by the owner's own dashboard.
    record, caller_session, denied = await _gate_app_capability_request(
        request, spool_id, "mcp-apps.call"
    )
    if denied is not None:
        return denied
    assert record is not None  # _gate returns a record whenever denied is None
    del record  # the gateway re-reads and re-verifies the record itself

    frame = {
        "type": "app-call",
        "spool_id": spool_id,
        "callback_secret": callback_secret,
        "tool": tool,
        "arguments": arguments,
    }
    try:
        reply = await asyncio.wait_for(_gateway_app_call(frame), timeout=_GATEWAY_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        return web.json_response({"error": "gateway timed out"}, status=504)
    except (ConnectionError, OSError, ValueError) as exc:
        logger.warning("mcp-apps call relay failed: %s", exc)
        return web.json_response({"error": "MCP gateway not available"}, status=503)

    kind = reply.get("type") if isinstance(reply, dict) else None
    if kind == "app-result":
        # Offload redaction (recurses over payloads up to the frame cap).
        redacted = await asyncio.to_thread(_redact_result, reply.get("result"))
        return web.json_response({"result": redacted})
    if kind == "app-error":
        # Backend JSON-RPC error the app understands — redact it too: an error
        # message/data can carry credential-bearing backend output before it
        # reaches the network-capable iframe.
        redacted_err = await asyncio.to_thread(_redact_result, reply.get("error"))
        return web.json_response({"error": redacted_err})
    if kind == "app-call-rejected":
        reason = str(reply.get("reason", "rejected"))
        status = 404 if "unknown or expired" in reason else 403
        return web.json_response({"error": reason}, status=status)
    logger.warning("mcp-apps call: unexpected gateway reply type %r", kind)
    return web.json_response({"error": "unexpected gateway reply"}, status=502)


# --- ui/message: app → conversation delivery (SEP-1865) ----------------------

#: Provenance banner wrapped around every app-originated message. Structural
#: classification is by the queue entry's ``kind`` tag (unforgeable, set at
#: enqueue time); the banner exists for the MODEL and the transcript reader,
#: so the turn's text says who authored it. Mirrors ``CRON_NOTIFY_PREFIX``.
#: Provenance banner constants live in ``chat_utils`` beside
#: ``MCP_APP_MESSAGE_KIND`` (the drain reads them too); re-exported here for
#: the endpoint's tests and callers.
from kiro_crew.dashboard.chat_utils import (  # noqa: E402
    APP_MESSAGE_END,
    APP_MESSAGE_PREFIX,
)

#: Total text budget for one ui/message delivery. The text becomes model
#: context verbatim, so the cap bounds what a hostile app can inject per
#: message; interactions apps legitimately send (a click, a form, a selection
#: summary) are far below it.
_MESSAGE_MAX_CHARS = 16_384

#: Minimum spacing between deliveries from one app instance. ui/message starts
#: (or queues) a MODEL TURN, which is orders of magnitude more expensive than
#: the tools/call relay's per-frame concurrency cap — a compromised app looping
#: sendMessage would otherwise burn tokens as fast as turns complete. Keyed by
#: spool id (one rendered app instance); state is in-memory and advisory across
#: restarts, which is fine — the budget it protects is per-burst, not exact.
_MESSAGE_MIN_INTERVAL_SECS = 3.0
_last_message_at: dict[str, float] = {}

#: Per-field bound for the MCP-backend-declared ``server`` / ``tool`` names
#: retained in the provenance label. Real names are tens of characters; the
#: bound exists because a hostile backend can declare a multi-megabyte tool
#: name and the label is retained in prompt, transcript, meta and audit.
_LABEL_MAX_CHARS = 128


def _bounded_label(value: str) -> str:
    """Sanitize one label field at its single point of retention.

    The label is interpolated into the provenance banner line, so it gets the
    SAME delimiter treatment the body gets (`_extract_message_text`) plus the
    banner's own syntax characters: a hostile backend can declare a tool named
    ``x"] [End of MCP app message] …`` and — with only a length clip — close
    the envelope from inside the banner while the dashboard's strip regex
    hides the forged line from the operator. Quote, bracket and line breaks
    are dropped (they are banner syntax, not name material), banner delimiter
    text is zero-width-space broken, and the result is clipped with the cut
    marked so a truncated label is visibly truncated.
    """
    value = value.replace("\r", " ").replace("\n", " ")
    value = value.replace('"', "").replace("[", "(").replace("]", ")")
    # The bracket swap above already breaks delimiter text; the explicit
    # neutralization stays so the two sanitizers cannot drift apart if the
    # banner shape ever changes.
    for marker in (APP_MESSAGE_END, APP_MESSAGE_PREFIX):
        if marker in value:
            value = value.replace(marker, marker.replace("[", "[\u200b", 1))
    if len(value) <= _LABEL_MAX_CHARS:
        return value
    return value[: _LABEL_MAX_CHARS - 1] + "…"


def _extract_message_text(params: dict) -> tuple[str, str]:
    """Validate ui/message params and return ``(text, error)``.

    The host capability declares ``message: {text: {}}`` — text blocks only.
    Non-text blocks are refused rather than dropped: an app that sent an image
    must not observe a success that silently delivered half its message.
    """
    if params.get("role") != "user":
        return "", 'role must be "user"'
    content = params.get("content")
    if not isinstance(content, list) or not content:
        return "", "content must be a non-empty array"
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return "", "only text content blocks are supported"
        text = block.get("text")
        if not isinstance(text, str):
            return "", "text block without string text"
        parts.append(text)
    joined = "\n".join(parts).strip()
    if not joined:
        return "", "message text is empty"
    if len(joined) > _MESSAGE_MAX_CHARS:
        return "", f"message exceeds {_MESSAGE_MAX_CHARS} characters"
    # Neutralize the provenance delimiters inside the body: an app whose text
    # contains the literal end banner would otherwise close its own envelope
    # and the remainder would reach the model as text that appears to
    # originate OUTSIDE the app block. Zero-width-space the leading bracket so
    # the text stays readable but never matches the banner lines.
    for marker in (APP_MESSAGE_END, APP_MESSAGE_PREFIX):
        if marker in joined:
            joined = joined.replace(marker, marker.replace("[", "[\u200b", 1))
    return joined, ""


async def api_mcp_apps_message(request: web.Request) -> web.Response:
    """POST /api/mcp-apps/message — deliver an app's ui/message into its session.

    The SEP-1865 return channel: an embedded app sends a user-role message
    (an Acknowledge click, a form submission, a selection) and it arrives in
    the conversation of the session that rendered the app, starting a turn —
    or queueing behind the live one — exactly like the cron origin-injection
    path.

    Unlike ``/call`` there is no gateway leg (nothing is asked of any MCP
    backend), so the ``callback_secret`` — which the gateway verifies for
    calls — is verified HERE, constant-time, against the spool record. A
    marker id alone must never start model turns.

    Body: ``{"spool_id", "callback_secret", "role": "user", "content": [..]}``.
    Replies mirror the spec's in-band result: 200 ``{"result": {"isError":
    false}}`` on delivery; 4xx/5xx with ``{"error": ...}`` otherwise (the
    frontend maps those to ``isError: true``). The result deliberately carries
    NOTHING beyond the SEP contract: queue-vs-turn state is recorded in the
    SEL audit line, where it has a reader — exposed to app authors it would be
    a field someone starts depending on, honored forever.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    spool_id = body.get("spool_id")
    if not isinstance(spool_id, str) or not spool_id:
        return web.json_response({"error": "missing spool_id"}, status=400)

    record, caller_session, denied = await _gate_app_capability_request(
        request, spool_id, "mcp-apps.message"
    )
    if denied is not None:
        return denied
    assert record is not None

    # The capability itself. /call defers this to the gateway's ladder; this
    # endpoint IS the authority, so a missing or mismatched secret is refused
    # here, constant-time, before any part of the message is acted on. The
    # comparison is over BYTES: `hmac.compare_digest` raises TypeError on a
    # non-ASCII str, so a forged secret carrying one non-ASCII character would
    # otherwise 500 BEFORE the audit line — a probe that leaves no SEL trail.
    # `surrogatepass` keeps even lone-surrogate garbage encodable, so every
    # malformed presentation reaches the same audited deny.
    import hmac

    record_secret = record.get("callback_secret")
    presented = body.get("callback_secret")
    if (
        not isinstance(record_secret, str)
        or not record_secret
        or not isinstance(presented, str)
        or not hmac.compare_digest(
            presented.encode("utf-8", "surrogatepass"),
            record_secret.encode("utf-8", "surrogatepass"),
        )
    ):
        _audit_denied(
            "mcp-apps.message", caller_session, f"bad_callback_secret spool_id={spool_id}"
        )
        return web.json_response({"error": "invalid callback capability"}, status=403)

    text, param_err = _extract_message_text(body)
    if param_err:
        return web.json_response({"error": param_err}, status=400)

    # Turn-rate floor (see _MESSAGE_MIN_INTERVAL_SECS). Checked after the
    # capability gate so an unauthorized caller learns nothing about the
    # app's send cadence.
    import time as _time

    now = _time.monotonic()
    last = _last_message_at.get(spool_id, 0.0)
    if now - last < _MESSAGE_MIN_INTERVAL_SECS:
        return web.json_response({"error": "too many messages; slow down"}, status=429)
    # Recency-ordered: re-inserting moves the key to the tail, so the bounded
    # eviction below removes the LEAST-RECENTLY-active app instance — an
    # update-in-place would keep insertion order and evict a still-active
    # early app before a one-shot later one, letting it slip the floor once.
    _last_message_at.pop(spool_id, None)
    _last_message_at[spool_id] = now
    if len(_last_message_at) > 512:  # bound the map like the reveal set does
        _last_message_at.pop(next(iter(_last_message_at)))

    # App-authored text entering the conversation: same redaction discipline
    # as every other server-authored string that reaches a transcript. The
    # server/tool names are MCP-backend-declared, so they get their own bound
    # BEFORE retention or interpolation — the 16 KiB message cap bounds only
    # `text`, and an unbounded label would ride into the prompt, transcript,
    # meta and audit under it. Truncation is marked so a clipped label is
    # visibly clipped rather than silently a different name.
    text = security.redact(text)
    server = _bounded_label(security.redact(str(record.get("server") or "")))
    tool = _bounded_label(security.redact(str(record.get("tool") or "")))
    label = f"{server}/{tool}"

    state = request.app["state"]
    session_key = str(record.get("session_key") or "")
    # The record's session_key is the PRODUCING session's canonical key — the
    # channel's or cron's own key for a session born there, not a dashboard
    # name. `dashboard_slot_key` owns that mapping (a bare prefix strip yields
    # names like `cron_<id>` that no slot has ever had, hard-409ing every app
    # rendered outside a dashboard-keyed session); fall back to the historical
    # prefix strip when it answers "" so a dashboard-born key still resolves.
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    slot_key = dashboard_slot_key(session_key) or session_key.removeprefix("dashboard:")

    # Resolve the owning slot; rehydrate from history when it is not live.
    # Truly-gone sessions (never persisted, deleted, or closed) stay None and
    # the delivery is refused — the app learns its host conversation is gone.
    from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

    slot = state.get_slot(slot_key)
    if slot is None:
        slot = await rehydrate_slot_from_history_async(state, slot_key)
    if slot is None:
        _audit_denied("mcp-apps.message", caller_session, f"session_gone spool_id={spool_id}")
        return web.json_response({"error": "session is closed"}, status=409)

    wrapped = f'{APP_MESSAGE_PREFIX}"{label}"]\n{text}\n{APP_MESSAGE_END}'
    inject_cls = json.dumps({"appLabel": label})

    from kiro_crew.dashboard.chat_utils import MCP_APP_MESSAGE_KIND

    # Queue while a turn is live OR a multi-stage plan is mid-flight — same
    # predicate as the cron origin-injection and user-typed paths, for the
    # same reason: an injection must never start a concurrent turn.
    if slot.running or slot._in_stage_execution:
        from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES

        if len(slot._queue) >= MAX_LIVE_QUEUE_ENTRIES:
            return web.json_response({"error": "session queue is full"}, status=429)
        # Admission containment stamp: app messages are NOT structurally exempt
        # from `_drop_stale_admissions` the way cron/subagent entries are (those
        # are runner-minted; this text is app-authored). Stamping the snapshot
        # that held at admission means the drain delivers under unchanged
        # containment and correctly DROPS the entry when a channel or mirror
        # link appeared while it waited — and an unstamped entry would fail
        # closed against the full constraint set on any linked session.
        from kiro_crew.dashboard import session_control

        qid = slot.queue_append(
            wrapped,
            kind=MCP_APP_MESSAGE_KIND,
            meta=session_control.containment_meta(state, slot),
        )
        cls = json.loads(inject_cls)
        cls["queue_id"] = qid
        # `meta.kind` mirrors the entry's structural tag onto the transcript
        # twin, so the LIVE queue card classifies it the same way a reloaded
        # tab does (the slot-detail queue view carries the same key).
        slot.append(
            "queued",
            wrapped,
            json.dumps(cls),
            meta={"kind": MCP_APP_MESSAGE_KIND, "appLabel": label},
        )
        state.push_slots_update()
        delivery = "queued"
    else:
        # circular import: chat_runner imports from kiro_crew.dashboard.handlers,
        # so it cannot be imported at module top-level without a cycle.
        from kiro_crew.dashboard.chat_runner import _run_chat
        from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

        # `cls` is not persisted for role `inject`, so the label also rides in
        # `meta` — otherwise the row loses its identity on the next rehydrate.
        slot.append(
            "inject",
            wrapped,
            inject_cls,
            meta={"injectKind": "mcp_app", "appLabel": label},
        )
        task = spawn_guarded_turn(
            state,
            slot,
            _run_chat(
                state,
                slot,
                wrapped,
                _directive_user_origin=False,
                # App-authored, not user speech: this is what suppresses the
                # linked-thread mirror that would otherwise post the text to a
                # channel as the human's own words (the queued twin gets the
                # same treatment structurally, via its kind in
                # `is_synthetic_payload_item`).
                _synthetic_payload=True,
                # Structural provenance for the session ledger: the queued twin
                # above carries MCP_APP_MESSAGE_KIND, and this branch is the
                # same injector dispatching directly. "app" is the crew-log
                # actor for app-authored turns (crew_log.emit.ACTORS).
                _turn_actor="app",
            ),
        )
        slot.task = task
        state.push_slots_update()
        delivery = "turn"

    try:
        SecurityEventLog().log_api_access(
            caller=caller_session or "unknown",
            operation="mcp-apps.message",
            outcome="success",
            source="dashboard",
            resources=f"spool_id={spool_id} app={label} delivery={delivery}",
        )
    except Exception:  # pragma: no cover — audit must not undo the delivery
        logger.debug("SEL audit for delivered mcp-apps message failed", exc_info=True)

    return web.json_response({"result": {"isError": False}})
