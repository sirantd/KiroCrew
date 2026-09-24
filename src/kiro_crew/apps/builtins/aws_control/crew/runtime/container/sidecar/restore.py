"""Bringing the two authority files back, before the backend can overwrite them.

## Why this runs before the backend and not beside it

``session_map.json`` and ``open_slots.json`` are what turn a slot id back into a
conversation. The backend flushes them periodically from its own in-memory state, so
a backend that starts before they are on disk starts with an empty slot table and
then PERSISTS that emptiness over the restored files. The conversation list comes up
blank, the transcripts are still on disk, and nothing reports a fault.

So the restore is not "early for speed". Finishing before the backend starts is the
correctness rule, and this function is called from the supervisor's startup order
where that is enforced, not from the sidecar, which does not exist yet at that point.

Transcripts are deliberately NOT restored here. The front fetches the one transcript
a turn continues, on that turn, which keeps the property that a task only ever holds
the conversations it has itself served. Downloading them all at boot would undo that
and would need a bucket listing, which the front's reader cannot do.

## Why the bytes are validated before they are written

Both of the backend's own readers ignore an authority file they cannot parse and
carry on with an empty result. That is right for them and wrong for this step: a
malformed object written here would be read as "no conversations" and then replaced by
the flush, so the restore would look like it worked and the customer's list would be
empty. Refusing to boot instead turns a silent loss into a message an operator gets
before the task serves a turn.

The check is exactly as strict as those readers require -- the file must be a JSON
object, and ``open_slots.json``'s ``keys`` must be a list if it is present -- and no
stricter. A schema invented here would refuse a file the backend would have accepted.

## What an existing local file means

It is kept. Nothing has started, so a file already at the path did not come from this
boot's backend: it came from a data home that outlived the task, and that copy leads
the bucket by up to one backup interval. Overwriting it would roll a conversation list
backwards. ``link_new`` makes that a filesystem guarantee rather than a check.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..common import Settings, keys, statefile
from ..common.config import MAX_OBJECT_BYTES
from .store import ObjectAbsent, ObjectStore

log = logging.getLogger("smc.sidecar.restore")

__all__ = [
    "RestoreFailed",
    "RestoreResult",
    "validate_authority",
    "restore_authority",
]


class RestoreFailed(RuntimeError):
    """The authority files could not be restored, so the task must not start.

    Fail-closed on purpose. Every alternative -- boot without them, boot with some of
    them, boot with bytes that did not parse -- ends the same way: the backend flushes
    an empty slot table over the real one and the customer's conversation list is gone
    with nothing to say so.
    """


@dataclass
class RestoreResult:
    """What the restore did, per authority file."""

    restored: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.restored)} restored, {len(self.absent)} not in the bucket, "
            f"{len(self.kept_local)} already on disk"
        )


def validate_authority(name: str, raw: bytes) -> None:
    """Refuse bytes the backend would silently ignore. Returns nothing on success.

    Raises :class:`RestoreFailed` naming the file and what was wrong with it.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RestoreFailed(
            f"{name} in the bucket is not UTF-8 text ({exc}). The backend would read it "
            "as no conversations and then replace it, so the task refuses to start "
            "rather than boot into an empty slot table."
        ) from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise RestoreFailed(
            f"{name} in the bucket is not valid JSON ({exc}). The backend would read it "
            "as no conversations and then replace it, so the task refuses to start "
            "rather than boot into an empty slot table."
        ) from exc
    if not isinstance(parsed, dict):
        raise RestoreFailed(
            f"{name} in the bucket is a JSON {type(parsed).__name__}, not an object. "
            "The backend requires an object at the top level and ignores anything else, "
            "so this would boot the task with an empty slot table."
        )
    if name == "open_slots.json" and "keys" in parsed and not isinstance(parsed["keys"], list):
        # The one field check, and it is here because the backend's reader applies
        # exactly it: a ``keys`` that is not a list yields no slots at all, which is the
        # empty-list-that-looks-restored case. An ABSENT ``keys`` is legal and means no
        # open slots, so its absence is not a fault.
        raise RestoreFailed(
            f"{name} in the bucket has a 'keys' field that is a "
            f"{type(parsed['keys']).__name__}, not a list. The backend reads that as no "
            "open slots, so the task would come up with an empty conversation list."
        )


