"""The owner's switch for credential redaction: ON by default, OFF on request.

Why a switch exists
-------------------
Every output surface -- the chat stream, the file viewer, the outbox download,
Slack and channel egress -- runs :func:`security.redaction.redact_credentials`
over what the agent produced before the owner sees it. The scrubber is
shape-based and deliberately over-inclusive (redacting a lookalike is the safe
direction for a secret), so it also swallows values the owner legitimately
needs to read back: the ``?token=`` value of a one-time approval-workflow link,
a base64 blob the owner generated on purpose, a test fixture that happens to
look like a key. Piping such a value to a file did not help, because the file
viewer runs the same pass. This switch lets the owner read such a FILE as
written. It reaches the CREDENTIAL pass only: a URL the exfiltration-URL pass
classifies as carrying data out is still replaced whole, on this surface as on
every other, unless a loaded companion's own host exemptions
(``CredentialPolicy.exempt_exact_hosts``) admit its host -- so whether a given
approval link becomes readable depends on that classification, not on this
switch.

Where the switch applies: OWNER-VIEW surfaces only
-------------------------------------------------
The switch is honoured ONLY inside an explicit :func:`owner_view` scope, which
the caller enters at a seam whose audience is the owner reading their own
dashboard, and only after that caller has verified the REQUESTER is the owner
(``owner_view_for_request``): the file viewer and its live refresh
(``handlers/files.py`` ``api_file_read`` / ``api_file_watch``). That is the
whole surface, and it is the one the motivating workflow needs: ask the agent to
write the value to a file, open the file. Chat is deliberately NOT a seam:
``chat_runner._flush_segment`` redacts the assistant text BEFORE ``slot.append``,
so the transcript holds the redacted bytes and no display-time scope could
restore them, and the live wire streams fan one chunk to every connected client
besides. Outside that scope ``redact_credentials`` is unconditional, exactly as
before: the chat, Slack, Webex, Discord, Telegram and every other channel egress,
the ACP prompt the model reads, the persisted-history load pass (whose bytes
also feed the model prompt), and every ADMISSION predicate that decides whether
a file may leave the machine (``file_send``, ``_gate_upload_file``, the outbox
flagged-file check) keep scanning whatever the owner chose. The design is the
opposite of a global bypass on purpose: a scope a caller must open cannot be
inherited by a third-party sink that forgot to opt out, so adding a channel
keeps the backend rule "scan before posting to any external surface" without
that channel knowing the switch exists.

What the switch does NOT do
---------------------------
* It never touches a request-BLOCKING decision. ``exfil.py`` decides whether a
  command or URL is refused through ``_contains_fixed_credential`` and its
  siblings, which read the pattern table directly and never call the redaction
  pass; those gates keep firing with the switch off.
* It never touches exfiltration-URL redaction. ``redact_exfiltration_urls`` is
  the control that stops a prompt-injected agent from carrying secrets out in a
  URL the dashboard would render and the browser would fetch; that pass has its
  own exemption seam (``CredentialPolicy.exempt_exact_hosts``) and stays
  unconditional.
* It never touches the diagnostics bundle, logs, the SEL, or any persisted
  copy: none of those paths opens the scope, so they are scrubbed whatever the
  owner chose for their own screen.

Why the record is a keystone file and not ``config.json``
---------------------------------------------------------
Switching the scrubber off is an authorization, not a preference, and the party
it constrains is the agent. ``config.json`` is writable by any auto-approved
agent shell, so a switch stored there could be flipped by a prompt-injected
agent that then prints the secrets it can read. The record therefore lives at
:func:`config.loader.credential_redaction_path`, on the read+write KEYSTONE
floor (``security._CREW_SECRET_LEAVES``) beside ``file_delivery_consent.json``:
``is_sensitive_path`` refuses the tool path and the OS sandbox masks the leaf,
so the only writer is the owner-gated dashboard handler.

Fail direction
--------------
Every read that cannot positively establish ``enabled: false`` -- a missing
file, an unreadable one, malformed JSON, a non-boolean value -- answers
``True``. A switch whose record cannot be read is a switch that is ON.

Read cost
---------
:func:`credential_redaction_enabled` can be reached from a coroutine on the
gateway event loop (the file viewer), so it
never touches the filesystem itself: it returns the in-memory snapshot and, when
that snapshot is older than :data:`_CACHE_TTL_SECS`, hands ONE refresh to a
daemon thread (:func:`_refresh_from_disk`, a ``stat`` plus, on change, a small
read). A slow or stalled filesystem therefore costs the caller nothing but a
verdict that is up to a second stale -- in the ON direction until the first
refresh completes, since the snapshot starts at ON. The owner-gated writer
(:func:`set_enabled`) refreshes the snapshot synchronously in its own thread,
so the gateway that took the click sees the new position at once; other
processes (the stdio MCP servers) converge within the TTL.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Whether the current (async task / thread) context is rendering for the
#: OWNER'S OWN VIEW. Only inside this scope is the switch consulted at all. A
#: ContextVar, not a global: an owner-view render on one task must not leak the
#: bypass into a Slack post the same gateway is making on another.
_OWNER_VIEW: ContextVar[bool] = ContextVar("kirocrew_redaction_owner_view", default=False)

#: The switch verdict SNAPSHOTTED when the scope was entered. One render must
#: see one verdict: the display cache decides whether an output may be memoised
#: from the same predicate the credential pass reads, and if the owner flipped
#: the switch between those two reads, a raw output could be cached as clean.
#: Reading the switch once per scope makes every call inside it agree.
_OWNER_VIEW_BYPASS: ContextVar[bool] = ContextVar(
    "kirocrew_redaction_owner_view_bypass", default=False
)


@contextmanager
def owner_view() -> Iterator[None]:
    """Mark the enclosed redaction calls as owner-view rendering.

    Enter this ONLY at a seam whose audience is the dashboard owner reading their
    own disk, after the requester has been verified as the owner. Inside it,
    ``redact_credentials`` returns its input unchanged when the owner has switched
    credential redaction OFF; outside it the switch is never read. The verdict is
    read ONCE, on entry, and held for the scope's lifetime (see
    :data:`_OWNER_VIEW_BYPASS`). Re-entrant and task-local.
    """
    token = _OWNER_VIEW.set(True)
    bypass_token = _OWNER_VIEW_BYPASS.set(not credential_redaction_enabled())
    try:
        yield
    finally:
        _OWNER_VIEW_BYPASS.reset(bypass_token)
        _OWNER_VIEW.reset(token)


def owner_view_active() -> bool:
    """Whether the calling context is inside an :func:`owner_view` scope."""
    return _OWNER_VIEW.get()


def credential_pass_bypassed() -> bool:
    """Whether ``redact_credentials`` should stand down for THIS call.

    True only inside an :func:`owner_view` scope whose entry-time snapshot of the
    switch was OFF. Constant for the whole scope by construction: a flip of the
    switch during a render is seen by the NEXT render, never half-way through
    this one. Outside any scope this is always False and reads nothing.
    """
    return _OWNER_VIEW_BYPASS.get()


#: How long a cached verdict is trusted before a refresh is scheduled.
_CACHE_TTL_SECS = 1.0

#: How long an OFF snapshot may go WITHOUT a successful refresh before the
#: verdict falls back to ON. A background refresh normally lands within
#: milliseconds of the TTL; one that has not landed after this many seconds is
#: a stalled filesystem or a dead thread, and "OFF forever because the disk
#: stopped answering" is the wrong fail direction for an authorization whose
#: default is ON. ON is never aged out: it is the default, so staleness in that
#: direction loses nothing.
_STALE_OFF_GRACE_SECS = 5.0

_CACHE_LOCK = threading.Lock()
_cached_enabled: bool = True
_cached_stat: tuple[int, int] | None = None
_cached_at: float = 0.0
#: Bumped by every :func:`set_enabled` write, read at the START of every refresh
#: and compared at PUBLISH time: a refresh that began before a write and read
#: the old bytes must not publish them over the write's own synchronous refresh.
_write_generation: int = 0

#: Serialises the read-modify-write in :func:`set_enabled`. An in-process lock,
#: for the same reason ``file_delivery_consent._STORE_LOCK`` gives: the store has
#: exactly one writer (the owner-gated dashboard handler in the gateway process),
#: and a sibling lock FILE would be an agent-reachable artifact that could block
#: the owner from turning redaction back ON.
_STORE_LOCK = threading.Lock()


@dataclass(frozen=True)
class RedactionState:
    """The recorded switch position and when it was last changed."""

    enabled: bool
    changed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "changed_at": self.changed_at}


def _path():
    # Deferred: this module is imported by ``security.redaction`` at package
    # load, and ``config.loader`` must not be pulled onto that path.
    from kiro_crew.config.loader import credential_redaction_path

    return credential_redaction_path()


def _parse(raw: object) -> RedactionState:
    """Interpret a decoded store; anything not positively ``false`` is ON."""
    if not isinstance(raw, dict):
        return RedactionState(enabled=True, changed_at="")
    enabled = raw.get("enabled")
    if enabled is not False:
        return RedactionState(enabled=True, changed_at=str(raw.get("changed_at", "")))
    return RedactionState(enabled=False, changed_at=str(raw.get("changed_at", "")))


def read_state() -> RedactionState:
    """The recorded switch, read fresh from disk. Fails soft to ENABLED."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RedactionState(enabled=True, changed_at="")
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError AND UnicodeDecodeError (non-UTF-8 bytes).
        logger.warning("credential-redaction switch is unreadable; redaction stays ON")
        return RedactionState(enabled=True, changed_at="")
    return _parse(raw)


