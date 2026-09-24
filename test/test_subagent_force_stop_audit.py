"""The subagent force-stop path records what actually happened to the process.

Twin of the cron reaper's contract (``test_cron_reaper.py``): ``_sigkill_session``
REPORTS a refused or failed signal instead of swallowing it, its callers audit
that as ``failed`` rather than ``reaped`` / ``sigkill`` and name it in the run's
error text, and both callers act on a process handle taken BEFORE the reset --
retained under the run's id -- so a stop that arrives after the reset has popped
the session from the map, or after a completed reset that did not end the
process, still names, verifies and signals the process.

Every signal seam is stubbed (``platform_compat.kill_process_tree_async`` /
``kill_pid_async`` / ``get_process_start_id`` / ``pid_exists`` and the child
helpers in ``acp.client``), so no test here touches a real process. The kill
and the survival check are ``kiro_crew.process_identity``'s, shared with the
cron reaper; once a leader is gone they decide by the tree it led, and those
two probes -- the POSIX process-group probe and the Windows exact-tree cleanup
pin -- are pinned "gone" for this whole module (:func:`_no_tree_behind_the_leader`):
no test here builds a tree, and an unpinned ``os.killpg(number, 0)`` on a
made-up pid answers whatever the host running the suite happens to run. The
tree cases themselves are the shared module's, proven in ``test_cron_reaper.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.subagent import SubagentInfo, SubagentManager

# The start id the fake client records at spawn (``platform_compat.get_process_start_id``
# reads ``/proc/<pid>/stat`` field 22 on Linux); the kill re-reads it before signalling.
_START_ID = "4821903"
_REFUSAL = ValueError("kill_process_tree: refusing non-int/reserved pid 4242")


@pytest.fixture(autouse=True)
def _no_tree_behind_the_leader() -> Iterator[None]:
    """The leader's process group is empty and no Windows tree cleanup is pending.

    Pinned for every test: the shared kill asks a gone or recycled leader's
    tree before calling it gone, and the probes are real otherwise. A test that
    asserts on either probe patches it again inside its own ``with`` (the inner
    patch wins).
    """
    with (
        patch("kiro_crew.platform_compat.pgroup_exists", return_value=False),
        patch("kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False),
    ):
        yield


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock(return_value=True)
    sessions.tearing_down = MagicMock(return_value=None)
    sessions._sessions = {}
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


def _session_with_pid(
    manager: SubagentManager,
    session_key: str,
    pid: int | None,
    *,
    start_id: str | None = _START_ID,
    child_pids: dict[Any, Any] | None = None,
) -> MagicMock:
    """Register a session under ``session_key`` whose ACP client reports ``pid``.

    The client carries the start id the recycled-pid check compares, so a test
    whose start-id read answers the same value drives ``_sigkill_session`` all
    the way to the group kill.
    """
    client = MagicMock()
    client._pid = pid
    client._child_pids = dict(child_pids or {})
    client._start_time = start_id
    session = MagicMock()
    session.provider._client = client
    manager._sessions._sessions[session_key] = session
    return client


def _overdue_run(
    agent_id: str, *, pid: int | None = 4242, user_stopped: bool = False
) -> tuple[SubagentManager, SubagentInfo, str]:
    """A manager with one overdue run whose session process reports ``pid``."""
    manager = SubagentManager(
        sessions=_mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
        on_done=AsyncMock(),
        on_event=AsyncMock(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(
        id=agent_id,
        task="stuck task",
        parent_session_key="dashboard:test-slot",
        started=time.time() - 7200,
        user_stopped=user_stopped,
    )
    manager._agents[agent_id] = info
    manager._running_count = 1
    session_key = f"subagent:{agent_id}"
    if pid is not None:
        _session_with_pid(manager, session_key, pid)
    return manager, info, session_key


def _hanging_reset(manager: SubagentManager) -> None:
    """A reset that never returns: the caller's ``wait_for`` times out."""

    async def _hang(session_key: str, **_: Any) -> bool:
        await asyncio.sleep(999)
        return True

    manager._sessions.reset = AsyncMock(side_effect=_hang)


def _kill_path_stubs() -> tuple[Any, Any, Any, Any]:
    """The child-tree probe, the liveness probe, the start-id read and the sweep stubbed.

    Only the kill decides: the pid exists and its start id reads back as the
    recorded one, so the root is this run's and the signal is reached.
    """
    return (
        patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
        patch("kiro_crew.acp.client._kill_escaped_children"),
    )


