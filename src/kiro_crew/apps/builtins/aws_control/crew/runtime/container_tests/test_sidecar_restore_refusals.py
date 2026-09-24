"""What the restore step refuses, and why refusing is the safe direction.

The backend's own readers ignore an authority file they cannot parse and carry on with
an empty result. That is right for them and wrong here: bytes written by this step are
read once as "no conversations" and are then replaced by the backend's next flush, so a
malformed object would look like a restore that worked and leave the customer's list
empty. Refusing to boot turns that silent loss into a message an operator gets before
the task serves a turn.

Absence is the one case that is NOT a failure. A crew's first task finds nothing in the
bucket, which is a first boot. Every other reason a read does not return bytes -- a
denial above all -- is a failure, because reading a denial as absence is the route to
booting with an empty slot table and flushing it over the real one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from container.common import keys
from container.sidecar import restore as restore_mod
from container.sidecar.store import ObjectAbsent

from ._settings_helper import make_settings


class _DictStore:
    """Serves the bytes it is given; absence is the store's own exception."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def put(self, key: str, body, size: int) -> None:  # pragma: no cover - unused here
        self.objects[key] = body.read(size)

    def get(self, key: str, *, limit: int) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectAbsent(key) from None


class _DeniedStore:
    """Every read fails for a reason that is not absence."""

    def put(self, key: str, body, size: int) -> None:  # pragma: no cover - unused here
        raise AssertionError("the restore step does not put")

    def get(self, key: str, *, limit: int) -> bytes:
        raise RuntimeError("AccessDenied")


def _settings(tmp_path: Path):
    return make_settings(tmp_path, crew="crew-31", prefix="crews")


def _bucket_with(settings, **files: bytes) -> _DictStore:
    return _DictStore({keys.authority_key(settings, name): raw for name, raw in files.items()})


def test_bytes_that_are_not_utf8_refuse_the_boot(tmp_path):
    settings = _settings(tmp_path)
    store = _bucket_with(settings, **{"session_map.json": b"\xff\xfe not text"})

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, store)

    assert "session_map.json" in str(caught.value)
    assert not (settings.config_dir / "session_map.json").exists()


def test_bytes_that_are_not_json_refuse_the_boot(tmp_path):
    settings = _settings(tmp_path)
    store = _bucket_with(settings, **{"session_map.json": b"{not json"})

    with pytest.raises(restore_mod.RestoreFailed):
        restore_mod.restore_authority(settings, store)


def test_json_that_is_not_an_object_refuses_the_boot(tmp_path):
    """The backend requires an object at the top level and ignores anything else."""
    settings = _settings(tmp_path)
    store = _bucket_with(settings, **{"session_map.json": b'["cust-1"]'})

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, store)

    assert "not an object" in str(caught.value)


def test_open_slots_with_a_keys_field_that_is_not_a_list_refuses_the_boot(tmp_path):
    settings = _settings(tmp_path)
    store = _bucket_with(
        settings,
        **{"session_map.json": b"{}", "open_slots.json": b'{"keys": "cust-1"}'},
    )

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, store)

    assert "open_slots.json" in str(caught.value)


def test_open_slots_without_a_keys_field_is_legal(tmp_path):
    """An absent ``keys`` means no open slots, which is a state the backend writes."""
    settings = _settings(tmp_path)
    store = _bucket_with(
        settings, **{"session_map.json": b"{}", "open_slots.json": b'{"version": 2}'}
    )

    result = restore_mod.restore_authority(settings, store)

    assert sorted(result.restored) == sorted(keys.AUTHORITY_NAMES)
    assert (settings.config_dir / "open_slots.json").read_bytes() == b'{"version": 2}'


def test_a_denied_read_is_not_read_as_absence(tmp_path):
    settings = _settings(tmp_path)

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, _DeniedStore())

    assert "not the same" in str(caught.value)


def test_an_empty_bucket_is_a_first_boot_and_not_a_failure(tmp_path):
    settings = _settings(tmp_path)

    result = restore_mod.restore_authority(settings, _DictStore({}))

    assert result.restored == []
    assert sorted(result.absent) == sorted(keys.AUTHORITY_NAMES)


def test_a_symlinked_config_directory_refuses_the_boot(tmp_path):
    """Writing through a link would put this task's conversation index outside its home.

    Seeded with BOTH authority files, because a half-published pair refuses earlier and for
    a different reason, and this test is about the write rather than about the pair.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "data").symlink_to(elsewhere, target_is_directory=True)
    settings = make_settings(root, crew="crew-31", prefix="crews")
    store = _bucket_with(
        settings, **{"session_map.json": b"{}", "open_slots.json": b'{"keys": []}'}
    )

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, store)

    assert "symlink" in str(caught.value)


def test_validate_accepts_what_the_backend_accepts(tmp_path):
    """The check is exactly as strict as the readers require, and no stricter."""
    restore_mod.validate_authority("session_map.json", b'{"cust-1": "sess-1"}')
    restore_mod.validate_authority("open_slots.json", b'{"keys": []}')
    restore_mod.validate_authority("open_slots.json", b"{}")