def _write(settings: Settings, name: str, raw: bytes) -> bool:
    """Put *raw* at the authority file's local path. ``False`` if one was already there.

    The parent is checked for a symlink first, for the reason the front checks the
    sessions directory: ``mkdir(exist_ok=True)`` succeeds on a link to a directory and
    every write then lands wherever the link points, which for these two files means
    the task's whole conversation index written outside the data home.
    """
    parent: Path = settings.config_dir
    if parent.is_symlink():
        raise RestoreFailed(
            f"the config directory is a symlink: {parent}. Writing {name} through it "
            "would put this task's conversation index outside the data home."
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RestoreFailed(
            f"the config directory could not be created at {parent} ({exc}), so {name} "
            "cannot be restored."
        ) from exc
    try:
        return statefile.link_new(parent / name, raw, prefix=f".smc-restore-{name}-")
    except OSError as exc:
        raise RestoreFailed(f"{name} could not be written to {parent} ({exc}).") from exc


def _published_record(settings: Settings, store: ObjectStore) -> frozenset[str] | None:
    """The authority names a complete cycle published, or ``None`` when there is no record.

    ``None`` is the ONLY answer that lets an absent authority file be read as absence. It
    means no cycle has yet put a whole pair in the bucket, so there is nothing a boot can
    overwrite. Every other way this can go raises, including a record that cannot be read
    and a record whose bytes do not parse: unreadable is not absent, and treating it as
    absent hands back exactly the boot this function exists to gate.

    Names outside :data:`keys.AUTHORITY_NAMES` are dropped rather than refused. The record
    is this writer's own object, and a name it does not recognise is a record written by a
    newer writer against a pair this one does not have -- the recognised names still decide
    this task's boot, and refusing on the unrecognised one would strand a bucket that a
    newer task reads correctly.
    """
    key = keys.authority_record_key(settings)
    try:
        raw = store.get(key, limit=MAX_OBJECT_BYTES)
    except ObjectAbsent:
        return None
    except Exception as exc:  # noqa: BLE001 - translated, never swallowed
        raise RestoreFailed(
            f"the completeness record could not be read from the bucket ({exc}). This is "
            "not the same as it being absent: absent means no pair has been published and "
            "a boot loses nothing, while unreadable leaves the task unable to tell that "
            "from a pair that lost a member. The task refuses to start."
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RestoreFailed(
            f"the completeness record in the bucket does not parse ({exc}). Reading it as "
            "absent would let a pair that lost a member boot as a first task, so the task "
            "refuses to start."
        ) from exc
    listed = parsed.get("authority") if isinstance(parsed, dict) else None
    if not isinstance(listed, list) or not all(isinstance(name, str) for name in listed):
        raise RestoreFailed(
            "the completeness record in the bucket has no 'authority' list of names, so "
            "it cannot say which files were published together. Reading it as absent would "
            "let a pair that lost a member boot as a first task, so the task refuses to "
            "start."
        )
    return frozenset(name for name in listed if name in keys.AUTHORITY_NAMES)


def restore_authority(settings: Settings, store: ObjectStore) -> RestoreResult:
    """Fetch, validate and write both authority files. Raise if any of it fails.

    An authority file missing from the bucket is one of two different things, and the
    bucket's own shape cannot tell them apart -- both look like one object present and one
    absent. Either the crew has never published a whole pair, in which case there is
    nothing to overwrite and the task must boot; or a publication did not finish, in which
    case booting from the file that made it lets the backend flush its own empty view of
    the other over a real conversation list. Reading every partial pair as the second is
    what turns a young bucket into one no replacement task can ever boot from.

    So the decision is made on the completeness record, which the writer publishes as the
    LAST step of a cycle that put the whole pair in the bucket. Four cases, each decided
    here: no record means no complete publication is on file, so absence is absence and
    the task boots; a record naming files that are all present is an ordinary restore; a
    record naming a file the bucket does not hold is a pair that lost a member, and the
    task refuses; a record that cannot be read or does not parse refuses too, because
    unreadable is not the same as absent and this module reads neither as permission to
    boot into an empty slot table.

    A read that fails for any OTHER reason is failure too, including a denial -- reading a
    denial as absence is the same route to booting with an empty slot table.
    """
    result = RestoreResult()
    fetched: dict[str, bytes] = {}
    for name in keys.AUTHORITY_NAMES:
        key = keys.authority_key(settings, name)
        try:
            raw = store.get(key, limit=MAX_OBJECT_BYTES)
        except ObjectAbsent:
            log.info("restore: %s is not in the bucket", name)
            result.absent.append(name)
            continue
        except Exception as exc:  # noqa: BLE001 - translated, never swallowed
            raise RestoreFailed(
                f"{name} could not be read from the bucket ({exc}). This is not the same "
                "as it being absent, so the task refuses to start rather than boot into "
                "an empty slot table and flush it over the real one."
            ) from exc
        validate_authority(name, raw)
        fetched[name] = raw
    recorded = _published_record(settings, store)
    if recorded is None:
        if result.absent:
            log.info(
                "restore: no completeness record, so %s is read as never published rather "
                "than as a pair that lost a member",
                ", ".join(sorted(result.absent)),
            )
    else:
        missing = sorted(name for name in recorded if name in result.absent)
        if missing:
            raise RestoreFailed(
                f"the completeness record names {', '.join(sorted(recorded))} as published "
                f"together, but the bucket does not hold {', '.join(missing)}. A pair that "
                "lost a member is not a first boot: starting from the rest would let the "
                "backend flush its own empty view of the missing one over a real "
                "conversation list. The task refuses to start."
            )
    for name, raw in fetched.items():
        if _write(settings, name, raw):
            log.info("restore: %s restored, %d B", name, len(raw))
            result.restored.append(name)
        else:
            log.info(
                "restore: %s is already on disk; keeping the local copy, which leads the "
                "bucket by up to one backup interval",
                name,
            )
            result.kept_local.append(name)
    log.info("restore: complete -- %s", result.summary())
    return result
