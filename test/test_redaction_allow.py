"""Allowed hosts: per workspace, trust-dir stored, relaxing only length/base64 checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.platform_compat import IS_POSIX
from kiro_crew.security import redaction_allow
from kiro_crew.security.exfil import redact_exfiltration_urls_with_records

_LONG = "https://reviews.corp.example/reviews?filter=" + "a" * 260


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "trust" / "redaction-allow.json"
    monkeypatch.setattr(redaction_allow, "_path_override", path)
    return path


def test_allow_list_and_revoke_round_trip(_store: Path) -> None:
    assert redaction_allow.allow_host("ws1", "Reviews.Corp.Example")
    assert redaction_allow.allowed_hosts_for("ws1") == {"reviews.corp.example"}
    assert redaction_allow.allowed_hosts_for("ws2") == frozenset()
    if IS_POSIX:  # Windows has no owner-only mode bits to read back.
        assert oct(_store.stat().st_mode & 0o777) == "0o600"
    assert redaction_allow.revoke_host("ws1", "reviews.corp.example")
    assert redaction_allow.list_allowed() == {}


def test_off_shape_host_is_refused() -> None:
    assert not redaction_allow.allow_host("ws1", "evil example")
    assert not redaction_allow.allow_host("ws1", "https://x.example")


def test_a_corrupt_file_means_nothing_is_allowed(_store: Path) -> None:
    _store.parent.mkdir(parents=True)
    _store.write_text("{not json")
    assert redaction_allow.allowed_hosts_for("ws1") == frozenset()


def test_an_allowed_host_keeps_its_long_query_link() -> None:
    blocked, _, records = redact_exfiltration_urls_with_records(_LONG)
    assert records and records[0]["rule"] == "exfil_query_length"
    kept, _, none = redact_exfiltration_urls_with_records(
        _LONG, extra_exempt_hosts=frozenset({"reviews.corp.example"})
    )
    assert kept == _LONG and none == []


def test_an_allowed_host_still_loses_a_credential() -> None:
    url = "https://reviews.corp.example/x?k=AKIA" + "IOSFODNN7EXAMPLE"
    out, _, _ = redact_exfiltration_urls_with_records(
        url, extra_exempt_hosts=frozenset({"reviews.corp.example"})
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_display_pass_keeps_an_allowed_link_only_inside_its_scope() -> None:
    from kiro_crew.dashboard.chat_utils import (
        _clear_display_redaction_cache,
        redact_display_content,
    )
    from kiro_crew.security.exfil import scoped_exempt_hosts

    _clear_display_redaction_cache()
    assert _LONG not in redact_display_content(_LONG)
    with scoped_exempt_hosts(frozenset({"reviews.corp.example"})):
        assert redact_display_content(_LONG) == _LONG
    # The cache is keyed on the scope, so the relaxed result does not leak out.
    assert _LONG not in redact_display_content(_LONG)


def test_prepare_messages_relaxes_the_slot_workspace_hosts() -> None:
    from kiro_crew.dashboard.chat_utils import _clear_display_redaction_cache, _prepare_messages

    _clear_display_redaction_cache()
    rows = [{"role": "assistant", "content": _LONG, "ts": "1"}]
    assert _LONG not in _prepare_messages(rows, False, live_child="", workspace="ws1")[0]["content"]
    redaction_allow.allow_host("ws1", "reviews.corp.example")
    assert _prepare_messages(rows, False, live_child="", workspace="ws1")[0]["content"] == _LONG
    assert _LONG not in _prepare_messages(rows, False, live_child="", workspace="ws2")[0]["content"]


def test_write_entry_keeps_an_allowed_link_only_inside_its_scope() -> None:
    from kiro_crew.dashboard.chat_persistence import _build_message_entry
    from kiro_crew.security.exfil import scoped_exempt_hosts

    row = {"role": "assistant", "content": _LONG, "ts": "1", "cls": "msg msg-a"}
    assert _LONG not in _build_message_entry(dict(row))["content"]
    with scoped_exempt_hosts(frozenset({"reviews.corp.example"})):
        assert _build_message_entry(dict(row))["content"] == _LONG
    # The entry cache is keyed on the scope, so the relaxed entry does not leak out.
    assert _LONG not in _build_message_entry(dict(row))["content"]


def test_load_keeps_an_allowed_link_for_the_slot_workspace() -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_persistence import _redact_loaded_content

    redaction_allow.allow_host("ws1", "reviews.corp.example")
    assert _redact_loaded_content(SimpleNamespace(workspace="ws1"), _LONG) == _LONG
    assert _LONG not in _redact_loaded_content(SimpleNamespace(workspace="ws2"), _LONG)


def test_every_persistence_redaction_of_row_content_is_scoped() -> None:
    import inspect

    from kiro_crew.dashboard import chat_persistence

    src = inspect.getsource(chat_persistence)
    assert src.count("_redact_loaded_content(slot, content)") == 2
    assert 'with scoped_exempt_hosts(allowed_hosts_for(getattr(slot, "workspace", None))):' in src


def _saved_row() -> dict:
    """An assistant row as saved before its host was allowed."""
    text, _, records = redact_exfiltration_urls_with_records(f"See [the reviews]({_LONG}).")
    assert records and records[0]["url"] == _LONG
    return {"role": "assistant", "content": text, "ts": "1", "meta": {"blocked_links": records}}


def test_a_saved_placeholder_shows_its_link_once_the_host_is_allowed() -> None:
    from kiro_crew.dashboard.chat_utils import _clear_display_redaction_cache, _prepare_messages

    _clear_display_redaction_cache()
    row = _saved_row()
    before = _prepare_messages([row], False, live_child="", workspace="ws1")[0]
    assert _LONG not in before["content"] and before["meta"]["blocked_links"]
    redaction_allow.allow_host("ws1", "reviews.corp.example")
    after = _prepare_messages([row], False, live_child="", workspace="ws1")[0]
    assert after["content"] == f"See [the reviews]({_LONG})."
    assert "blocked_links" not in after["meta"]
    # Another workspace, and the stored row itself, are untouched.
    other = _prepare_messages([row], False, live_child="", workspace="ws2")[0]
    assert _LONG not in other["content"]
    assert _LONG not in row["content"]


def test_restore_keeps_links_the_allow_does_not_cover() -> None:
    from kiro_crew.security.exfil import restore_allowed_links

    second = _LONG.replace("filter=", "sort=")
    text, _, records = redact_exfiltration_urls_with_records(f"a {_LONG} b {second}")
    assert len(records) == 2
    records[1] = {**records[1], "rule": "exfil_credential"}
    out, left = restore_allowed_links(text, records, frozenset({"reviews.corp.example"}))
    assert _LONG in out and second not in out and left == [records[1]]
    same, kept = restore_allowed_links(text, records, frozenset({"other.example"}))
    assert same == text and kept == records
    withheld = [{**r, "url": None} for r in records]
    assert restore_allowed_links(text, withheld, frozenset({"reviews.corp.example"}))[0] == text
