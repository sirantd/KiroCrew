"""The owner's credential-redaction switch (``security.redaction_switch``).

Credential-shaped material is SYNTHESIZED at runtime, for the reason
``test_file_delivery_consent.py`` gives: a diff carrying a working token string
reads as an exfiltration recipe to a review provider, while the scanner sees the
same bytes either way.
"""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import security
from kiro_crew.security import redaction_switch


def _synth_aws_key() -> str:
    prefix = "A" + "KIA"
    return prefix + hashlib.sha256(b"kc-redaction-switch").hexdigest().upper()[:16]


def _synth_token_url() -> str:
    # A ``?token=`` URL: the pass-4 shape that motivated the switch.
    return "https://approve.example.test/auth?token=" + hashlib.sha256(b"kc-rs").hexdigest()


def _synth_exfil_url() -> str:
    # A long high-entropy query to a non-exempt host trips the exfil pass.
    blob = hashlib.sha256(b"kc-exfil").hexdigest() * 4
    return f"https://collector.example.test/x?d={blob}"


@pytest.fixture(autouse=True)
def _isolated_switch(tmp_path, monkeypatch):
    """Point the switch at a tmp keystone and drop any cached verdict."""
    store = tmp_path / "credential_redaction.json"
    monkeypatch.setattr(redaction_switch, "_path", lambda: store, raising=True)
    redaction_switch.invalidate_cache()
    yield store
    redaction_switch.invalidate_cache()


# ------------------------------------------------------------- the material trips


class TestSynthesizedMaterialActuallyTrips:
    def test_aws_key_is_redacted_by_default(self):
        key = _synth_aws_key()
        assert security.redact_credentials(key)[0] != key

    def test_token_url_value_is_redacted_by_default(self):
        url = _synth_token_url()
        out = security.redact_credentials(url)[0]
        assert out != url
        assert "token=" in out


# ------------------------------------------------------------------ the default


class TestDefaultIsOn:
    def test_absent_record_reads_enabled(self, _isolated_switch):
        assert not _isolated_switch.exists()
        assert redaction_switch.read_state().enabled is True
        assert redaction_switch.refresh_now() is True

    def test_non_utf8_bytes_read_enabled(self, _isolated_switch):
        _isolated_switch.write_bytes(b'{"enabled": false, "x": "\xff\xfe"}')
        redaction_switch.invalidate_cache()
        assert redaction_switch.read_state().enabled is True
        assert redaction_switch.refresh_now() is True

    @pytest.mark.parametrize(
        "raw",
        ["", "not json", "[false]", "null", '{"enabled": "false"}', '{"enabled": 0}', "{}"],
    )
    def test_only_a_literal_false_disables(self, _isolated_switch, raw):
        _isolated_switch.write_text(raw, encoding="utf-8")
        redaction_switch.invalidate_cache()
        assert redaction_switch.read_state().enabled is True
        assert redaction_switch.refresh_now() is True

    def test_an_unreadable_record_reads_enabled(self, _isolated_switch):
        _isolated_switch.write_text('{"enabled": false}', encoding="utf-8")
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("needs a POSIX permission denial")
        _isolated_switch.chmod(0)
        redaction_switch.invalidate_cache()
        try:
            assert redaction_switch.read_state().enabled is True
            assert redaction_switch.refresh_now() is True
        finally:
            _isolated_switch.chmod(0o600)


# -------------------------------------------------------------- what OFF does


def _off() -> None:
    redaction_switch.set_enabled(False, changed_at="2026-09-24T23:00:00+00:00")


