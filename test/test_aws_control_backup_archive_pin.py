"""The backup archive's bytes are bound to ONE inode from build to upload.

``test_aws_control_backup.py`` covers the two push paths with ``put_file`` mocked
out, so nothing there sees which file the upload would actually have opened. That
is the question these tests ask. The archive is staged in a directory a same-UID
process can write, and a NAME resolved once per step -- the entry-set fingerprint,
the size, the AWS CLI ``--body``, the body fingerprint -- is a separate answer per
step. A process that replaces the file between two of them makes the upload carry
bytes nothing checked, and an uploaded object has no recall.

So the whole-run tests here go through the REAL ``storage.put_file`` and stub the
subprocess chokepoint, reading the body the way the CLI child would. A swap
performed in the window immediately before that child resolves the body is what
separates a name-based upload from a descriptor-bound one.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.aws_control.backend import backup, storage

ACCOUNT = "111122223333"

#: What a same-UID process plants over the finished archive. Not a tarball, so a
#: test that reports these bytes as uploaded is reporting real exposure: they
#: stand in for any file the owner can read, which is what a hard link to
#: ``~/.aws/credentials`` would have made the upload carry.
PLANTED = b"SECRET-CREDENTIAL-BYTES-THAT-WERE-NEVER-CHECKED"


def _read_whole(fd: int) -> bytes:
    """Every byte of *fd* from offset 0, without disturbing its position."""
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(fd, 1024 * 1024, offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)


def _body_the_child_would_read(args: list[str], stdin_fd: int | None) -> bytes:
    """The bytes the AWS CLI child resolves for ``--body``, in either spelling.

    A path is read as the child would read it -- by re-resolving the name, which
    is the whole exposure. A descriptor spelling is read from the descriptor the
    child inherits, because that is literally the file it opens.
    """
    body = args[args.index("--body") + 1]
    if body.startswith(("/dev/stdin", "/dev/fd/", "/proc/self/fd/")):
        assert stdin_fd is not None, f"{body} was passed with no descriptor to resolve it"
        return _read_whole(stdin_fd)
    return Path(body).read_bytes()


class _SwapOnUpload:
    """Stands in for the subprocess chokepoint, swapping the archive first.

    The swap lands in the narrowest window there is: after every local decision
    has been taken and immediately before the child resolves the body. A
    name-based upload reads the planted file; an upload bound to the descriptor
    opened before the swap reads the archive.
    """

    def __init__(self) -> None:
        self.archive: Path | None = None
        self.uploaded: bytes = b""
        self.before_swap: bytes = b""
        self.swapped = False

    def note_archive(self, local_path: str) -> None:
        self.archive = Path(local_path)

    def __call__(
        self,
        args: list[str],
        profile: str,
        *,
        action: str,
        timeout: int = 30,
        extra_visible_dirs: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> str:
        if "put-object" in args and self.archive is not None and not self.swapped:
            self.swapped = True
            # Read before replacing, so the assertions have the archive the tar
            # actually wrote rather than having to re-derive it.
            self.before_swap = self.archive.read_bytes()
            # Unlink and re-create rather than truncate: this is the same-UID
            # replacement the module's own sandbox notes describe, and it leaves
            # any descriptor already open on the real inode pointing at it.
            self.archive.unlink()
            self.archive.write_bytes(PLANTED)
        if "put-object" in args:
            self.uploaded = _body_the_child_would_read(args, kwargs.get("stdin_fd"))
        return "{}"


@pytest.fixture
def _sessions_host(tmp_path, monkeypatch):
    """A populated host with both session halves and isolated module state."""
    if not backup._CAN_PIN_TRAVERSAL:
        pytest.skip("descriptor-pinned traversal is unavailable here; the run refuses by design")
    crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
    crew.mkdir(parents=True)
    (crew / "t.jsonl").write_bytes(b"transcript\n")
    cli = tmp_path / "cli_sessions"
    cli.mkdir(parents=True)
    (cli / "replay.jsonl").write_bytes(b"{}\n")
    monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
    monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
    monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: True)
    monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
    # No prior archive, so the unchanged-check cannot short-circuit the upload.
    monkeypatch.setattr(backup.storage, "list_object_versions", lambda *a, **k: [])
    return tmp_path


def _run_with_swap(monkeypatch) -> _SwapOnUpload:
    """Run one sessions backup whose archive is swapped just before the upload."""
    swap = _SwapOnUpload()
    real_put = storage.put_file

    def watching_put(*args: Any, **kwargs: Any) -> str:
        # positional: profile, region, bucket, section, key, local_path
        key, local_path = args[4], args[5]
        if key.endswith(".tar.gz"):
            swap.note_archive(local_path)
        return real_put(*args, **kwargs)

    monkeypatch.setattr(storage, "_checked", swap)
    monkeypatch.setattr(backup.storage, "put_file", watching_put)
    with (
        mock.patch.object(backup, "_authorize_upload"),
        # Both run after the archive PUT and would each reach the stub again;
        # neither is what these tests are about.
        mock.patch.object(backup, "_publish_label"),
        mock.patch.object(backup, "_prune_remote_archives"),
    ):
        backup.run_sessions_backup(ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER)
    return swap


class TestUploadedBytesAreTheArchive:
    def test_a_swap_before_the_upload_cannot_change_the_bytes_that_leave(
        self, _sessions_host, monkeypatch
    ):
        # The regression. On a name-based upload the planted file is what the CLI
        # opens, so bytes nothing checked leave the host unrecallably.
        swap = _run_with_swap(monkeypatch)
        assert swap.swapped, "the swap never fired, so this test proves nothing"
        assert swap.uploaded != PLANTED
        assert swap.uploaded[:2] == b"\x1f\x8b", "the upload did not carry a gzip archive"

    def test_the_uploaded_archive_still_holds_both_session_halves(
        self, _sessions_host, monkeypatch, tmp_path
    ):
        # Not merely "not the planted bytes": the bytes that left are the archive
        # the tar wrote, member for member. A refusal would also satisfy the test
        # above, and a refused nightly backup is its own failure.
        swap = _run_with_swap(monkeypatch)
        landed = tmp_path / "landed.tar.gz"
        landed.write_bytes(swap.uploaded)
        with tarfile.open(landed) as tar:
            assert sorted(tar.getnames()) == ["cli/replay.jsonl", "crew/t.jsonl"]

    def test_the_recorded_fingerprint_describes_the_bytes_that_left(
        self, _sessions_host, monkeypatch
    ):
        # The record's fingerprint and size describe the archive, not whatever the
        # name reaches when they are taken: from separate resolutions of one name the
        # record can describe the substitute while the entry-set digest that
        # authorized the upload describes the archive.
        # Asserted against the bytes read BEFORE the swap, which is the archive the
        # tar wrote: comparing the record to the uploaded bytes alone passes when
        # both read the substitute, which is the state this closes.
        swap = _run_with_swap(monkeypatch)
        record = backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]
        expected = hashlib.md5(swap.before_swap, usedforsecurity=False).hexdigest()  # noqa: S324
        assert swap.uploaded == swap.before_swap
        assert record["fingerprint"] == expected
        assert record["bytes"] == len(swap.before_swap)

    def test_a_swap_between_the_tar_closing_and_the_entry_read_changes_nothing(
        self, _sessions_host, monkeypatch
    ):
        # The entry-set digest is what DECIDES whether to upload, so it has to
        # describe the file that is then uploaded. This puts the substitution in
        # the window between the tar closing and that digest being taken: read
        # from the name, the digest describes the planted file -- which is not a
        # tarball, so it is empty; read from the descriptor, it describes the
        # archive.
        real_entries = backup._archive_entries
        fired: dict[str, Any] = {}

        def swapping_entries(archive=None, *, volatile_root, fd=None):
            if archive is not None and "real" not in fired and Path(archive).exists():
                here = Path(archive)
                fired["real"] = here.read_bytes()
                here.unlink()
                here.write_bytes(PLANTED)
            return real_entries(archive, volatile_root=volatile_root, fd=fd)

        monkeypatch.setattr(backup, "_archive_entries", swapping_entries)
        swap = _run_with_swap(monkeypatch)
        assert "real" in fired, "the swap never fired, so this test proves nothing"
        record = backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]
        # A digest taken from the planted file is empty, because it is not a
        # tar.gz -- so a non-empty digest is itself the evidence the read went
        # through the descriptor.
        assert record["tree"] != ""
        assert swap.uploaded == fired["real"]


class TestPutFileRefusesASubstitutedBody:
    """``put_file`` is the shared helper every backup kind uploads through.

    A helper that takes only a name cannot express "upload THIS file": the CLI
    resolves the name again. These pin the identity checks it makes on the inode it
    uploads, which is what protects the callers with no fingerprint of their own --
    the label sidecar and the library push.
    """

    @pytest.fixture(autouse=True)
    def _stub_cli(self, monkeypatch):
        self.seen: dict[str, Any] = {}

        def fake_checked(args, profile, *, action, timeout=30, extra_visible_dirs=(), **kw):
            self.seen["args"] = args
            self.seen["body"] = _body_the_child_would_read(args, kw.get("stdin_fd"))
            return "{}"

        monkeypatch.setattr(storage, "_checked", fake_checked)

    def test_a_symlink_at_the_name_is_refused_rather_than_followed(self, tmp_path):
        secret = tmp_path / "secret"
        secret.write_bytes(b"owner-only")
        link = tmp_path / "payload.bin"
        link.symlink_to(secret)
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW is unavailable, so the link cannot be refused at the open")
        with pytest.raises(storage.AWSError, match="not a regular file|link"):
            storage.put_file(
                "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(link), account=ACCOUNT
            )
        assert "body" not in self.seen

    def test_a_hard_link_is_refused_because_it_defeats_the_other_two_checks(self, tmp_path):
        # A hard link is a genuine regular file reached under the expected name,
        # so S_ISREG passes and O_NOFOLLOW has nothing to reject. The link COUNT
        # is the only thing that tells it from the file we staged.
        secret = tmp_path / "secret"
        secret.write_bytes(b"owner-only")
        linked = tmp_path / "payload.bin"
        os.link(secret, linked)
        with pytest.raises(storage.AWSError, match="more than one name|link"):
            storage.put_file(
                "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(linked), account=ACCOUNT
            )
        assert "body" not in self.seen

    def test_a_fifo_is_refused_before_any_byte_is_read(self, tmp_path):
        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFOs on this platform")
        fifo = tmp_path / "payload.bin"
        os.mkfifo(fifo)
        assert stat.S_ISFIFO(fifo.stat().st_mode)
        with pytest.raises(storage.AWSError, match="not a regular file"):
            storage.put_file(
                "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(fifo), account=ACCOUNT
            )
        assert "body" not in self.seen

    def test_an_ordinary_file_uploads_its_own_bytes(self, tmp_path):
        payload = tmp_path / "payload.bin"
        payload.write_bytes(b"real-payload")
        storage.put_file(
            "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(payload), account=ACCOUNT
        )
        assert self.seen["body"] == b"real-payload"

    def test_the_owner_pinning_and_the_size_ceiling_survive_the_descriptor_body(self, tmp_path):
        # The ceiling is measured on the DESCRIPTOR being uploaded, so the number
        # checked and the bytes sent cannot disagree -- a size read from the name is
        # the planted inode's size whenever one has been planted.
        payload = tmp_path / "payload.bin"
        payload.write_bytes(b"x" * 10)
        monkeypatched = 4
        with mock.patch.object(storage, "_MAX_PINNED_TRANSFER_BYTES", monkeypatched):
            with pytest.raises(storage.AWSError, match="exceeds the"):
                storage.put_file(
                    "p",
                    "us-west-2",
                    "bkt",
                    "backup",
                    "k/payload.bin",
                    str(payload),
                    account=ACCOUNT,
                )
        storage.put_file(
            "p", "us-west-2", "bkt", "backup", "k/payload.bin", str(payload), account=ACCOUNT
        )
        assert "--expected-bucket-owner" in self.seen["args"]
        assert self.seen["args"][self.seen["args"].index("--expected-bucket-owner") + 1] == ACCOUNT


class TestFingerprintsReadTheHeldInode:
    """Both digests describe the file the caller holds, not what its name reaches.

    They are what decides whether to upload at all and what the run record claims
    the object holds, so a digest taken from a re-resolved name can approve one
    file and record another. These replace the file at the name after the
    descriptor is open, which is the whole substitution in one step.
    """

    @staticmethod
    def _archive(tmp_path: Path) -> Path:
        path = tmp_path / "sessions.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("crew/t.jsonl")
            payload = b"transcript\n"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return path

    def test_the_entry_set_digest_survives_a_swap_at_the_name(self, tmp_path):
        path = self._archive(tmp_path)
        fd = os.open(path, os.O_RDONLY)
        try:
            from_descriptor_before = backup._tree_fingerprint(path, volatile_root=False, fd=fd)
            path.unlink()
            path.write_bytes(PLANTED)
            assert backup._tree_fingerprint(path, volatile_root=False, fd=fd) == (
                from_descriptor_before
            )
            # The name now reaches something that is not a tar.gz at all, which is
            # the reading the fingerprint would have taken from it.
            assert backup._tree_fingerprint(path, volatile_root=False) == ""
        finally:
            os.close(fd)

    def test_the_body_digest_survives_a_swap_at_the_name(self, tmp_path):
        path = tmp_path / "payload.bin"
        path.write_bytes(b"real-payload")
        fd = os.open(path, os.O_RDONLY)
        try:
            expected = hashlib.md5(b"real-payload", usedforsecurity=False).hexdigest()  # noqa: S324
            path.unlink()
            path.write_bytes(PLANTED)
            assert backup._body_fingerprint(fd=fd) == expected
            assert backup._body_fingerprint(path) != expected
        finally:
            os.close(fd)

    def test_reading_from_a_descriptor_leaves_its_position_alone(self, tmp_path):
        # The push paths hand ONE descriptor to the entry-set digest, the body
        # digest, the size and the upload in turn. A reader that consumed the
        # position would leave the next one with an empty file, so the upload
        # would send nothing and the record would still call it a success.
        path = self._archive(tmp_path)
        fd = os.open(path, os.O_RDONLY)
        try:
            backup._tree_fingerprint(path, volatile_root=False, fd=fd)
            backup._body_fingerprint(fd=fd)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 0
            assert _read_whole(fd) == path.read_bytes()
        finally:
            os.close(fd)


class TestPinnedStagingDirectory:
    """The archive is created relative to a held directory descriptor.

    A pre-planted entry at the archive's own name must fail the create rather
    than become the file the tar writes through, and the directory itself must
    not be reachable through a link.
    """

    def test_a_plain_file_already_at_the_archive_name_fails_the_create(self, tmp_path):
        # Only O_EXCL refuses this one. A pre-planted REGULAR file is not a link,
        # so O_NOFOLLOW has nothing to reject, and without the exclusive create the
        # tar would open it and write through it -- which is a file this process
        # did not stage becoming the object that leaves the host.
        directory = tmp_path / "staging"
        directory.mkdir()
        (directory / "archive.tar.gz").write_bytes(b"planted")
        dir_fd = platform_compat.pin_directory(directory)
        try:
            with pytest.raises(FileExistsError):
                backup._create_pinned_archive_fd(dir_fd, "archive.tar.gz")
            assert (directory / "archive.tar.gz").read_bytes() == b"planted"
        finally:
            os.close(dir_fd)

    def test_a_link_planted_at_the_archive_name_fails_the_create(self, tmp_path):
        target = tmp_path / "elsewhere"
        target.write_bytes(b"")
        directory = tmp_path / "staging"
        directory.mkdir()
        (directory / "archive.tar.gz").symlink_to(target)
        dir_fd = platform_compat.pin_directory(directory)
        try:
            with pytest.raises(OSError):
                backup._create_pinned_archive_fd(dir_fd, "archive.tar.gz")
        finally:
            os.close(dir_fd)

    def test_the_created_archive_is_ours_alone(self, tmp_path):
        directory = tmp_path / "staging"
        directory.mkdir()
        dir_fd = platform_compat.pin_directory(directory)
        try:
            fd = backup._create_pinned_archive_fd(dir_fd, "archive.tar.gz")
            try:
                info = os.fstat(fd)
                assert stat.S_ISREG(info.st_mode)
                assert info.st_nlink == 1
                assert stat.S_IMODE(info.st_mode) == 0o600
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