def _refresh_from_disk() -> None:
    """Re-read the keystone if its ``stat`` changed; runs OFF the event loop.

    Generation-fenced: the write generation is sampled before any I/O and the
    result is published only if no :func:`set_enabled` landed in between, so a
    slow refresh cannot resurrect the bytes a write just replaced.
    """
    global _cached_enabled, _cached_stat, _cached_at
    with _CACHE_LOCK:
        started_gen = _write_generation
        known_stat = _cached_stat
        known_enabled = _cached_enabled
    try:
        st = os.stat(_path())
        stat_key: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        stat_key = None
    except OSError:
        # An unreadable path is not a disabled switch.
        with _CACHE_LOCK:
            if _write_generation == started_gen:
                _cached_enabled, _cached_stat, _cached_at = True, None, time.monotonic()
        return
    enabled: bool | None = None
    # The stat shortcut is taken only while the cached verdict is ON. An OFF
    # verdict is re-established by an actual READ every time: a ``chmod 000``
    # changes neither mtime nor size, so "metadata unchanged" cannot be allowed
    # to re-confirm an authorization that has become unreadable.
    if stat_key != known_stat or not known_enabled:
        # A missing file and the initial cache share ``None`` on purpose: both
        # mean "no record", and the default verdict is already ON.
        enabled = read_state().enabled if stat_key is not None else True
    with _CACHE_LOCK:
        if _write_generation != started_gen:
            return  # a write landed meanwhile; its own refresh is authoritative
        if enabled is not None:
            _cached_enabled, _cached_stat = enabled, stat_key
        _cached_at = time.monotonic()