def _audit(mock_sel: MagicMock, tool_name: str) -> dict[str, Any]:
    """The kwargs of the one SEL audit row written under ``tool_name``."""
    rows = [
        call.kwargs
        for call in mock_sel().log_tool_invocation.call_args_list
        if call.kwargs.get("tool_name") == tool_name
    ]
    assert len(rows) == 1, f"expected one {tool_name} audit row, saw {len(rows)}"
    return rows[0]


def _handle_of(manager: SubagentManager, agent_id: str) -> Any:
    """The kill handle the teardown paths take before their reset and hand to the kill."""
    handle = manager._retain_process_handle(agent_id, f"subagent:{agent_id}")
    assert handle is not None, f"no session registered for {agent_id}"
    return handle


# ── A refused or failed SIGKILL is a kill failure, not a reap ──


class TestReaperRecordsAFailedKill:
    """``reaper_force_kill`` never says ``reaped`` for a process the kill left alive.

    ``_sigkill_session`` raises nothing -- the reap must still finish the
    teardown it owns -- but it REPORTS what stopped the kill: the broadcast
    guard's refusal of the pid, or the error the kill raised. ``_force_reap``
    carries that into the run's error text (``…; kill failed: <reason>``) and
    audits ``failed``. A swallowed refusal leaves only a log line and an audit
    that reads ``reaped`` while the process keeps running.
    """

    @pytest.mark.asyncio
    async def test_a_refused_pid_is_audited_as_a_failed_kill_not_reaped(self) -> None:
        manager, info, _key = _overdue_run("refused1", pid=4242)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=_REFUSAL),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await manager._force_reap("refused1", info, 7200.0)

        # The guard refused the pid outright: nothing was safe to signal, and
        # nothing was.
        pid_kill.assert_not_awaited()
        assert (
            _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        ), "the SEL audit says the process was reaped while it is still alive"
        assert info.error.startswith("Reaped after")
        assert "; kill failed: ValueError: kill_process_tree: refusing" in info.error
        assert "4242" in info.error, "the record does not name the refused pid"
        # The run still ended for the record: done, reaped, the slot released --
        # the failure is added, not substituted.
        assert info.done and info.reaped
        assert manager._running_count == 0

    @pytest.mark.asyncio
    async def test_a_group_kill_that_raises_with_no_pid_fallback_is_a_failed_kill(self) -> None:
        """EPERM on the group and on the pid: the process is there and unsignalled."""
        manager, info, _key = _overdue_run("eperm1", pid=4343)
        manager._sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
        ):
            await manager._force_reap("eperm1", info, 7200.0)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: PermissionError: " in info.error

    @pytest.mark.asyncio
    async def test_a_kill_path_error_before_the_signal_is_a_failed_kill(self) -> None:
        """The catch-all that only logged: a probe that raises left the process unsignalled."""
        manager, info, _key = _overdue_run("probe1", pid=4444)
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch(
                "kiro_crew.acp.client._get_child_pids",
                side_effect=RuntimeError("cannot schedule new futures after shutdown"),
            ),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await manager._force_reap("probe1", info, 7200.0)

        tree_kill.assert_not_awaited()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: RuntimeError: cannot schedule" in info.error

    @pytest.mark.asyncio
    async def test_a_delivered_sigkill_is_still_audited_as_reaped(self) -> None:
        manager, info, _key = _overdue_run("killed1", pid=4545)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._force_reap("killed1", info, 7200.0)

        tree_kill.assert_awaited_once_with(4545, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_group_that_is_already_gone_is_nothing_to_kill(self) -> None:
        """ProcessLookupError on the group AND the pid: the run's process exited on its own."""
        manager, info, _key = _overdue_run("gone1", pid=4646)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
        ):
            await manager._force_reap("gone1", info, 7200.0)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_user_stop_whose_kill_failed_stays_stopped_but_names_the_failure(
        self,
    ) -> None:
        """A neutral stop keeps its outcome; the process it left alive is still on the record."""
        manager, info, _key = _overdue_run("stop1", pid=4747, user_stopped=True)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=_REFUSAL),
            ),
        ):
            await manager._force_reap("stop1", info, 60.0, reason="user_stop")

        assert info.outcome == "stopped", "the failed kill must not turn a user stop into a failure"
        assert info.error == (
            "kill failed: ValueError: kill_process_tree: refusing non-int/reserved pid 4242"
        )
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"


# ── The kill acts on a handle taken before the reset ──


