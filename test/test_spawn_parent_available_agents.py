"""The parent agent's ``toolsSettings.subagent.availableAgents`` gates ``spawn_run``.

kiro-cli defines that key for its own built-in ``subagent`` tool: a glob list
of the agents THIS agent may spawn, and omitting it allows all. Kiro Crew's
sub-agents come through ``spawn_run`` / ``spawn_sub_agents`` instead, whose
admission only ever asked "does the target exist" plus Kiro Crew's own
governance -- so an operator who declared the allowlist on their agent spec
saw it silently ignored and the spawn tool advertised every installed agent as
valid.

Fixture (the reproduction from the triage): an ``orchestrator`` spec that
declares ``availableAgents: [agent1, agent2, agent3]`` (and, to prove the two
keys are not confused, the same names under ``trustedAgents``), the three named
agents, and a ``rogue`` agent that is installed but not in the list.

Contract pinned here:

* ``rogue`` is REFUSED with ``AGENT_NOT_AVAILABLE_CODE`` when the parent is
  ``orchestrator``; ``agent1`` is admitted.
* A parent whose spec OMITS ``availableAgents`` admits everything it did
  before -- the control that proves no undeclared user's behaviour moves.
* ``trustedAgents`` alone is NOT an allowlist (upstream semantics: it means
  "run without approval prompts").
* The gate is an intersection with governance, never a replacement for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import agent_discovery
from kiro_crew import subagent as sa
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.mcp_tools import spawn as spawn_tools


def _write_spec(agents_dir: Path, name: str, **extra: Any) -> None:
    spec: dict[str, Any] = {"name": name, "description": f"{name} test agent", "tools": ["fs_read"]}
    spec.update(extra)
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", d)
    agent_discovery.clear_list_agents_cache()
    yield d
    agent_discovery.clear_list_agents_cache()


@pytest.fixture
def triage_fixture(agents_dir: Path) -> Path:
    """§3c of the triage report: orchestrator + agent1..3 + an out-of-list agent."""
    names = ["agent1", "agent2", "agent3"]
    _write_spec(
        agents_dir,
        "orchestrator",
        toolsSettings={"subagent": {"availableAgents": names, "trustedAgents": names}},
    )
    for n in names:
        _write_spec(agents_dir, n)
    _write_spec(agents_dir, "rogue")
    # A parent that declares NOTHING about sub-agents: the pre-existing shape.
    _write_spec(agents_dir, "plain")
    # A parent that only wrote trustedAgents -- the misreading the report came in
    # with. It must NOT act as an allowlist.
    _write_spec(agents_dir, "trust-only", toolsSettings={"subagent": {"trustedAgents": names}})
    return agents_dir


class TestSpecReading:
    def test_omitted_key_is_none_not_empty(self) -> None:
        assert sa.spawn_allowlist({}) is None
        assert sa.spawn_allowlist({"toolsSettings": {}}) is None
        assert sa.spawn_allowlist({"toolsSettings": {"subagent": {}}}) is None
        # trustedAgents is a trust grant, not an allowlist.
        assert sa.spawn_allowlist({"toolsSettings": {"subagent": {"trustedAgents": ["a"]}}}) is None

    def test_declared_list_is_returned_verbatim(self) -> None:
        spec = {"toolsSettings": {"subagent": {"availableAgents": ["reviewer", "docs-*"]}}}
        assert sa.spawn_allowlist(spec) == ("reviewer", "docs-*")

    def test_malformed_declaration_fails_closed(self) -> None:
        """Declared but not a list of strings: the operator meant to restrict, so
        the answer is "nothing allowed", never "allow all"."""
        assert sa.spawn_allowlist({"toolsSettings": {"subagent": {"availableAgents": "x"}}}) == ()
        assert sa.spawn_allowlist(
            {"toolsSettings": {"subagent": {"availableAgents": [1, "a"]}}}
        ) == ("a",)

    def test_glob_semantics_match_kiro_cli(self) -> None:
        assert sa.agent_matches_allowlist("docs-writer", ("docs-*",))
        assert sa.agent_matches_allowlist("reviewer", ("reviewer",))
        assert not sa.agent_matches_allowlist("reviewer2", ("reviewer",))
        assert not sa.agent_matches_allowlist("anything", ())

    def test_app_namespaced_agent_matches_its_bare_name(self) -> None:
        """An app's materialized ``<app>--<agent>`` is what the gateway sees, while
        the app's spec lists the bare name kiro-cli's own gate matches against."""
        assert sa.agent_matches_allowlist(
            "pptx-maker--pptx-maker-composer", ("pptx-maker-composer",)
        )


class TestParentAllowlistResolution:
    def test_declared_parent_resolves_to_its_list(self, triage_fixture: Path) -> None:
        assert sa.parent_spawn_allowlists("orchestrator") == (("agent1", "agent2", "agent3"),)

    def test_undeclared_parent_resolves_to_nothing(self, triage_fixture: Path) -> None:
        assert sa.parent_spawn_allowlists("plain") == ()
        assert sa.parent_spawn_allowlists("trust-only") == ()

    def test_unknown_parent_resolves_to_nothing(self, triage_fixture: Path) -> None:
        # No spec for the parent means no declaration to honour: unchanged behaviour.
        assert sa.parent_spawn_allowlists("no-such-agent") == ()
        assert sa.parent_spawn_allowlists("") == ()


class TestVetAgainstParentSpec:
    def test_rogue_is_refused_by_a_declaring_parent(self, triage_fixture: Path) -> None:
        denial = sa._vet_parent_available_agents("orchestrator", "rogue")
        assert denial is not None
        assert "rogue" in denial and "availableAgents" in denial
        # The allowlist travels with the refusal so the caller can self-correct.
        assert "agent1" in denial

    def test_listed_agent_is_admitted(self, triage_fixture: Path) -> None:
        for name in ("agent1", "agent2", "agent3"):
            assert sa._vet_parent_available_agents("orchestrator", name) is None

    def test_undeclared_parent_admits_everything(self, triage_fixture: Path) -> None:
        """The no-regression control: every pre-existing spawn from a parent that
        never wrote ``availableAgents`` still goes through, rogue included."""
        for parent in ("plain", "trust-only", "no-such-agent", ""):
            for child in ("agent1", "rogue", "orchestrator", "kirocrew"):
                assert sa._vet_parent_available_agents(parent, child) is None, (parent, child)

    def test_empty_child_is_not_vetted_here(self, triage_fixture: Path) -> None:
        # The gate resolves the EFFECTIVE template before calling; an empty name
        # never reaches the glob match, so it cannot be refused by accident.
        assert sa._vet_parent_available_agents("orchestrator", "") is None


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="")
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _parent_execution(template: str) -> ExecutionContext:
    return ExecutionContext(
        None, MemoryStoreRef("default"), "template", template, "persistent", "", template
    )


@pytest.mark.usefixtures("healthy_host_memory")
class TestGateWiring:
    """The check runs at the admission gate, next to governance, before any row
    is persisted, and reports its own code on the refused ``SubagentInfo``."""

    async def _spawn(self, parent_template: str, agent: str) -> Any:
        from kiro_crew.subagent import SubagentManager

        manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder())
        await manager.wait_taskq_ready()
        sel_mock = MagicMock()
        parent = _parent_execution(parent_template)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel", return_value=sel_mock),
            patch("kiro_crew.execution_context.read_session_execution", return_value=parent),
        ):
            info = manager.spawn("t", parent_session_key="chat-parent", agent=agent)
        return manager, info, sel_mock

    @pytest.mark.asyncio
    async def test_gate_refuses_out_of_list_agent(self, triage_fixture: Path) -> None:
        manager, info, sel_mock = await self._spawn("orchestrator", "rogue")
        assert info is not None and info.done is True
        assert info.error_code == sa.AGENT_NOT_AVAILABLE_CODE
        assert "rogue" in info.error and "availableAgents" in info.error
        # A policy refusal: no slot taken, no row persisted, audited as denied.
        assert manager._running_count == 0
        assert (
            manager._admission.taskq_store() is None
            or not manager._admission.taskq_store().list_rows()
        )
        denied = [
            c
            for c in sel_mock.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert len(denied) == 1 and "availableAgents" in denied[0].kwargs.get("error", "")

    @pytest.mark.asyncio
    async def test_gate_admits_listed_agent(self, triage_fixture: Path) -> None:
        _manager, info, _sel = await self._spawn("orchestrator", "agent1")
        assert info is not None
        assert info.error_code != sa.AGENT_NOT_AVAILABLE_CODE
        assert "availableAgents" not in (info.error or "")

    @pytest.mark.asyncio
    async def test_gate_leaves_an_undeclared_parent_alone(self, triage_fixture: Path) -> None:
        """No-regression control at the gate itself: the same out-of-list name
        from a parent that never declared the key is not refused on this ground."""
        for parent in ("plain", "trust-only"):
            _manager, info, _sel = await self._spawn(parent, "rogue")
            assert info is not None
            assert info.error_code != sa.AGENT_NOT_AVAILABLE_CODE
            assert "availableAgents" not in (info.error or "")

    @pytest.mark.asyncio
    async def test_inherited_template_is_vetted_too(self, triage_fixture: Path) -> None:
        """Omitting ``agent`` inherits the parent's own template, which is not in
        the parent's list here -- the effective template is what is checked, so
        the list cannot be routed around by not naming an agent."""
        _manager, info, _sel = await self._spawn("orchestrator", "")
        assert info is not None and info.error_code == sa.AGENT_NOT_AVAILABLE_CODE

    def test_error_code_is_distinct_from_not_found(self) -> None:
        assert sa.AGENT_NOT_AVAILABLE_CODE != sa.AGENT_NOT_FOUND_CODE
        assert sa.AGENT_NOT_AVAILABLE_CODE == "agent_not_available"

    def test_wave_short_circuit_recognises_the_code(self) -> None:
        assert spawn_tools._is_unknown_agent_refusal({"code": sa.AGENT_NOT_AVAILABLE_CODE}, "rogue")
        assert not spawn_tools._is_unknown_agent_refusal({"code": sa.AGENT_NOT_AVAILABLE_CODE}, "")


class TestToolDescriptionRoster:
    """``spawn_run``'s "Valid names right now" lists only what the parent may spawn."""

    def test_roster_is_filtered_by_the_parent_allowlist(
        self, triage_fixture: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(spawn_tools, "_parent_template_for_roster", lambda: "orchestrator")
        hint = spawn_tools._agent_roster_hint()
        assert "agent1" in hint and "agent2" in hint and "agent3" in hint
        assert "rogue" not in hint
        assert "availableAgents" in hint

    def test_roster_is_unfiltered_for_an_undeclared_parent(
        self, triage_fixture: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(spawn_tools, "_parent_template_for_roster", lambda: "plain")
        hint = spawn_tools._agent_roster_hint()
        assert "rogue" in hint and "agent1" in hint
        assert "availableAgents" not in hint

    def test_roster_is_unfiltered_when_the_parent_is_unknown(
        self, triage_fixture: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pre-existing shape: no session identity -> the whole installed roster.
        monkeypatch.setattr(spawn_tools, "_parent_template_for_roster", lambda: "")
        hint = spawn_tools._agent_roster_hint()
        assert "rogue" in hint and "agent1" in hint