class TestSwitchedOffInsideOwnerView:
    """The switch acts ONLY inside an explicit ``owner_view`` scope."""

    def test_set_enabled_false_records_the_position(self, _isolated_switch):
        state = redaction_switch.set_enabled(False, changed_at="2026-09-24T23:00:00+00:00")
        assert state.enabled is False
        assert json.loads(_isolated_switch.read_text())["enabled"] is False

    def test_owner_view_passes_credentials_through_when_off(self):
        _off()
        key, url = _synth_aws_key(), _synth_token_url()
        with redaction_switch.owner_view():
            assert security.redact_credentials(key) == (key, [])
            assert security.redact_credentials(url) == (url, [])
            assert security.redact_owner_view(key) == key

    def test_owner_view_still_redacts_when_on(self):
        key = _synth_aws_key()
        with redaction_switch.owner_view():
            assert security.redact_credentials(key)[0] != key
            assert security.redact_owner_view(key) != key

    def test_set_enabled_true_restores_redaction_in_owner_view(self):
        _off()
        key = _synth_aws_key()
        with redaction_switch.owner_view():
            assert security.redact_credentials(key)[0] == key
        redaction_switch.set_enabled(True, changed_at="2026-09-24T23:01:00+00:00")
        with redaction_switch.owner_view():
            assert security.redact_credentials(key)[0] != key

    def test_exfiltration_url_redaction_is_untouched_inside_owner_view(self):
        _off()
        url = _synth_exfil_url()
        with redaction_switch.owner_view():
            assert security.redact_exfiltration_urls(url)[0] != url
            assert security.redact(url) != url
            assert security.redact_owner_view(url) != url

    def test_the_verdict_is_snapshotted_for_the_whole_scope(self):
        """A flip mid-render is seen by the NEXT render, so cache guard and pass agree."""
        key = _synth_aws_key()
        with redaction_switch.owner_view():
            assert redaction_switch.credential_pass_bypassed() is False
            _off()  # the owner flips the switch while this render is in flight
            assert redaction_switch.credential_pass_bypassed() is False
            assert security.redact_credentials(key)[0] != key
        with redaction_switch.owner_view():
            assert redaction_switch.credential_pass_bypassed() is True
            redaction_switch.set_enabled(True, changed_at="2026-09-24T23:01:00+00:00")
            assert redaction_switch.credential_pass_bypassed() is True
            assert security.redact_credentials(key)[0] == key
        with redaction_switch.owner_view():
            assert redaction_switch.credential_pass_bypassed() is False

    def test_scope_is_task_local_and_restored_on_exit(self):
        _off()
        key = _synth_aws_key()
        assert redaction_switch.owner_view_active() is False
        with redaction_switch.owner_view():
            assert redaction_switch.owner_view_active() is True
        assert redaction_switch.owner_view_active() is False
        assert security.redact_credentials(key)[0] != key

    @pytest.mark.asyncio
    async def test_scope_does_not_leak_across_tasks(self):
        """An owner-view render on one task must not switch off a sibling task's post."""
        import asyncio

        _off()
        key = _synth_aws_key()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def owner_task():
            with redaction_switch.owner_view():
                entered.set()
                await release.wait()
                return security.redact_credentials(key)[0]

        async def sibling_task():
            await entered.wait()
            out = security.redact_credentials(key)[0]
            release.set()
            return out

        owner_out, sibling_out = await asyncio.gather(owner_task(), sibling_task())
        assert owner_out == key
        assert sibling_out != key


