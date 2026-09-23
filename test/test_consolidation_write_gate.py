"""The consolidation write gate: one path for every durable write, pinned structurally.

A consolidation pass resolves its target's memory mode before its snapshot and then
spends minutes in model calls and offloaded writes. Four review heads in a row found
the same window one call further along -- pass start, then each write boundary, then
the resolver's own awaited header read, then inside a batch -- because each fix
guarded the site it was written for and no other. ``_WriteGate`` ends that by
putting the check INTO the write path: every durable write is a verb of the gate,
every batch of writes is dispatched through it, and the mode is re-read at the
moment of writing (``restricted_in_memory`` per mutation, the full resolution per
dispatch).

The first class here is the invariant itself, read off the module's syntax tree
rather than off a list of sites: the gate's verbs are the inventory, no verb is
called anywhere else, every verb admits before it writes, every dispatcher resolves
before it dispatches, the write batches are dispatched only through the gate, and
outside the gate a store is only ever read. The remaining tests are the behaviour
that inventory buys: a tightening between two mutations of one batch, inside a
member transaction, or between two skill writes stops the next one and leaves
nothing partial behind.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_history_consolidation_retry import KEY, _make_consolidator, _seed_log

import kiro_crew.history_consolidation as hc
from kiro_crew import history as history_mod
from kiro_crew.history_consolidation import (
    _CONSOLIDATION_REFUSED,
    TARGET_SOURCE_EXECUTION,
    TARGET_SOURCE_HEADER,
    TARGET_SOURCE_SESSION_MAP,
    HistoryConsolidator,
    RestrictedTarget,
    _ModeTightened,
    _WriteGate,
)

# ── the structural pin ───────────────────────────────────────────────────────

MODULE = Path(hc.__file__)
GATE = "_WriteGate"
#: The gate's methods that are not write verbs: the two tiers and the dispatchers.
TIERS = frozenset({"admit", "boundary", "run", "run_in_thread"})
#: The batch helpers: functions that perform several writes; dispatched only by the gate.
BATCH_HELPERS = frozenset({"_write_structured_memory", "_save_lessons", "_process_auto_skills"})
#: A receiver whose attributes are a store's: the transcript log, the Markdown memory,
#: the vector store, the lesson store, the skills loader -- by any of the names the
#: module gives them (``self._log``, ``vector_store``, ``lessons_store``, ``loader`` ...).
STORE_RECEIVER = re.compile(
    r"(^|\.)_?(log|memory|store|loader|lesson_store|lessons_store|vector_store|skills_loader)$"
)
#: What a store may be asked OUTSIDE the gate: reads, plus three writes that are not
#: memory writes and carry nothing of the session -- named so the list explains itself.
STORE_READS = frozenset(
    {
        # ConversationLog: the transcript, read for the pass.
        "get_metadata",
        "snapshot_for_consolidation",
        "_read_messages",
        "_read_metadata",
        "consolidation_counts",
        "unconsolidated_count",
        "consolidation_retry_state",
        # ConversationLog: the pass's own retry accounting, written to the
        # transcript's metadata line -- bookkeeping ABOUT the span, not a memory
        # write, and the span it charges was never extracted.
        "record_consolidation_failure",
        "record_consolidation_environment_failure",
        # MemoryStore / VectorMemoryStore: what the prompt is built from.
        "read_preferences",
        "read_projects",
        "get_all_semantic",
        "with_record_metadata",
        "consolidation_receipt",
        # SkillsLoader: the dedupe judge's view of the skill set.
        "list_auto_skills",
        "list_pending_skills",
        "find_similar",
        "get_auto_skill_version",
        "read_auto_skill_body",
        "is_auto_generated",
        # SkillsLoader: skill-set housekeeping -- archives auto-skills by age;
        # carries nothing from this session and runs on an hourly throttle.
        "run_skill_lifecycle",
    }
)
#: One write verb reached outside the gate, by design: the abandon marker. At the
#: attempt cap ``_note_failed_attempt`` marks the span consolidated WITHOUT a memory
#: pass -- nothing of the span was or will be extracted, so a tightened mode has
#: nothing to stop there, and a refusal raised inside a failure handler would only
#: mask the failure being recorded.
TRANSCRIPT_BOOKKEEPING = frozenset({("_note_failed_attempt", "mark_consolidated")})


def _parse(source: str) -> tuple[ast.Module, dict[ast.AST, ast.AST]]:
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return tree, parents


def _module() -> tuple[ast.Module, dict[ast.AST, ast.AST]]:
    return _parse(MODULE.read_text(encoding="utf-8"))


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} is not defined at module scope")


def _methods(cls: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _is_property(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(isinstance(d, ast.Name) and d.id == "property" for d in fn.decorator_list)


def write_verbs(tree: ast.Module) -> set[str]:
    """The gate's inventory: its public methods that are neither a tier nor a dispatcher."""
    gate = _class(tree, GATE)
    return {
        fn.name
        for fn in _methods(gate)
        if not fn.name.startswith("_") and fn.name not in TIERS and not _is_property(fn)
    }


