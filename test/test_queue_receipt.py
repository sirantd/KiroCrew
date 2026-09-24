"""The shared mid-turn queue receipt: lifecycle, lock contract, and a ratchet.

Telegram and Discord grew this subsystem independently and kept ~560 duplicated
lines of it. The channel-neutral half now lives in
``messaging/queue_receipt.py``; these tests pin that channel-neutral behaviour
once, and add the
mechanism that stops a third channel from starting a third copy.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import kiro_crew.messaging.queue_receipt as Q
from kiro_crew.messaging.queue_receipt import RECEIPT_MAX_ITEMS, ReceiptQueue, receipt_text


class _Surface:
    """Records what a channel would have put on the wire."""

    label = "fake"

    def __init__(
        self,
        *,
        send_id: Any = 7,
        edit_raises: bool = False,
        edit_refuses: bool = False,
        edit_answers_none: bool = False,
        send_fails_after: int | None = None,
    ) -> None:
        self._send_id = send_id
        self.edit_raises = edit_raises
        #: Report the refusal a rate-limited chat or a spent edit cap really answers.
        self.edit_refuses = edit_refuses
        #: Answer None: silence, which is not a reported failure.
        self.edit_answers_none = edit_answers_none
        self._send_fails_after = send_fails_after
        self.sent: list[str] = []
        self.edits: list[tuple[Any, str]] = []

    async def send_receipt(self, body: str) -> Any | None:
        self.sent.append(body)
        if self._send_fails_after is not None and len(self.sent) > self._send_fails_after:
            return None
        return self._send_id

    async def edit_receipt(self, msg_id: Any, body: str) -> bool | None:
        self.edits.append((msg_id, body))
        if self.edit_raises:
            raise RuntimeError("edit failed mid-flush")
        if self.edit_answers_none:
            return None
        return not self.edit_refuses


class TestReceiptText:
    def test_queued_grows_with_the_count(self) -> None:
        assert receipt_text(["a"]).startswith("⏳ Queued (1):")
        assert receipt_text(["a", "b"]).startswith("⏳ Queued (2):")

    def test_past_the_cap_the_tail_is_summarised_not_dropped(self) -> None:
        texts = [f"m{i}" for i in range(RECEIPT_MAX_ITEMS + 3)]
        out = receipt_text(texts)
        # The count is the TRUE total even though only the cap is listed.
        assert f"({len(texts)})" in out
        assert "…and 3 more" in out

    def test_the_three_states_are_distinguishable(self) -> None:
        assert "Now answering" in receipt_text(["a"], answering=True)
        assert "Cancelled" in receipt_text(["a"], cancelled=True)


class TestLifecycle:
    def test_create_then_grow_edits_one_bubble(self) -> None:
        q, s = ReceiptQueue(), _Surface(send_id=42)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "first")
                await q.create_or_grow_locked("s", s, "second")

        asyncio.run(go())
        assert len(s.sent) == 1, "a second message would orphan the first bubble"
        assert s.edits == [(42, receipt_text(["first", "second"]))]

    def test_flip_drops_the_entry_so_the_next_burst_opens_a_fresh_bubble(self) -> None:
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                assert not q.has_receipt("s")
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert len(s.sent) == 2, "post-flip burst must start a NEW receipt"

    def test_deferred_remainder_is_stated_not_implied(self) -> None:
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"], deferred=4)

        asyncio.run(go())
        assert "+4 deferred" in s.edits[-1][1]

    def test_cancel_finalises_with_the_full_queued_list(self) -> None:
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.create_or_grow_locked("s", s, "b")
                await q.finish_cancelled_locked("s", s)

        asyncio.run(go())
        assert "Cancelled (2)" in s.edits[-1][1]
        assert not q.has_receipt("s")

    def test_a_failing_edit_never_escapes(self) -> None:
        """Receipt upkeep is cosmetic; it must not fail the turn around it."""
        q, s = ReceiptQueue(), _Surface(edit_raises=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.create_or_grow_locked("s", s, "b")  # edit raises
                await q.flip_answering_locked("s", s, ["a", "b"])  # raises too

        asyncio.run(go())  # must not raise

    def test_a_send_that_returns_no_id_records_no_receipt(self) -> None:
        """No id means no bubble to edit later -- storing one would 404 forever."""
        q, s = ReceiptQueue(), _Surface(send_id=None)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")

        asyncio.run(go())
        assert not q.has_receipt("s")


class TestATransitionIsPublishedOnlyWhenItLands:
    """A refused edit is an ordinary answer, not an exception, and not a success."""

    def test_a_refused_flip_keeps_the_bubbles_only_handle(self) -> None:
        q, s = ReceiptQueue(), _Surface(send_id=42, edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])

        asyncio.run(go())
        # Dropped here, nothing would ever revisit the bubble: it reads "Queued" for good.
        assert q._receipts["s"].owes_record
        assert q._receipts["s"].final_body == receipt_text(["a"], answering=True)
        # Not LIVE though -- it cannot be grown.
        assert not q.has_receipt("s")

    def test_a_refused_grow_leaves_no_record_owed(self) -> None:
        """That message is still QUEUED, which is what the bubble's ledger says."""
        q, s = ReceiptQueue(), _Surface(edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert q.has_receipt("s"), "nothing has left the queue, so the bubble is still live"
        assert q._receipts["s"].texts == ["a", "b"]

    def test_silence_is_not_a_reported_failure(self) -> None:
        """A surface answering None has not reported one; reading it as failure would
        keep every receipt in the registry for good."""
        q, s = ReceiptQueue(), _Surface(edit_answers_none=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])

        asyncio.run(go())
        assert "s" not in q._receipts

    def test_a_raise_and_a_refusal_are_the_same_answer(self) -> None:
        q, s = ReceiptQueue(), _Surface(edit_raises=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])

        asyncio.run(go())
        assert q._receipts["s"].owes_record

    def test_a_refused_cancel_keeps_the_handle_too(self) -> None:
        q, s = ReceiptQueue(), _Surface(edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.finish_cancelled_locked("s", s)

        asyncio.run(go())
        assert q._receipts["s"].final_body == receipt_text(["a"], cancelled=True)


class TestAnOwedRecordIsTheOneThatWasOwed:
    """The record travels with the entry, so a retry writes what actually happened."""

    def test_the_next_message_writes_the_owed_record_before_opening_a_bubble(self) -> None:
        q = ReceiptQueue()
        s = _Surface(send_id=42, edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])  # refused, now terminal
                s.edit_refuses = False
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        owed = receipt_text(["a"], answering=True)
        assert s.edits[-1] == (42, owed), "the retry writes the record the flip owed"
        assert len(s.sent) == 2, "and then a FRESH bubble, not a grow of the answered one"
        assert s.sent[-1] == receipt_text(["b"])

    def test_a_terminal_entry_is_never_grown(self) -> None:
        """Growing it would put already-answered text back under "Queued"."""
        q, s = ReceiptQueue(), _Surface(edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                s.edit_refuses = False
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert all("a" not in body for body in s.sent[1:]), "answered text must not return"

    def test_the_key_is_kept_until_the_record_is_on_the_bubble(self) -> None:
        q = ReceiptQueue()
        # The edit stays refused AND the record cannot be posted either.
        s = _Surface(send_id=42, edit_refuses=True, send_fails_after=1)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert q._receipts["s"].owes_record, "the record is still owed, so the key is held"
        assert q._receipts["s"].final_body == receipt_text(["a"], answering=True)

    def test_a_bubble_that_refuses_edits_gets_the_record_posted(self) -> None:
        """Past Webex's per-message edit cap no edit of that id ever lands, so retrying
        alone would owe the record for the life of the process."""
        q = ReceiptQueue()
        s = _Surface(send_id=42, edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        owed = receipt_text(["a"], answering=True)
        assert owed in s.sent, "the record is POSTED when the bubble will not take it"
        assert "s" in q._receipts and not q._receipts["s"].owes_record

    def test_a_stop_does_not_recompute_a_record_another_transition_owes(self) -> None:
        """Writing "Cancelled" over an owed "Now answering" says the opposite of what
        happened, permanently."""
        q = ReceiptQueue()
        s = _Surface(send_id=42, edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])  # owes "Now answering"
                s.edit_refuses = False
                await q.finish_cancelled_locked("s", s)

        asyncio.run(go())
        assert s.edits[-1][1] == receipt_text(["a"], answering=True)
        assert "Cancelled" not in s.edits[-1][1]
        assert "s" not in q._receipts

    def test_a_second_flip_retries_the_owed_record_rather_than_its_own(self) -> None:
        q = ReceiptQueue()
        s = _Surface(send_id=42, edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                s.edit_refuses = False
                await q.flip_answering_locked("s", s, ["zzz"])

        asyncio.run(go())
        assert s.edits[-1][1] == receipt_text(["a"], answering=True)
        assert "zzz" not in s.edits[-1][1]


class TestLockIsCallerHeld:
    def test_the_transitions_do_not_take_the_lock_themselves(self) -> None:
        """The atomicity contract, asserted rather than documented.

        Callers hold the lock ACROSS enqueue+receipt (and dequeue+flip), which is
        what makes the subsystem race-free against the drain. If a future change
        moved the acquire inside these methods, this deadlocks -- so the bounded
        wait is the assertion, not a timeout guard.
        """
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await asyncio.wait_for(q.create_or_grow_locked("s", s, "a"), timeout=2)
                await asyncio.wait_for(q.flip_answering_locked("s", s, ["a"]), timeout=2)
                await asyncio.wait_for(q.finish_cancelled_locked("s", s), timeout=2)

        asyncio.run(go())


def _dispatchers() -> list[Path]:
    pkg = Path(Q.__file__).resolve().parent.parent
    found = sorted(pkg.glob("*/transport_dispatch.py"))
    assert len(found) >= 5, f"expected the dispatcher set, found {found}"
    return found


class TestRatchet:
    def test_no_channel_keeps_its_own_receipt_registry_or_lock(self) -> None:
        """A third copy of this subsystem must fail here, not in production."""
        offenders: dict[str, list[str]] = {}
        for path in _dispatchers():
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            names = {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and node.attr
                in {
                    "_queue_receipts",
                    "_receipt_lock",
                }
            }
            if names:
                offenders[path.parent.name] = sorted(names)
        assert not offenders, (
            "these channels carry a private receipt registry/lock instead of the "
            f"shared ReceiptQueue, so the lock discipline can drift again: {offenders}"
        )

    def test_every_channel_with_a_queue_uses_the_shared_one(self) -> None:
        missing = []
        for path in _dispatchers():
            src = path.read_text(encoding="utf-8")
            if "_enqueue_with_receipt" in src and "ReceiptQueue" not in src:
                missing.append(path.parent.name)
        assert not missing, f"{missing} implement a mid-turn queue without the shared ReceiptQueue"