class TestSwitchedOffOutsideOwnerView:
    """Every surface that never opens the scope keeps the full pass."""

    def test_bare_redact_credentials_is_unconditional(self):
        _off()
        key = _synth_aws_key()
        assert security.redact_credentials(key)[0] != key
        assert security.redact(key) != key
        assert security.redact_with_findings(key)[0] != key

    def test_stream_redactor_default_is_unconditional(self):
        _off()
        key = _synth_aws_key()
        sr = security.StreamRedactor()
        out = sr.feed(key + " ") + sr.flush()
        assert key not in out

    def test_request_blocking_predicate_is_untouched(self):
        _off()
        assert security._contains_fixed_credential(_synth_aws_key()) is True

    def test_diagnostics_bundle_scrubs_regardless(self):
        """The bundle goes to a public issue; it never opens the owner scope."""
        from kiro_crew import diagnostics

        _off()
        clean, count = diagnostics._scrub(_synth_aws_key())
        assert clean != _synth_aws_key()
        assert count >= 1

    def test_channel_renderers_never_open_the_scope(self):
        """Slack, Webex and the shared messaging renderer redact at their sinks with
        the unconditional pass; none of them may import the owner-view scope."""
        import inspect

        from kiro_crew.messaging import renderer as messaging_renderer
        from kiro_crew.slack import renderer as slack_renderer
        from kiro_crew.webex import renderer as webex_renderer

        for mod in (slack_renderer, webex_renderer, messaging_renderer):
            src = inspect.getsource(mod)
            assert "owner_view" not in src, mod.__name__

    def test_only_the_named_owner_seams_open_the_scope(self):
        """A grep-pinned allowlist: adding an owner-view seam is a review decision."""
        import re
        from pathlib import Path

        pkg = Path(security.__file__).resolve().parents[1]  # src/kiro_crew
        needle = re.compile(r"owner_view(?:_scope)?\(|redact_owner_view")
        files = {
            "src/kiro_crew/" + f.relative_to(pkg).as_posix()
            for f in pkg.rglob("*.py")
            if "/tests/" not in f.as_posix() and needle.search(f.read_text(encoding="utf-8"))
        }
        assert files == {
            "src/kiro_crew/dashboard/handlers/files.py",  # api_file_read / api_file_watch, owner only
            "src/kiro_crew/platform/context.py",
            "src/kiro_crew/security/__init__.py",
            "src/kiro_crew/security/_exports.py",  # the frozen facade name list
            "src/kiro_crew/security/redaction.py",
            "src/kiro_crew/security/redaction_switch.py",
        }, files

    def test_the_chat_surface_never_opens_the_scope(self):
        """Chat is OUT of scope, and deliberately so: ``_flush_segment`` redacts the
        assistant text BEFORE ``slot.append``, so the transcript holds the redacted
        bytes and no display-time scope could restore them; the live SSE/WS streams
        fan one chunk to every connected client besides. An owner-view seam there
        would advertise a raw transcript it cannot deliver."""
        import inspect

        from kiro_crew.dashboard import chat_runner, chat_utils

        assert "owner_view" not in inspect.getsource(chat_runner)
        assert "owner_view" not in inspect.getsource(chat_utils)

    def test_file_viewer_opens_the_scope_only_for_the_owner(self):
        import inspect

        from kiro_crew.dashboard.handlers import files as files_handlers

        for fn in (files_handlers.api_file_read, files_handlers.api_file_watch):
            src = inspect.getsource(fn)
            assert "redact_owner_view_via_context" in src
            assert "owner_view_for_request" in src

    def test_file_admission_gates_use_the_unconditional_pass(self):
        """The outbox flagged-file check and the upload gates decide whether a
        file may LEAVE; they are not owner-view renders."""
        import inspect

        from kiro_crew.dashboard.handlers import files as files_handlers

        assert "redact_owner_view_via_context" not in inspect.getsource(
            files_handlers.api_outbox_download
        )
        assert "redact_owner_view_via_context" not in inspect.getsource(
            files_handlers._gate_upload_file
        )
        assert "redact_owner_view_via_context" in inspect.getsource(files_handlers.api_file_read)


# ---------------------------------------------------------------- the cache