def _enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST], kind: type) -> ast.AST | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, kind):
            return node
    return None


def _dotted(node: ast.AST) -> str | None:
    """``self._log`` / ``vector_store`` as a dotted name; ``None`` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return None if head is None else f"{head}.{node.attr}"
    return None


def _body(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # the docstring
    return body


def _self_call(stmt: ast.stmt, attr: str, *, awaited: bool) -> bool:
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if awaited:
        if not isinstance(call, ast.Await):
            return False
        call = call.value
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and call.func.attr == attr
    )


def _attribute_uses(tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> Iterator[ast.Attribute]:
    """Every attribute that is CALLED, or HANDED to a call as an argument."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and (parent.func is node or node in parent.args):
            yield node


def verbs_outside_the_gate(tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> list[str]:
    """Every write verb called or handed to a call outside ``_WriteGate``, as ``func:line``.

    A verb reached THROUGH the gate -- ``gate.append_history`` handed to
    ``gate.run`` -- is the gate's own surface and is not a violation; the gate object
    is the one receiver the module names ``gate``.
    """
    verbs = write_verbs(tree)
    out = []
    for node in _attribute_uses(tree, parents):
        if node.attr not in verbs:
            continue
        cls = _enclosing(node, parents, ast.ClassDef)
        if isinstance(cls, ast.ClassDef) and cls.name == GATE:
            continue
        if isinstance(node.value, ast.Name) and node.value.id == "gate":
            continue
        fn = _enclosing(node, parents, (ast.FunctionDef, ast.AsyncFunctionDef))
        fn_name = fn.name if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
        if (fn_name, node.attr) in TRANSCRIPT_BOOKKEEPING:
            continue
        out.append(f"{fn_name}:{node.lineno} {_dotted(node)}")
    return out


def store_writes_outside_the_gate(tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> list[str]:
    """Every call on a store receiver outside the gate that is not an allowlisted read."""
    out = []
    for node in _attribute_uses(tree, parents):
        receiver = _dotted(node.value)
        if receiver is None or not STORE_RECEIVER.search(receiver):
            continue
        cls = _enclosing(node, parents, ast.ClassDef)
        if isinstance(cls, ast.ClassDef) and cls.name == GATE:
            continue
        if node.attr in STORE_READS:
            continue
        fn = _enclosing(node, parents, (ast.FunctionDef, ast.AsyncFunctionDef))
        fn_name = fn.name if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
        if (fn_name, node.attr) in TRANSCRIPT_BOOKKEEPING:
            continue
        out.append(f"{fn_name}:{node.lineno} {receiver}.{node.attr}")
    return out


def batches_dispatched_outside_the_gate(
    tree: ast.Module, parents: dict[ast.AST, ast.AST]
) -> list[str]:
    """Every executor dispatch of a batch helper that is not the gate's own."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        callee = _dotted(node.func)
        if callee not in ("run_in_embed_pool", "asyncio.to_thread"):
            continue
        cls = _enclosing(node, parents, ast.ClassDef)
        if isinstance(cls, ast.ClassDef) and cls.name == GATE:
            continue
        first = node.args[0]
        if isinstance(first, ast.Attribute) and first.attr in BATCH_HELPERS:
            out.append(f"{node.lineno} {callee}({_dotted(first)})")
    return out


class TestEveryDurableWriteGoesThroughTheGate:
    """The invariant, read off the module's syntax tree.

    The question a reviewer asks of a check-per-site design is "which write did
    you miss?". These tests answer it structurally: there is no write outside the
    gate to miss, and a new one -- a store verb called from a helper, a batch
    handed to ``run_in_embed_pool`` directly, a verb that forgets to admit -- fails
    here before it reaches a reviewer.
    """

    def test_the_inventory_is_the_stores_own_write_verbs(self):
        tree, _ = _module()
        assert write_verbs(tree) == {
            "set_semantic",
            "propose_semantic_delete",
            "delete_semantic",
            "write_episodic",
            "write_lesson",
            "save",
            "append_history",
            "write_preferences",
            "write_projects",
            "stage_skill_candidate",
            "create_auto_skill",
            "update_auto_skill",
            "mark_consolidated",
            "apply_consolidation",
        }, "a verb was added or removed: update the inventory here AND check its callers"

    def test_every_verb_admits_first_and_writes_exactly_its_own_store_method(self):
        tree, parents = _module()
        gate = _class(tree, GATE)
        for fn in _methods(gate):
            if fn.name not in write_verbs(tree):
                continue
            body = _body(fn)
            assert body and _self_call(
                body[0], "admit", awaited=False
            ), f"{GATE}.{fn.name} does not admit as its first statement"
            delegates = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == fn.name
                and not (isinstance(n.func.value, ast.Name) and n.func.value.id == "self")
            ]
            assert len(delegates) == 1, f"{GATE}.{fn.name} must delegate to exactly one {fn.name}"
            # The store is the positional-only parameter after self, so the verb
            # cannot drift from the signature it fronts.
            assert [a.arg for a in fn.args.posonlyargs] == [
                "self",
                fn.args.posonlyargs[-1].arg,
            ] and (
                len(fn.args.posonlyargs) == 2
            ), f"{GATE}.{fn.name} takes the store as its one positional-only parameter"
            assert (
                fn.args.vararg is not None and fn.args.kwarg is not None
            ), f"{GATE}.{fn.name} passes the store method's own arguments through"

    def test_both_dispatchers_resolve_before_they_dispatch(self):
        tree, _ = _module()
        gate = _class(tree, GATE)
        dispatchers = {fn.name: fn for fn in _methods(gate) if fn.name in ("run", "run_in_thread")}
        assert set(dispatchers) == {"run", "run_in_thread"}
        for name, fn in dispatchers.items():
            body = _body(fn)
            assert body and _self_call(
                body[0], "boundary", awaited=True
            ), f"{GATE}.{name} does not resolve at the boundary as its first statement"

    def test_no_write_verb_is_called_or_dispatched_outside_the_gate(self):
        tree, parents = _module()
        assert verbs_outside_the_gate(tree, parents) == []

    def test_outside_the_gate_a_store_is_only_read(self):
        tree, parents = _module()
        assert store_writes_outside_the_gate(tree, parents) == [], (
            "a store method that is not an allowlisted read is called outside the gate; "
            "a WRITE belongs on the gate, a READ belongs in STORE_READS with its reason"
        )

    def test_write_batches_are_dispatched_only_through_the_gate(self):
        tree, parents = _module()
        assert batches_dispatched_outside_the_gate(tree, parents) == []

    def test_every_helper_that_writes_takes_the_gate_with_no_default(self):
        """No ungated default: a helper cannot be called without the pass's gate."""
        for name in (
            "_write_structured_memory",
            "_save_lessons",
            "_process_auto_skills",
            "_stage_skill_update",
            "_run_skill_detection",
        ):
            params = inspect.signature(getattr(HistoryConsolidator, name)).parameters
            assert "gate" in params, f"{name} does not take the gate"
            assert params["gate"].default is inspect.Parameter.empty, f"{name} defaults its gate"

    def test_the_one_bookkeeping_exception_is_the_abandon_marker_only(self):
        """The allowlist above is one entry, and the module still matches it exactly."""
        tree, parents = _module()
        reached: set[tuple[str, str]] = set()
        for node in _attribute_uses(tree, parents):
            if node.attr != "mark_consolidated":
                continue
            if isinstance(node.value, ast.Name) and node.value.id == "gate":
                continue
            cls = _enclosing(node, parents, ast.ClassDef)
            if isinstance(cls, ast.ClassDef) and cls.name == GATE:
                continue
            fn = _enclosing(node, parents, (ast.FunctionDef, ast.AsyncFunctionDef))
            assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            reached.add((fn.name, node.attr))
        assert reached == set(TRANSCRIPT_BOOKKEEPING)


class TestThePinSeesABypass:
    """The pin is only worth its name if a bypass fails it: three mutations, each red."""

    @staticmethod
    def _mutated(old: str, new: str) -> tuple[ast.Module, dict[ast.AST, ast.AST]]:
        source = MODULE.read_text(encoding="utf-8")
        assert source.count(old) == 1, f"mutation anchor not unique: {old!r}"
        return _parse(source.replace(old, new))

    def test_a_store_verb_called_directly_from_a_helper(self):
        tree, parents = self._mutated(
            "ep_ok = gate.write_episodic(\n                        vector_store,",
            "ep_ok = vector_store.write_episodic(",
        )
        found = verbs_outside_the_gate(tree, parents)
        assert found and all("vector_store.write_episodic" in f for f in found), found
        assert store_writes_outside_the_gate(tree, parents)

    def test_a_batch_handed_to_the_executor_directly(self):
        tree, parents = self._mutated(
            'await gate.run(\n                    "the lesson writes",\n                    self._save_lessons,',
            "await run_in_embed_pool(\n                    self._save_lessons,",
        )
        found = batches_dispatched_outside_the_gate(tree, parents)
        assert len(found) == 1 and found[0].endswith(
            " run_in_embed_pool(self._save_lessons)"
        ), found

    def test_a_verb_that_forgets_to_admit(self):
        tree, _ = self._mutated(
            'self.admit("the history write")\n        return memory.append_history',
            "return memory.append_history",
        )
        gate = _class(tree, GATE)
        fn = next(f for f in _methods(gate) if f.name == "append_history")
        body = _body(fn)
        assert not (body and _self_call(body[0], "admit", awaited=False))


# ── the tier split ───────────────────────────────────────────────────────────


def _channel_thread(tmp_path, monkeypatch):
    """A channel thread with a real session map: the records the modifier writes."""
    from kiro_crew.session_map import SessionMap

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    live_key = "telegram:kirocrew:direct:4242"
    log = _seed_log(tmp_path, key=live_key)
    sm = SessionMap()
    sessions = SimpleNamespace(_session_map=sm, channel_key_for_stem=sm.channel_key_for_stem)
    return live_key, log, sm, sessions


def _sel_events(monkeypatch) -> list[dict]:
    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(history_mod, "sel", lambda: fake)
    return events


class TestTheTwoTiers:
    """``admit`` reads the in-memory records, any thread; ``boundary`` reads the header too."""

    def test_admit_sees_the_live_execution_registry(self, tmp_path, monkeypatch):
        from kiro_crew import execution_context

        live = execution_context.ExecutionContext(
            None, execution_context.MemoryStoreRef("default"), "template", "kirocrew", "incognito"
        )
        monkeypatch.setattr(execution_context, "read_live_session_execution", lambda key: live)
        gate = _make_consolidator(_seed_log(tmp_path))._write_gate(KEY)
        with pytest.raises(_ModeTightened) as raised:
            gate.admit("a write")
        assert (raised.value.mode, raised.value.source, raised.value.site) == (
            "incognito",
            TARGET_SOURCE_EXECUTION,
            "a write",
        )

    @pytest.mark.parametrize("record", ["tracker", "map-flag"])
    def test_admit_sees_a_channel_threads_tracker_and_map_flag_without_marking(
        self, tmp_path, monkeypatch, record
    ):
        from kiro_crew.messaging import privacy_mode

        live_key, log, sm, sessions = _channel_thread(tmp_path, monkeypatch)
        gate = _make_consolidator(log, sessions=sessions)._write_gate(live_key)
        privacy_mode.reset()
        try:
            gate.admit("a write")  # unflagged: admitted
            if record == "tracker":
                privacy_mode.mark_temporary(live_key)
            else:
                sm.set_flag(live_key, "temporary", True)
            with pytest.raises(_ModeTightened) as raised:
                gate.admit("a write")
            assert (raised.value.mode, raised.value.source) == (
                "temporary",
                TARGET_SOURCE_SESSION_MAP,
            )
            # Read-only: a map flag is not copied into the tracker by the worker.
            assert privacy_mode.is_temporary(live_key) is (record == "tracker")
        finally:
            privacy_mode.reset()

    @pytest.mark.asyncio
    async def test_a_header_only_mode_is_the_boundarys_to_see(self, tmp_path):
        """The tier split, pinned: the header is a file read, so ``admit`` does not
        pay it; ``boundary`` does, and refuses on it."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"memory_mode": "temporary"})
        gate = _make_consolidator(log)._write_gate(KEY)
        gate.admit("a write")  # the in-memory records say nothing
        with pytest.raises(_ModeTightened) as raised:
            await gate.boundary("a batch")
        assert (raised.value.mode, raised.value.source) == ("temporary", TARGET_SOURCE_HEADER)

    def test_a_refused_verb_never_reaches_the_store(self, tmp_path, monkeypatch):
        from kiro_crew import execution_context

        live = execution_context.ExecutionContext(
            None, execution_context.MemoryStoreRef("default"), "template", "kirocrew", "temporary"
        )
        monkeypatch.setattr(execution_context, "read_live_session_execution", lambda key: live)
        gate = _make_consolidator(_seed_log(tmp_path))._write_gate(KEY)
        store = MagicMock()
        with pytest.raises(_ModeTightened):
            gate.set_semantic(store, key="pref.x", value="y", confidence=1.0, source="s")
        store.set_semantic.assert_not_called()
        assert restricted_target(gate) == RestrictedTarget("temporary", TARGET_SOURCE_EXECUTION)

    def test_admit_does_not_walk_the_map_for_a_live_channel_key(self, tmp_path, monkeypatch):
        """The stem unfold is an O(map) walk under the map lock, and the admit tier
        runs before EVERY mutation. A live ``slack:<ts>`` key can never be a stem
        (the fold turns every ``:`` into ``_``), so the walk is skipped for it and
        for a legacy bare thread ts; a real stem is still unfolded, exactly once.
        Mutation: unfold unconditionally -- the spy is called for the live key."""
        from kiro_crew.messaging import privacy_mode

        live_key, log, sm, sessions = _channel_thread(tmp_path, monkeypatch)
        walks: list[str] = []
        real_unfold = sm.channel_key_for_stem

        def _spy(stem: str) -> str:
            walks.append(stem)
            return real_unfold(stem)

        sessions.channel_key_for_stem = _spy
        gate = _make_consolidator(log, sessions=sessions)._write_gate(live_key)
        privacy_mode.reset()
        try:
            gate.admit("a write")
            assert hc.channel_thread_mode(live_key, sessions) is None
            assert hc._live_channel_key("1785861252.833429", sessions) == "1785861252.833429"
            assert walks == [], f"the map was walked for a key that cannot be a stem: {walks}"
            # A real stem still unfolds through the map, once.
            sm.set(live_key, "sid-live")
            stem = live_key.replace(":", "_")
            assert hc._live_channel_key(stem, sessions) == live_key
            assert walks == [stem]
        finally:
            privacy_mode.reset()


def restricted_target(gate: _WriteGate) -> RestrictedTarget | None:
    return hc.restricted_in_memory(gate.key, gate._consolidator._sessions)


# ── a transition landing DURING a mutation: ordering, never a clobber ────────


class TestATransitionDuringAMutationCannotClobberTheHeader:
    """The gate admits, THEN the mutation runs -- so a ``!temporary`` can land between
    the admission and the mutation's completion. Two records could in principle
    collide there: the transition's header stamp (``memory_mode``, written by
    ``update_metadata_if``) and the pass's own header write (``last_consolidated``,
    written by ``mark_consolidated``), both read-modify-writes of the transcript's
    one metadata line. These tests drive each into the OTHER's window -- the
    interloper is fired from a second thread between the first writer's read and
    its write -- and show the outcome is ordering, not loss: the interloper waits
    on the per-session lock every header writer holds across its whole
    read-modify-write, then reads the first writer's result and merges its own
    field, so both records survive whichever lands first. The pass's write stands
    (its content was admitted before the tightening; not retroactive) and the
    stamped mode is never clobbered. A lock around admit+mutation would add
    nothing these locks do not already give.
    """

    @staticmethod
    def _tighten_only(metadata: dict) -> bool:
        from kiro_crew.history import transcript_privacy_mode
        from kiro_crew.messaging.privacy_mode import strictest

        return strictest([transcript_privacy_mode(metadata.get("memory_mode")), "temporary"]) == (
            "temporary"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("in_flight", ["offset-advance", "header-stamp"])
    async def test_the_interloper_waits_for_the_lock_and_both_records_survive(
        self, tmp_path, monkeypatch, in_flight
    ):
        """``in_flight`` names the writer whose read-modify-write is in progress;
        the other is fired from a second thread between its read and its write.
        Mutation (hypothetical -- there is no such code): a header writer that
        reads outside the lock and writes inside it would let the interloper
        land between, and the first writer's stale record would erase the
        interloper's field: ``memory_mode`` gone after an offset advance, or
        ``last_consolidated`` reset after a stamp."""
        import tempfile
        import threading

        log = _seed_log(tmp_path)  # 3 messages, header without a mode
        generation = int(log.get_metadata(KEY).get("rotation_generation", 0) or 0)
        landed_inside: list[bool] = []
        interloper: list[threading.Thread] = []
        fired = threading.Event()

        def _stamp() -> None:
            # What ``_persist_transcript_mode`` runs: the transition's header write.
            log.update_metadata_if(KEY, {"memory_mode": "temporary"}, self._tighten_only)

        def _advance() -> None:
            # What the gate's ``mark_consolidated`` verb runs: the pass's header write.
            log.mark_consolidated(KEY, 3, generation)

        def _fire(target) -> None:
            # Called from INSIDE the first writer's critical section, after its
            # read and before its write. Start the interloper and give it a
            # second: if it finishes now, the lock is not held across the
            # read-modify-write and the first writer's write will clobber it.
            if fired.is_set():
                return
            fired.set()
            thread = threading.Thread(target=target, name="interloper")
            thread.start()
            thread.join(timeout=1.0)
            landed_inside.append(not thread.is_alive())
            interloper.append(thread)

        if in_flight == "offset-advance":
            # ``mark_consolidated`` stamps ``updated_at`` between its read and its
            # write; the seam is that clock.
            real_now = history_mod.metadata_now_iso

            def _now_then_stamp() -> str:
                _fire(_stamp)
                return real_now()

            monkeypatch.setattr(history_mod, "metadata_now_iso", _now_then_stamp)
            await asyncio.to_thread(_advance)
        else:
            # ``_update_metadata_locked`` opens its temp file between its read and
            # its write; the seam is ``tempfile.mkstemp``.
            real_mkstemp = tempfile.mkstemp

            def _mkstemp_then_advance(*args, **kwargs):
                _fire(_advance)
                return real_mkstemp(*args, **kwargs)

            monkeypatch.setattr(tempfile, "mkstemp", _mkstemp_then_advance)
            await asyncio.to_thread(_stamp)

        assert interloper, "premise: the seam inside the first writer's critical section fired"
        interloper[0].join(timeout=10)
        assert not interloper[0].is_alive()
        # (a) does not reproduce: the interloper could not land inside the window ...
        assert landed_inside == [False], "the interloper wrote inside another writer's RMW window"
        # ... and both records are on disk afterwards, whichever landed first.
        header = log.get_metadata(KEY)
        assert header.get("memory_mode") == "temporary", "the stamped mode was clobbered"
        assert header.get("last_consolidated") == 3, "the offset advance was clobbered"
        assert log.unconsolidated_count(KEY) == 0


# ── between two mutations of one batch ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["semantic", "episodic", "lessons"])
async def test_a_mode_tightened_between_two_rows_of_one_batch_stops_the_second(
    tmp_path, monkeypatch, tier
):
    """The modifier lands INSIDE the first row's write; the second row is never written.

    A check ahead of the batch guards its first row and no other: the rows of a
    structured-memory or lesson batch embed one at a time, seconds each, so a
    ``!incognito`` landing while the first row embeds would still have the second
    row land the thread's content. The gate re-reads the in-memory records before
    EVERY row (``admit``). The modifier's records are the map flag and the tracker,
    written before its awaited header write -- so the flag is what the first row's
    write sets here, exactly the interleaving. Mutation: the batch helpers calling
    the store directly, with no admission per row -- the second row is written.
    """
    from kiro_crew.messaging import privacy_mode

    events = _sel_events(monkeypatch)
    live_key, log, sm, sessions = _channel_thread(tmp_path, monkeypatch)
    vectors = MagicMock()
    vectors.algorithm_version = "v1"
    vectors.get_all_semantic.return_value = []
    c = _make_consolidator(log, vector_store=vectors, sessions=sessions)

    def _tighten(*_a, **_kw):
        sm.set_flag(live_key, "incognito", True)
        return None if tier == "semantic" else True

    two = [
        {"key": "pref.editor", "value": "vim", "confidence": 1.0},
        {"key": "pref.shell", "value": "zsh", "confidence": 1.0},
    ]
    if tier == "semantic":
        result = {"semantic": two}
        first = vectors.set_semantic
    elif tier == "episodic":
        result = {
            "episodic": [
                {
                    "text": "the user set up a new editor today",
                    "tags": ["setup"],
                    "importance": 0.8,
                },
                {
                    "text": "the user switched shells the same day",
                    "tags": ["setup"],
                    "importance": 0.8,
                },
            ]
        }
        first = vectors.write_episodic
    else:
        result = {"lessons": [{"rule": "always run the tests first"}, {"rule": "never force-push"}]}
        first = vectors.write_lesson
    first.side_effect = _tighten
    c._call_llm = AsyncMock(return_value=result)
    privacy_mode.reset()
    try:
        outcome = await asyncio.wait_for(c._consolidate(live_key), 10)
    finally:
        privacy_mode.reset()
    # The leak, named first: the second row must not receive the thread's content.
    first.assert_called_once()
    assert outcome is _CONSOLIDATION_REFUSED
    assert log.unconsolidated_count(live_key) == 3, "the offset advanced after the mode tightened"
    assert [e["resources"] for e in events] == [f"restricted_target_session:incognito:{live_key}"]
    assert c._restricted_refused == {live_key: "incognito"}


@pytest.mark.asyncio
async def test_a_mode_tightened_between_two_skill_writes_stops_the_second(tmp_path, monkeypatch):
    """The skill pass can write twice -- a created skill, then a refinement -- with the
    dedupe judge's model turn between them. The modifier lands inside the first write;
    the refinement is never written. Mutation: the skill writes calling the loader
    directly, with no admission per write -- the refinement lands."""
    from kiro_crew.messaging import privacy_mode

    events = _sel_events(monkeypatch)
    live_key, log, sm, sessions = _channel_thread(tmp_path, monkeypatch)
    loader = MagicMock()
    loader.list_auto_skills.return_value = []
    loader.list_pending_skills.return_value = []
    loader.find_similar.return_value = None
    loader.is_auto_generated.return_value = True
    c = _make_consolidator(log, skills_loader=loader, auto_skills_enabled=True, sessions=sessions)
    c._auto_min_tool_calls = 0
    c._approval_required = False
    c._auto_refine_enabled = True
    c._dedupe_candidate = lambda *_: (hc.VERDICT_NEW, None)  # type: ignore[method-assign]

    def _tighten(*_a, **_kw):
        sm.set_flag(live_key, "temporary", True)
        return "auto/must-be-the-only-write"

    loader.create_auto_skill.side_effect = _tighten

    async def _llm(prompt, **kw):
        if prompt.startswith("You are a skill-extraction agent."):
            return {
                "new_skill": {
                    "slug": "first-write",
                    "description": "Do the first thing",
                    "triggers": "first",
                    "procedure_md": "## Steps\n1. x",
                },
                "refined_skill": {
                    "name": "auto/second-write",
                    "description": "Refined",
                    "triggers": "second",
                    "procedure_md": "## Steps\n1. y",
                },
            }
        return {"history_entry": "written before the skill pass"}

    c._call_llm = _llm
    privacy_mode.reset()
    try:
        outcome = await asyncio.wait_for(c._consolidate(live_key), 10)
    finally:
        privacy_mode.reset()
    loader.create_auto_skill.assert_called_once()
    loader.update_auto_skill.assert_not_called()
    c._memory.append_history.assert_called_once()  # the earlier write stands
    assert outcome is _CONSOLIDATION_REFUSED
    assert log.unconsolidated_count(live_key) == 3
    assert [e["resources"] for e in events] == [f"restricted_target_session:temporary:{live_key}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("inside", ["the-first-of-two-rows", "the-last-mutation"])
async def test_a_mode_tightened_inside_the_member_transaction_rolls_it_back_whole(
    tmp_path, monkeypatch, inside
):
    """A member (V2) store publishes a span in ONE transaction: every row, the history
    line and the receipt commit together. The gate's per-mutation check is handed in
    as the store's ``admit`` hook, called before each mutation and once more before
    the commit; a tightening inside the transaction raises out of it and the
    store's own rollback discards everything, so nothing partial is committed --
    not the rows written before the tightening, not the receipt. Two placements:
    inside the first of two semantic rows (the second row's admission refuses) and
    inside the LAST mutation, the history line (the pre-commit admission refuses).
    Mutation: a store with no ``admit`` hook, resolved once ahead of the transaction
    -- the transaction commits both rows.
    """
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.memory_stores import memory_store_dir_for, provision_member_memory
    from kiro_crew.messaging import privacy_mode
    from kiro_crew.vector_memory import open_member_database

    events = _sel_events(monkeypatch)
    live_key, log, sm, sessions = _channel_thread(tmp_path, monkeypatch)
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig()
    store_name = provision_member_memory(cfg, "writer")
    cfg.save()
    directory = memory_store_dir_for(store_name)
    vectors = open_member_database(
        directory / "memory.db", member_id=cfg.agents["writer"].member_id, store_id=store_name
    )
    try:
        c = _make_consolidator(log, vector_store=vectors, sessions=sessions)
        monkeypatch.setattr("kiro_crew.context.store_of_session", lambda *_: store_name)
        monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=vectors))
        monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda *a, **kw: c._memory)
        monkeypatch.setattr(ContextBuilder, "get_lessons_for", lambda *a, **kw: None)
        result = {
            "history_entry": "the thread's summary",
            "semantic": [
                {"key": "pref.editor", "value": "vim", "confidence": 1.0},
                {"key": "pref.shell", "value": "zsh", "confidence": 1.0},
            ],
        }
        c._call_llm = AsyncMock(return_value=result)
        if inside == "the-first-of-two-rows":
            real = vectors._write_semantic

            def _write_then_modifier(*a, **kw):
                sm.set_flag(live_key, "incognito", True)
                return real(*a, **kw)

            monkeypatch.setattr(vectors, "_write_semantic", _write_then_modifier)
        else:
            real_append = vectors._append_history

            def _append_then_modifier(entry):
                real_append(entry)
                sm.set_flag(live_key, "incognito", True)

            monkeypatch.setattr(vectors, "_append_history", _append_then_modifier)
        privacy_mode.reset()
        try:
            outcome = await asyncio.wait_for(c._consolidate(live_key), 10)
        finally:
            privacy_mode.reset()
        # Nothing partial: neither row, no history line, no receipt.
        assert {row["key"] for row in vectors.get_all_semantic()} == set()
        assert vectors.db.execute("SELECT COUNT(*) FROM memory_consolidations").fetchone()[0] == 0
        assert vectors.db.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0] == 0
        assert outcome is _CONSOLIDATION_REFUSED
        assert log.unconsolidated_count(live_key) == 3
        assert [e["resources"] for e in events] == [
            f"restricted_target_session:incognito:{live_key}"
        ]
        assert c._restricted_refused == {live_key: "incognito"}
    finally:
        vectors.close()