class TestForceStopActsAfterAResetHang:
    """The reset pops the session from the map before it can hang; the kill must not need it.

    ``SessionLifecycle.reset`` removes the map entry under its lock and only then
    awaits the shutdown that can hang. Without a handle taken before the reset,
    a fallback that looks the key up finds nothing and the run is audited
    ``reaped`` while its process keeps running.
    """

    @pytest.mark.asyncio
    async def test_a_reset_that_hangs_after_popping_the_session_still_gets_the_kill(
        self,
    ) -> None:
        manager, info, key = _overdue_run("popped1", pid=5151)

        async def _pop_then_hang(session_key: str, **_: Any) -> bool:
            manager._sessions._sessions.pop(session_key, None)
            raise asyncio.TimeoutError

        manager._sessions.reset = AsyncMock(side_effect=_pop_then_hang)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._force_reap("popped1", info, 7200.0)

        assert key not in manager._sessions._sessions, "the fixture did not pop the session"
        tree_kill.assert_awaited_once_with(5151, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_successor_under_the_key_is_not_the_run_s_process(self) -> None:
        """Only the pre-reset handle names the process; a session in the map now is a successor.

        The reset pops the run's session and awaits; a cold start (a queued turn,
        a continuation) can register a NEW session under the same key in that
        window. Reading the map at kill time would signal that successor and
        leave the run's own, hung process alive -- recorded reaped.
        """
        manager, info, key = _overdue_run("succ1", pid=5252)

        async def _pop_register_successor_then_hang(session_key: str, **_: Any) -> bool:
            manager._sessions._sessions.pop(session_key, None)
            _session_with_pid(manager, session_key, 8080)
            raise asyncio.TimeoutError

        manager._sessions.reset = AsyncMock(side_effect=_pop_register_successor_then_hang)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._force_reap("succ1", info, 7200.0)

        assert manager._sessions._sessions[key].provider._client._pid == 8080
        tree_kill.assert_awaited_once_with(5252, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_the_root_is_verified_by_its_recorded_start_id(self) -> None:
        """A root whose live start id matches the one the client recorded is ours: killed.

        The shared child verifier denies a pid with no recorded basename, and the
        root has none, so validating the root through it never let a real kill
        through. The root is compared by start id, the recycling detector itself.
        """
        manager, _info, key = _overdue_run("root1", pid=5353)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.acp.client._is_our_child", return_value=False) as child_verifier,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "root1")) is None

        child_verifier.assert_not_called()
        tree_kill.assert_awaited_once_with(5353, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_a_start_id_mismatch_never_signals(self) -> None:
        """A live start id that differs from the recorded one means another process owns the pid."""
        manager, _info, key = _overdue_run("recycled1", pid=5454)
        manager._sessions._sessions[key].provider._client._child_pids = {6161: ("111", b"node")}

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7171]) as probe,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "recycled1")) is None

        tree_kill.assert_not_awaited()
        pid_kill.assert_not_awaited()
        # Nothing is read through a pid that is not this run's any more; only
        # the children recorded before the reset are swept.
        probe.assert_not_called()
        sweep.assert_called_once_with({6161: ("111", b"node")})

    @pytest.mark.asyncio
    async def test_a_root_that_exits_during_the_child_walk_is_not_signalled(self) -> None:
        """The start id is re-read immediately before the signal; a changed reading is a gone root."""
        manager, _info, key = _overdue_run("walk1", pid=5555)
        manager._sessions._sessions[key].provider._client._child_pids = {6262: ("222", b"node")}

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7272]),
            patch(
                "kiro_crew.acp.client._capture_child_records", return_value={7272: ("333", b"x")}
            ),
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=[_START_ID, "9999999"],
            ),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "walk1")) is None

        tree_kill.assert_not_awaited()
        sweep.assert_called_once_with({6262: ("222", b"node")})

    @pytest.mark.asyncio
    async def test_a_root_gone_at_the_signal_is_decided_by_its_tree_on_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root that exits during the walk is nothing to kill once its tree is shown gone.

        On Windows a start id still reads back for an exited process while any
        handle to it is open, so the pre-signal identity read passes and the
        tree kill reaches ``taskkill``, which answers rc 128 (not found --
        ``ProcessLookupError``). The leader's absence hands the decision to its
        tree: with no exact-tree cleanup pin pending for the root there is
        nothing left to reach, the pid-scoped fallback is not attempted, and
        the kill reports no failure.
        """
        manager, _info, key = _overdue_run("walk2", pid=5757)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        not_found = ProcessLookupError('[taskkill rc=128] ERROR: The process "5757" not found.')

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch(
                "kiro_crew.platform_compat.windows_tree_cleanup_pending", return_value=False
            ) as pin,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=not_found),
            ) as tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "walk2")) is None

        tree_kill.assert_awaited_once_with(5757, platform_compat.SIGKILL)
        pin.assert_called_once_with(5757, _START_ID)
        pid_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_live_pid_whose_identity_cannot_be_confirmed_is_a_failed_kill(self) -> None:
        """No readable start id behind a pid that exists: not signalled, and not gone either."""
        manager, _info, key = _overdue_run("unverified1", pid=5656)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            with patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID):
                handle = _handle_of(manager, "unverified1")
            failure = await manager._sigkill_session(key, handle)

        tree_kill.assert_not_awaited()
        assert failure == "pid 5656 is alive but could not be verified as this run's; not signalled"

    @pytest.mark.asyncio
    async def test_no_handle_and_no_usable_pid_are_nothing_to_kill(self) -> None:
        """A session with no recorded pid names no process: no candidate, nothing to kill."""
        manager, _info, key = _overdue_run("nopid1", pid=None)
        _session_with_pid(manager, key, None)

        with patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill:
            assert manager._retain_process_handles("nopid1", key) == []
            assert manager._retain_process_handle("nopid1", key) is None
            assert await manager._sigkill_session("subagent:absent", None) is None
            assert await manager._sigkill_sessions(key, []) is None

        tree_kill.assert_not_awaited()
        assert manager._process_handles == {}


# ── The reap names the run's own session ──


class TestTheReapNamesTheRunsOwnSession:
    """A continuation lives under its conversation's key, not ``subagent:<run id>``.

    ``spawn_continue`` mints a new run id on the ORIGINAL run's conversation key
    (``info.conversation_key``), and the run's own teardown resets that key. A
    reap that derives the key from the run id resets and releases a key no
    session is under: the retain misses, the fallback has nothing to signal,
    and the audit reads ``reaped`` for a process -- and a session lease -- the
    stop never touches.
    """

    @pytest.mark.asyncio
    async def test_a_continuation_run_is_stopped_under_its_conversation_key(self) -> None:
        manager, info, _run_key = _overdue_run("cont-run", pid=None)
        info.conversation_key = "subagent:cont-orig"
        _session_with_pid(manager, "subagent:cont-orig", 7373)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._force_reap("cont-run", info, 7200.0)

        # The reset, the kill and the release all name the conversation's key.
        manager._sessions.reset.assert_awaited_once_with("subagent:cont-orig")
        tree_kill.assert_awaited_once_with(7373, platform_compat.SIGKILL)
        row = _audit(mock_sel, "reaper_force_kill")
        assert row["outcome"] == "reaped"
        assert row["session_key"] == "subagent:cont-orig"
        manager._sessions.release.assert_called_once_with("subagent:cont-orig", cleanup=False)
        assert manager._process_handles == {}


# ── A completed reset is not proof of death ──


class TestACompletedResetIsNotProofOfDeath:
    """After every completed reset the handle is asked, and a survivor gets the fallback."""

    @pytest.mark.asyncio
    async def test_a_process_that_survives_a_completed_reset_still_gets_the_kill(self) -> None:
        """A reset that returned True is not proof the process is gone.

        The reset's own shutdown can fail without raising out of it. After every
        completed reset the handle is asked -- pid plus recorded start id -- and
        a process that still stands gets the fallback; its outcome, not the
        reset's boolean, is what the record says.
        """
        manager, info, _key = _overdue_run("survived1", pid=5959)
        manager._sessions.reset = AsyncMock(return_value=True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._force_reap("survived1", info, 7200.0)

        tree_kill.assert_awaited_once_with(5959, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_survivor_the_kill_cannot_signal_is_a_failed_kill_not_reaped(self) -> None:
        """The survivor's fallback is audited on its own outcome: refused here, so ``failed``."""
        manager, info, _key = _overdue_run("survived2", pid=6060)
        manager._sessions.reset = AsyncMock(return_value=True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=_REFUSAL),
            ),
        ):
            await manager._force_reap("survived2", info, 7200.0)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: ValueError: kill_process_tree: refusing" in info.error

    @pytest.mark.asyncio
    async def test_a_process_gone_after_a_completed_reset_is_nothing_to_kill(self) -> None:
        """Control: the reset did its job -- no start id and no process behind the pid, no kill."""
        manager, info, _key = _overdue_run("gone2", pid=6161)
        manager._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await manager._force_reap("gone2", info, 7200.0)

        tree_kill.assert_not_awaited()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_an_exited_process_whose_start_id_still_reads_back_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows false survivor: identity alone said alive for a process the OS confirmed exited.

        A start id reads back for an EXITED Windows process as long as any handle
        to its kernel object is open (asyncio's Proactor transport keeps one until
        GC). The survivor check asks existence FIRST through the exit-code-confirmed
        probe (``pid_exists`` on win32), so the fallback never signals a dead pid
        and never records that error as a failed kill.
        """
        manager, info, _key = _overdue_run("exited1", pid=6363)
        manager._sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.pid_exists", return_value=False) as alive,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await manager._force_reap("exited1", info, 7200.0)

        alive.assert_called_with(6363)
        tree_kill.assert_not_awaited()
        pid_kill.assert_not_awaited()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error

    @pytest.mark.asyncio
    async def test_a_recycled_pid_after_a_completed_reset_is_nothing_to_kill(self) -> None:
        """A live pid whose start id differs from the recorded one is another process's: gone."""
        manager, info, _key = _overdue_run("recycled2", pid=6464)
        manager._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await manager._force_reap("recycled2", info, 7200.0)

        tree_kill.assert_not_awaited()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"

    @pytest.mark.asyncio
    async def test_a_surviving_pid_whose_identity_cannot_be_read_is_a_failed_kill(self) -> None:
        """Exists, identity unreadable: not proven gone, so the kill decides -- and does not signal."""
        manager, info, _key = _overdue_run("unread1", pid=6565)
        manager._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await manager._force_reap("unread1", info, 7200.0)

        tree_kill.assert_not_awaited()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: pid 6565 is alive but could not be verified" in info.error

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_still_kills_through_the_handle(self) -> None:
        """``reset`` answers False when the key is already gone: it stopped nothing.

        A concurrent reset popped the entry between the reap's handle and the
        reset's lock. Whether that reset's shutdown lands is not this reap's to
        assume: the handle says the process is standing (its recorded start id
        reads back), so the kill goes through the handle and the record says
        reaped only once it has been signalled.
        """
        manager, info, _key = _overdue_run("unmapped1", pid=6262)

        async def _already_popped(session_key: str, **_: Any) -> bool:
            manager._sessions._sessions.pop(session_key, None)
            return False

        manager._sessions.reset = AsyncMock(side_effect=_already_popped)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._force_reap("unmapped1", info, 7200.0)

        tree_kill.assert_awaited_once_with(6262, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_and_no_handle_is_nothing_to_stop(self) -> None:
        """Control: no session before the reset either -- the run had no process to answer for."""
        manager, info, _key = _overdue_run("nosess1", pid=None)
        manager._sessions.reset = AsyncMock(return_value=False)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await manager._force_reap("nosess1", info, 7200.0)

        tree_kill.assert_not_awaited()
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}