class TestCache:
    def test_the_hot_path_never_touches_the_filesystem(self, _isolated_switch, monkeypatch):
        """``credential_redaction_enabled`` answers from memory; disk work is off-thread."""
        redaction_switch.refresh_now()

        def boom(*a, **k):
            raise AssertionError("os.stat on the hot path")

        monkeypatch.setattr(redaction_switch.os, "stat", boom)
        monkeypatch.setattr(redaction_switch, "_refresh_in_background", lambda: None)
        redaction_switch._cached_at = 0.0  # stale on purpose
        for _ in range(50):
            redaction_switch.credential_redaction_enabled()

    def test_a_stale_snapshot_schedules_one_background_refresh(self, _isolated_switch, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(
            redaction_switch,
            "_refresh_in_background",
            lambda: calls.__setitem__("n", calls["n"] + 1),
        )
        redaction_switch._cached_at = 0.0
        redaction_switch.credential_redaction_enabled()
        assert calls["n"] == 1
        redaction_switch._cached_at = redaction_switch.time.monotonic()
        redaction_switch.credential_redaction_enabled()
        assert calls["n"] == 1  # fresh: no refresh scheduled

    def test_a_write_by_another_process_is_seen_after_a_refresh(self, _isolated_switch):
        assert redaction_switch.refresh_now() is True
        # Written behind the cache's back (no invalidate), as a second process would.
        _isolated_switch.write_text('{"enabled": false}', encoding="utf-8")
        assert redaction_switch.credential_redaction_enabled() is True  # snapshot
        assert redaction_switch.refresh_now() is False  # what the thread computes

    def test_background_refresh_updates_the_snapshot(self, _isolated_switch):
        import time

        redaction_switch.refresh_now()
        _isolated_switch.write_text('{"enabled": false}', encoding="utf-8")
        redaction_switch._cached_at = 0.0
        redaction_switch.credential_redaction_enabled()  # schedules the thread
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and redaction_switch._cached_enabled:
            time.sleep(0.01)
        assert redaction_switch._cached_enabled is False

    def test_a_stale_off_snapshot_falls_back_to_on(self, _isolated_switch, monkeypatch):
        """A stalled refresh must not keep the bypass alive: OFF is aged out, ON is not."""
        _off()
        monkeypatch.setattr(redaction_switch, "_refresh_in_background", lambda: None)
        clock = {"now": redaction_switch.time.monotonic()}
        monkeypatch.setattr(redaction_switch.time, "monotonic", lambda: clock["now"])
        redaction_switch._cached_at = clock["now"]
        assert redaction_switch.credential_redaction_enabled() is False
        clock["now"] += redaction_switch._STALE_OFF_GRACE_SECS - 0.1
        assert redaction_switch.credential_redaction_enabled() is False  # inside the grace
        clock["now"] += 0.2
        assert redaction_switch.credential_redaction_enabled() is True  # grace spent
        # ON is the default and is never aged out.
        redaction_switch.set_enabled(True, changed_at="2026-09-24T23:01:00+00:00")
        clock["now"] += 3600
        assert redaction_switch.credential_redaction_enabled() is True

    def test_a_refresh_that_started_before_a_write_cannot_publish_over_it(
        self, _isolated_switch, monkeypatch
    ):
        """Generation fence: stale bytes read before set_enabled(True) are discarded."""
        import threading

        _off()
        assert redaction_switch.refresh_now() is False
        # Make the refresh's read slow enough that a write lands in the middle.
        read_started = threading.Event()
        release_read = threading.Event()
        real_read_state = redaction_switch.read_state

        def slow_read_state():
            stale = real_read_state()  # the OLD bytes, read BEFORE the write lands
            read_started.set()
            release_read.wait(5)
            return stale

        monkeypatch.setattr(redaction_switch, "read_state", slow_read_state)
        # Force a stat change so the refresh goes through read_state.
        _isolated_switch.write_text(
            '{"enabled": false, "changed_at": "2026-09-24T23:00:01+00:00"}', encoding="utf-8"
        )
        refresher = threading.Thread(target=redaction_switch._refresh_from_disk)
        refresher.start()
        assert read_started.wait(5)
        # The write lands while the refresh still holds the OLD bytes.
        monkeypatch.setattr(redaction_switch, "read_state", real_read_state)
        redaction_switch.set_enabled(True, changed_at="2026-09-24T23:01:00+00:00")
        assert redaction_switch.credential_redaction_enabled() is True
        release_read.set()
        refresher.join(5)
        # The stale refresh published nothing: ON stands.
        assert redaction_switch._cached_enabled is True
        assert redaction_switch.credential_redaction_enabled() is True

    def test_an_off_record_made_unreadable_without_a_stat_change_falls_back_to_on(
        self, _isolated_switch
    ):
        """``chmod 000`` changes neither mtime nor size; OFF must still be RE-READ."""
        _off()
        assert redaction_switch.refresh_now() is False
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("needs a POSIX permission denial")
        _isolated_switch.chmod(0)
        try:
            assert redaction_switch.refresh_now() is True
        finally:
            _isolated_switch.chmod(0o600)

    def test_an_on_record_keeps_the_stat_shortcut(self, _isolated_switch, monkeypatch):
        redaction_switch.set_enabled(True, changed_at="2026-09-24T23:01:00+00:00")
        calls = {"n": 0}
        real = redaction_switch.read_state

        def counting():
            calls["n"] += 1
            return real()

        monkeypatch.setattr(redaction_switch, "read_state", counting)
        redaction_switch.refresh_now()
        redaction_switch.refresh_now()
        assert calls["n"] == 0

    def test_set_enabled_is_visible_to_this_process_at_once(self):
        _off()
        assert redaction_switch.credential_redaction_enabled() is False
        redaction_switch.set_enabled(True, changed_at="2026-09-24T23:01:00+00:00")
        assert redaction_switch.credential_redaction_enabled() is True


# ------------------------------------------------------------- the keystone


class TestTheLeafIsAKeystone:
    def test_the_path_is_the_named_leaf_under_the_data_home(self, monkeypatch, tmp_path):
        from kiro_crew.config import loader

        monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
        assert loader.credential_redaction_path() == tmp_path / "credential_redaction.json"

    def test_fenced_on_the_agent_file_tool_path(self):
        from kiro_crew.security.paths import _CREW_SECRET_LEAVES, is_sensitive_path

        assert "credential_redaction.json" in _CREW_SECRET_LEAVES
        assert is_sensitive_path("~/.kiro/crew/credential_redaction.json") is True

    def test_mounted_read_only_and_pre_created_in_the_sandbox(self):
        from kiro_crew import sandbox

        assert "credential_redaction.json" in sandbox._CREW_READONLY_LEAVES
        assert "credential_redaction.json" in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        assert "credential_redaction.json" in sandbox._CREW_CHILD_WITHHELD_LEAVES

    def test_config_json_carries_no_switch(self):
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert not any("redaction" in key for key in _EDITABLE_CONFIG)

    def test_exported_on_the_security_facade(self):
        from kiro_crew.security._exports import EXPORTED_NAMES

        assert "credential_redaction_enabled" in EXPORTED_NAMES
        assert "owner_view" in EXPORTED_NAMES
        assert "redact_owner_view" in EXPORTED_NAMES
        assert (
            security.credential_redaction_enabled is redaction_switch.credential_redaction_enabled
        )
        assert security.owner_view is redaction_switch.owner_view


# --------------------------------------------------------------- the handler


def _request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1", body=None):
    """A request shaped like a real DASHBOARD OWNER call (see test_decisions_consent.py)."""
    req = MagicMock()
    req.path = "/api/security/credential-redaction"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    if isinstance(body, Exception):
        req.json = AsyncMock(side_effect=body)
    else:
        req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def _quiet_audit(monkeypatch):
    from kiro_crew.dashboard.handlers import credential_redaction as handlers

    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handlers,
        "_audit",
        lambda *, outcome, caller, detail="": events.append((outcome, caller)),
        raising=True,
    )
    return events