_REFRESH_IN_FLIGHT = threading.Lock()


def _refresh_in_background() -> None:
    """Run one :func:`_refresh_from_disk` on a daemon thread, never two at once."""
    if not _REFRESH_IN_FLIGHT.acquire(blocking=False):
        return

    def _run() -> None:
        try:
            _refresh_from_disk()
        finally:
            _REFRESH_IN_FLIGHT.release()

    try:
        threading.Thread(target=_run, name="credential-redaction-refresh", daemon=True).start()
    except RuntimeError:
        # Interpreter shutting down: nothing to refresh for.
        _REFRESH_IN_FLIGHT.release()


def credential_redaction_enabled() -> bool:
    """Whether ``redact_credentials`` should redact inside an owner-view scope.

    Non-blocking: answers from the in-memory snapshot and schedules a background
    refresh when the snapshot is stale (see the module docstring, *Read cost*).
    """
    with _CACHE_LOCK:
        enabled = _cached_enabled
        age = time.monotonic() - _cached_at
    if age >= _CACHE_TTL_SECS:
        _refresh_in_background()
    if not enabled and age >= _STALE_OFF_GRACE_SECS:
        # An OFF that no refresh has re-confirmed for this long is not trusted:
        # fail toward the default until the disk answers again.
        return True
    return enabled


def refresh_now() -> bool:
    """Synchronously re-read the keystone and return the verdict. OFF-loop callers only."""
    _refresh_from_disk()
    with _CACHE_LOCK:
        return _cached_enabled


def invalidate_cache() -> None:
    """Drop the cached verdict so the next refresh hits the disk.

    The verdict itself is reset to ON as well: a cleared cache holds no
    knowledge, and "no record" reads as enabled, so a file that turns out to be
    absent on the next read must not resurrect a stale OFF.
    """
    global _cached_at, _cached_stat, _cached_enabled, _write_generation
    with _CACHE_LOCK:
        _cached_at = 0.0
        _cached_stat = None
        _cached_enabled = True
        _write_generation += 1  # discard any refresh already in flight


def set_enabled(enabled: bool, *, changed_at: str) -> RedactionState:
    """Persist the owner's switch position and refresh the cache.

    Fail-loud lockdown BEFORE any content lands, as the sibling keystone stores
    do: ``restrict_to_owner=True`` applies the owner-only mode to the temp file
    before the payload reaches it, and the default ``restrict_on_error="raise"``
    refuses to write a record it cannot protect.
    """
    from kiro_crew.atomic_write import atomic_write

    state = RedactionState(enabled=bool(enabled), changed_at=changed_at)
    with _STORE_LOCK:
        atomic_write(
            _path(),
            json.dumps(state.to_dict(), indent=2, sort_keys=True),
            restrict_to_owner=True,
        )
        # The writer already runs off the loop (the handler hands it to a
        # thread), so it can afford the synchronous re-read that makes the new
        # position visible to this process at once. ``invalidate_cache`` bumps
        # the write generation, so a background refresh that read the OLD bytes
        # before this write cannot publish over what this re-read publishes.
        invalidate_cache()
        _refresh_from_disk()
    return state