# ── The run's own finally-reset: the other caller ──


class TestTheRunsOwnTeardownRecordsTheKill:
    """``run_finally_force_kill`` follows the same rules as the reaper's audit."""

    @pytest.mark.asyncio
    async def test_a_refused_kill_in_the_runs_finally_is_audited_failed_not_sigkill(
        self,
    ) -> None:
        manager, info, key = _overdue_run("fin1", pid=7070)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=_REFUSAL),
            ),
        ):
            await manager._teardown_run_session(info, key)

        row = _audit(mock_sel, "run_finally_force_kill")
        assert row["outcome"] == "failed", "the audit says SIGKILL while the process is alive"
        assert "ValueError: kill_process_tree: refusing" in row["error"]
        assert manager._process_handles == {}, "the retained handle outlived its decision"

    @pytest.mark.asyncio
    async def test_a_delivered_kill_in_the_runs_finally_is_audited_sigkill(self) -> None:
        manager, info, key = _overdue_run("fin2", pid=7171)
        _hanging_reset(manager)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._teardown_run_session(info, key)

        tree_kill.assert_awaited_once_with(7171, platform_compat.SIGKILL)
        row = _audit(mock_sel, "run_finally_force_kill")
        assert row["outcome"] == "sigkill"
        assert row["error"] == ""

    @pytest.mark.asyncio
    async def test_a_process_that_survives_the_runs_own_reset_gets_the_fallback(self) -> None:
        manager, info, key = _overdue_run("fin3", pid=7272)
        manager._sessions.reset = AsyncMock(return_value=True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._teardown_run_session(info, key)

        tree_kill.assert_awaited_once_with(7272, platform_compat.SIGKILL)
        assert _audit(mock_sel, "run_finally_force_kill")["outcome"] == "sigkill"

    @pytest.mark.asyncio
    async def test_a_reset_that_ends_the_process_writes_no_kill_audit(self) -> None:
        """Control: the reset did its job, so there is no fallback to audit."""
        manager, info, key = _overdue_run("fin4", pid=7373)
        manager._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await manager._teardown_run_session(info, key)

        tree_kill.assert_not_awaited()
        mock_sel().log_tool_invocation.assert_not_called()
        assert manager._process_handles == {}


# ── The handle is RETAINED across the pop, for the other path ──


class TestTheRetainedHandleOutlivesThePop:
    """The common shape: the run's own ``finally`` resets, that reset hangs after
    popping the session, and the reaper (a deadline, a user Stop) then arrives.
    A handle the reaper took before ITS reset would see an empty map; the entry
    the run's teardown retained before its reset is what the reaper acts on.
    """

    @pytest.mark.asyncio
    async def test_the_reaper_acts_on_the_handle_the_runs_finally_retained(self) -> None:
        manager, info, key = _overdue_run("race1", pid=8181)
        popped = asyncio.Event()
        hang = asyncio.Event()
        resets = 0

        async def _reset(session_key: str, **_: Any) -> bool:
            nonlocal resets
            resets += 1
            if resets == 1:
                # The run's own teardown: pops the session, then hangs in the
                # provider shutdown that follows the pop.
                manager._sessions._sessions.pop(session_key, None)
                popped.set()
                await hang.wait()
                return True
            # The reaper's reset: the key is already gone, so it stops nothing.
            return False

        manager._sessions.reset = AsyncMock(side_effect=_reset)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            teardown = asyncio.create_task(manager._teardown_run_session(info, key))
            manager._tasks["race1"] = teardown
            await asyncio.wait_for(popped.wait(), timeout=5)
            assert key not in manager._sessions._sessions
            assert manager._process_handles["race1"][0].pid == 8181, "the pop site retained nothing"

            await manager._force_reap("race1", info, 7200.0)

            # The reaper cancels the run's task after its kill; the teardown's
            # own clear then finds the entry already consumed.
            with pytest.raises(asyncio.CancelledError):
                await teardown

        assert resets == 2
        tree_kill.assert_awaited_once_with(8181, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}
        assert info.done and info.reaped

    @pytest.mark.asyncio
    async def test_the_runs_finally_acts_on_the_handle_the_reaper_retained(self) -> None:
        """Roles reversed: the reaper's reset pops and hangs, the run's finally arrives."""
        manager, info, key = _overdue_run("race2", pid=8282)
        popped = asyncio.Event()
        hang = asyncio.Event()
        resets = 0

        async def _reset(session_key: str, **_: Any) -> bool:
            nonlocal resets
            resets += 1
            if resets == 1:
                manager._sessions._sessions.pop(session_key, None)
                popped.set()
                await hang.wait()
                return True
            return False

        manager._sessions.reset = AsyncMock(side_effect=_reset)
        tree_kill = AsyncMock(return_value=True)

        def _alive(pid: int) -> bool:
            # The process stands until the kill lands; then it is gone.
            return not tree_kill.await_count

        def _start_id(pid: int) -> str | None:
            return None if tree_kill.await_count else _START_ID

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.pid_exists", side_effect=_alive),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id),
            patch("kiro_crew.platform_compat.kill_process_tree_async", tree_kill),
        ):
            reap = asyncio.create_task(manager._force_reap("race2", info, 7200.0))
            await asyncio.wait_for(popped.wait(), timeout=5)
            assert manager._process_handles["race2"][0].pid == 8282

            await manager._teardown_run_session(info, key)
            tree_kill.assert_awaited_once_with(8282, platform_compat.SIGKILL)

            hang.set()
            await asyncio.wait_for(reap, timeout=5)

        # The reaper's own survivor check found the process gone: one kill, not two.
        tree_kill.assert_awaited_once()
        assert _audit(mock_sel, "run_finally_force_kill")["outcome"] == "sigkill"
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}


