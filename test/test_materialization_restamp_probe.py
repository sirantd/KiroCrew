"""Focused regression probe for non-capability materialization drift."""

import json

import pytest
from test_agent_capabilities import (  # noqa: F401 -- editor is a pytest fixture used by name
    editor,
    save,
    spec_for,
)

from kiro_crew import agent_state
from kiro_crew.agent_capabilities import (
    CapabilityError,
    _digest,
    prepare_member_capabilities,
)
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION


def test_empty_review_refuses_hidden_permission_shortcut_drift(
    editor,  # noqa: F811 -- pytest fixture by name
):
    service, home, specs, _ = editor
    save(service, enroll=True)
    spec = spec_for(home, specs)
    target = spec["name"]
    original_intent = agent_state.get_capabilities(target)
    path = specs / (target + ".json")
    spec["toolsSettings"] = {"shell": {"autoAllowReadonly": True}}
    path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(CapabilityError, match="alternate_permissions_require_review"):
        save(service)

    assert agent_state.get_capabilities(target) == original_intent
    assert spec_for(home, specs) == spec
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")


def test_empty_review_restamps_noncapability_drift(editor):  # noqa: F811 -- pytest fixture by name
    service, home, specs, _ = editor
    save(service, enroll=True)
    spec = spec_for(home, specs)
    target = spec["name"]
    original_intent = agent_state.get_capabilities(target)
    path = specs / (target + ".json")
    spec["toolsSettings"] = {"read": {"setting": "custom"}}
    path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")

    save(service)

    repaired_intent = agent_state.get_capabilities(target)
    assert spec_for(home, specs) == spec
    assert repaired_intent["revision"] != original_intent["revision"]
    assert repaired_intent["materialized"] != original_intent["materialized"]
    assert prepare_member_capabilities("A")["status"] == "unverified"


def test_empty_review_restamp_hashes_untouched_spec_when_permission_gate_refuses(
    editor,  # noqa: F811 -- pytest fixture by name
    monkeypatch,
):
    from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

    service, home, specs, parent = editor
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )
    parent["permissions"] = derived_agent_permissions(parent["allowedTools"], parent["name"])
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    save(service, enroll=True)
    spec = spec_for(home, specs)
    target = spec["name"]
    path = specs / (target + ".json")
    assert "permissions" in spec

    spec["toolsSettings"] = {"read": {"setting": "custom"}}
    path.write_text(json.dumps(spec), encoding="utf-8")
    on_disk_bytes = path.read_bytes()
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")

    # An unknown CLI version removes permissions from the validation projection.
    # Restamping must still hash the untouched spec because this branch writes no file.
    monkeypatch.setattr("kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: None)
    save(service)

    repaired_intent = agent_state.get_capabilities(target)
    on_disk_spec = json.loads(path.read_bytes())
    mutated_projection = dict(on_disk_spec)
    mutated_projection.pop("permissions")
    assert path.read_bytes() == on_disk_bytes
    assert repaired_intent["materialized"] == _digest(on_disk_spec)
    assert repaired_intent["materialized"] != _digest(mutated_projection)
    assert prepare_member_capabilities("A")["status"] == "unverified"