class TestHandlers:
    @pytest.mark.asyncio
    async def test_get_reports_the_default(self, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_get

        resp = await api_credential_redaction_get(_request())
        assert resp.status == 200
        assert json.loads(resp.text) == {"enabled": True, "changed_at": ""}

    @pytest.mark.asyncio
    async def test_put_false_records_and_audits(self, _isolated_switch, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        resp = await api_credential_redaction_put(_request(body={"enabled": False}))
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert payload["enabled"] is False and payload["changed_at"]
        assert json.loads(_isolated_switch.read_text())["enabled"] is False
        assert ("disabled", "owner") in _quiet_audit
        with redaction_switch.owner_view():
            assert security.redact_credentials(_synth_aws_key())[0] == _synth_aws_key()
        assert security.redact_credentials(_synth_aws_key())[0] != _synth_aws_key()

    @pytest.mark.asyncio
    async def test_put_true_restores(self, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        await api_credential_redaction_put(_request(body={"enabled": False}))
        resp = await api_credential_redaction_put(_request(body={"enabled": True}))
        assert json.loads(resp.text)["enabled"] is True
        assert ("enabled", "owner") in _quiet_audit
        with redaction_switch.owner_view():
            assert security.redact_credentials(_synth_aws_key())[0] != _synth_aws_key()

    def test_write_and_audit_are_one_unit_on_the_worker_thread(self):
        """A cancelled handler cannot skip the audit: both run in the same closure."""
        import inspect

        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        src = inspect.getsource(api_credential_redaction_put)
        closure = src[src.index("def _write_and_audit") : src.index("try:\n        state = await")]
        assert "set_enabled(" in closure
        assert 'outcome="enabled" if enabled else "disabled"' in closure
        assert "asyncio.to_thread(_write_and_audit)" in src

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{"enabled": "false"}, {"enabled": 0}, {}, [False], None])
    async def test_put_refuses_a_non_boolean(self, _isolated_switch, _quiet_audit, body):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        resp = await api_credential_redaction_put(_request(body=body))
        assert resp.status == 400
        assert not _isolated_switch.exists()

    @pytest.mark.asyncio
    async def test_put_refuses_invalid_json(self, _isolated_switch, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        resp = await api_credential_redaction_put(_request(body=ValueError("bad json")))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_non_owner_is_refused_on_both_verbs(self, _isolated_switch, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import (
            api_credential_redaction_get,
            api_credential_redaction_put,
        )

        get = await api_credential_redaction_get(_request(user="someone-else"))
        put = await api_credential_redaction_put(
            _request(user="someone-else", body={"enabled": False})
        )
        assert get.status == 403 and put.status == 403
        assert not _isolated_switch.exists()
        assert ("denied", "gateway") in _quiet_audit

    @pytest.mark.asyncio
    async def test_an_app_token_is_refused(self, _isolated_switch, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        resp = await api_credential_redaction_put(_request(app="some-app", body={"enabled": False}))
        assert resp.status == 403
        assert not _isolated_switch.exists()

    def test_routes_are_registered(self):
        import inspect

        from kiro_crew.dashboard.routes import system

        src = inspect.getsource(system)
        assert (
            'add_get("/api/security/credential-redaction", handlers.api_credential_redaction_get)'
            in src
        )
        assert (
            'add_put("/api/security/credential-redaction", handlers.api_credential_redaction_put)'
            in src
        )