# ── Every candidate under a shared key, each on its own handle ──


def _finally_popped_and_hung(
    manager: SubagentManager, info: SubagentInfo, key: str
) -> tuple[asyncio.Event, asyncio.Event]:
    """The run's own ``finally`` reset has popped the session and hangs; the reaper's reset completes.

    Returns the ``popped`` event (set once the pop happened) and the ``hang``
    event the test sets to let the run's teardown finish.
    """
    popped = asyncio.Event()
    hang = asyncio.Event()
    resets = 0

    async def _reset(session_key: str, **_: Any) -> bool:
        nonlocal resets
        resets += 1
        if resets == 1:
            manager._sessions._sessions.pop(session_key, None)
            popped.set()
            await hang.wait()
            return True
        # The reaper's reset pops whatever is live under the key and returns.
        manager._sessions._sessions.pop(session_key, None)
        return True

    manager._sessions.reset = AsyncMock(side_effect=_reset)
    return popped, hang


class TestEveryCandidateUnderTheKeyIsKilledOnItsOwnHandle:
    """Two sessions can stand under one key at snapshot time: the run's own, popped
    and retained by the teardown that hangs, and a live successor a cold start
    registered under the key during that teardown's awaits. The reap ends the
    KEY -- its reset pops the successor too -- so both are candidates, each
    verified and signalled on its own handle; preferring either alone leaves
    the other's process unrecorded.
    """

    @pytest.mark.asyncio
    async def test_a_live_successor_at_snapshot_time_is_killed_alongside_the_runs_process(
        self,
    ) -> None:
        manager, info, key = _overdue_run("shared1", pid=9191)
        popped, hang = _finally_popped_and_hung(manager, info, key)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            teardown = asyncio.create_task(manager._teardown_run_session(info, key))
            manager._tasks["shared1"] = teardown
            await asyncio.wait_for(popped.wait(), timeout=5)
            # A successor registered under the key while the run's teardown hangs.
            _session_with_pid(manager, key, 9292)

            await manager._force_reap("shared1", info, 7200.0)
            with pytest.raises(asyncio.CancelledError):
                await teardown

        # The reaper's reset completed (it popped the successor) and both
        # processes survived it: both are killed, each on its own handle.
        assert sorted(call.args[0] for call in tree_kill.await_args_list) == [9191, 9292]
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert "kill failed" not in info.error
        assert manager._process_handles == {}

    @pytest.mark.asyncio
    async def test_one_refused_kill_among_several_is_named_and_the_rest_are_still_signalled(
        self,
    ) -> None:
        manager, info, key = _overdue_run("shared2", pid=9393)
        popped, hang = _finally_popped_and_hung(manager, info, key)
        children, alive, start_id, sweep = _kill_path_stubs()

        async def _kill(pid: int, sig: int) -> bool:
            if pid == 9393:
                raise ValueError(f"kill_process_tree: refusing non-int/reserved pid {pid}")
            return True

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(side_effect=_kill)
            ) as tree_kill,
        ):
            teardown = asyncio.create_task(manager._teardown_run_session(info, key))
            manager._tasks["shared2"] = teardown
            await asyncio.wait_for(popped.wait(), timeout=5)
            _session_with_pid(manager, key, 9494)

            await manager._force_reap("shared2", info, 7200.0)
            with pytest.raises(asyncio.CancelledError):
                await teardown

        assert sorted(call.args[0] for call in tree_kill.await_args_list) == [9393, 9494]
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        assert "; kill failed: ValueError: kill_process_tree: refusing" in info.error
        assert "9393" in info.error
        assert manager._process_handles == {}

    @pytest.mark.asyncio
    async def test_the_same_process_under_two_sources_is_one_handle(self) -> None:
        """The retained entry and the live session naming one pid is one candidate, one kill."""
        manager, info, key = _overdue_run("shared3", pid=9595)
        # The run's teardown retained the handle but its pop has not happened yet
        # (the session is still live under the key): same process on both sources.
        assert manager._retain_process_handle("shared3", key) is not None
        children, alive, start_id, sweep = _kill_path_stubs()
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._force_reap("shared3", info, 7200.0)

        tree_kill.assert_awaited_once_with(9595, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"

    @pytest.mark.asyncio
    async def test_a_session_torn_down_by_another_path_is_reached_through_tearing_down(
        self,
    ) -> None:
        """A reset started outside the two teardown paths still leaves the process reachable.

        Neither teardown path retained a handle (a dashboard reset or
        ``cancel_all`` popped the session), so the table and the live map are
        both empty; the session manager keeps the popped session readable
        through ``tearing_down`` for the life of its teardown, and the reap
        reads it there.
        """
        manager, info, key = _overdue_run("shared4", pid=9696)
        torn = manager._sessions._sessions.pop(key)
        manager._sessions.tearing_down = MagicMock(
            side_effect=lambda session_key: torn if session_key == key else None
        )
        children, alive, start_id, sweep = _kill_path_stubs()
        _hanging_reset(manager)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await manager._force_reap("shared4", info, 7200.0)

        tree_kill.assert_awaited_once_with(9696, platform_compat.SIGKILL)
        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "reaped"
        assert manager._process_handles == {}


# ── The record is persisted before it is published ──


class TestTheRecordCarriesTheKillBeforeItIsPublished:
    """A failed kill reaches the ONE terminal report and the tombstone, not memory.

    The common shape of a reap on a live run: the reap's reset closes the ACP
    pipes, the run's in-flight stream raises ``AcpProcessDied``, and the run's
    own arm writes the record (``done``, the tombstone) while the reap is still
    awaiting that reset or the fallback kill that follows it. A run ``finally``
    that claimed and published the terminal report then would send the
    ``subagent_done`` event and the parent's completion before the kill has
    decided, and a failure the fallback reports afterwards would reach only the
    in-memory error text -- the tombstone on disk and the report the parent
    reads would both say the run was simply reaped. Once a reap has started,
    it owns the report: it claims after its kill has decided, with the failure
    appended and the tombstone re-written.
    """

    @pytest.mark.asyncio
    async def test_a_failed_kill_reaches_the_report_when_the_stream_dies_under_the_reset(
        self,
    ) -> None:
        manager, info, _key = _overdue_run("pub1", pid=9191)
        info._session_sharing = False
        reset_started = asyncio.Event()
        hang = asyncio.Event()

        async def _reset(session_key: str, **_: Any) -> bool:
            if manager._sessions._sessions.pop(session_key, None) is not None:
                # The reap's reset: pops the session, then hangs in the
                # provider shutdown; the run's stream dies under it (below).
                reset_started.set()
                await hang.wait()
                return True
            # The run's own teardown, arriving second: the key is already gone.
            return False

        async def _stream(_info: SubagentInfo, _session_key: str) -> None:
            await reset_started.wait()
            raise AcpProcessDied("Runtime process died during prompt — killed (provider shutdown)")

        manager._sessions.reset = AsyncMock(side_effect=_reset)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05),
            patch("kiro_crew.subagent.write_tombstone") as tombstones,
            patch.object(manager, "_run_inner", AsyncMock(side_effect=_stream)),
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=_REFUSAL),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()),
        ):
            run_task = asyncio.create_task(manager._run(info))
            manager._tasks["pub1"] = run_task
            await asyncio.sleep(0)

            await manager._force_reap("pub1", info, 7200.0)

            hang.set()
            await asyncio.wait_for(asyncio.gather(run_task, return_exceptions=True), timeout=5)

        assert _audit(mock_sel, "reaper_force_kill")["outcome"] == "failed"
        # ONE terminal report, published after the kill decided, naming the failure.
        done_events = [
            call.args
            for call in manager._on_event.await_args_list
            if call.args[0] == "subagent_done"
        ]
        assert len(done_events) == 1, f"expected one subagent_done event, saw {len(done_events)}"
        published_error = done_events[0][2]["error"] or ""
        assert "; kill failed: ValueError: kill_process_tree: refusing" in published_error, (
            "the parent was told the run was reaped while its process is still alive: "
            f"{published_error!r}"
        )
        assert manager._on_done.await_count == 1
        # The tombstone on disk carries the failure too: the record was
        # re-written after the kill decided, not left as the run's arm wrote it.
        assert tombstones.call_args is not None, "no tombstone was written"
        assert "; kill failed: ValueError: kill_process_tree: refusing" in (
            tombstones.call_args.kwargs["detail"]
        ), "the tombstone says the run was reaped while its process is still alive"
        assert info.done and info.reaped
        assert manager._process_handles == {}


# ── Windows shape ──


class TestWindowsShape:
    """On win32 ``taskkill /T`` is the only tree walker, so a raised tree kill stays a failure."""

    @pytest.mark.asyncio
    async def test_on_windows_a_root_only_fallback_does_not_clear_a_tree_kill_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, _info, key = _overdue_run("win1", pid=9090)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=OSError("taskkill /T failed (exit 128)")),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            failure = await manager._sigkill_session(key, _handle_of(manager, "win1"))

        assert failure == "OSError: taskkill /T failed (exit 128)"

    @pytest.mark.asyncio
    async def test_on_windows_a_tree_that_is_already_gone_is_nothing_to_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, _info, key = _overdue_run("win2", pid=9191)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=ProcessLookupError("no such tree")),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "win2")) is None

    @pytest.mark.asyncio
    async def test_on_posix_a_root_only_fallback_that_lands_is_a_delivered_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: on POSIX the escaped-children sweep reaches what the group signal missed."""
        manager, _info, key = _overdue_run("posix1", pid=9292)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            assert await manager._sigkill_session(key, _handle_of(manager, "posix1")) is None
