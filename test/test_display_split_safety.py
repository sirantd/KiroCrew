"""``joins_to_a_credential`` / ``safe_split_offset`` -- the cut-safety primitive.

A message cap cuts RAW text while the reader sees the CANONICAL rendering of each
piece, so a credential the model split with markup can be severed by the cut:
each piece is scrubbed on its own and matches nothing, and the reader's client
renders the markup away and rejoins the halves. These pin the primitive that
decides where a cut may fall.
"""

from __future__ import annotations

import pytest

from conftest import CREDENTIAL_STRADDLE_SHAPES
from kiro_crew.messaging.display_safety import (
    CREDENTIAL_SEAM_TAG,
    break_credential_seam,
    canonicalize_display,
    joins_to_a_credential,
    redact_for_display,
    safe_split_offset,
)
from kiro_crew.messaging.renderer import _default_redactor, count_redaction_tags


class TestTheOracleIsAsStrongAsTheSendPath:
    """The cut is CHOSEN with one scrubber and the bytes are SENT through another.

    Every call site hands the oracle the bare ``_default_redactor``, while the outgoing
    slice is scrubbed with the render-aware ``Renderer.redact_for_target``, which is
    ``redact_for_display`` wrapped around that same redactor. Choosing a cut with a WEAKER
    scrubber than the one the bytes are rendered through would approve a cut whose halves
    the send path then leaves intact, so the two must agree.

    They do, because the oracle canonicalizes each reading BEFORE scrubbing it -- it earns
    the display-awareness internally instead of being handed it. That is invisible at the
    call sites, and a future edit that scrubbed raw text inside either primitive would
    break it silently. Hence this test.
    """

    @staticmethod
    def _display_aware(text: str) -> str:
        # What the send path scrubs with, as a plain function.
        safe, _ = redact_for_display(text, _default_redactor)
        return safe

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_both_scrubbers_decide_the_same_join(self, head: str, tail: str) -> None:
        assert joins_to_a_credential(head, tail, _default_redactor) == joins_to_a_credential(
            head, tail, self._display_aware
        ), "the oracle disagreed with the scrubber the bytes are sent through"

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_the_offsets_do_not_depend_on_which_scrubber_decides(
        self, head: str, tail: str
    ) -> None:
        # The primitive reaches the redactor ONLY through the oracle, so the agreement
        # above has to carry through to the offsets it returns.
        text = head + tail
        for limit in (len(head), len(text), len(text) // 2):
            assert safe_split_offset(text, limit, _default_redactor) == safe_split_offset(
                text, limit, self._display_aware
            ), "a cut offset changed with the scrubber"


class TestRedactionIsAFixedPoint:
    """The keystone `joins_to_a_credential` rests on, pinned on its own.

    The oracle reads a join twice -- canonicalize-then-scan, and scan-each-side-then-join
    -- and both readings assume that once a piece is scrubbed, scrubbing it again is a
    no-op EVEN AFTER the reader's client has rendered the markup away. Nothing else in
    these tests says so out loud, so a change to the tag or to canonicalization could
    quietly turn a scrubbed piece back into something that scans, and every caller that
    inserts the tag (the terminal seam breaker most of all) would be resting on sand.
    """

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_scrubbing_a_scrubbed_piece_changes_nothing(self, head: str, tail: str) -> None:
        # Each side alone, the join, and the canonical join: every piece a caller can
        # hand the oracle after a scrub.
        for piece in (head, tail, head + tail, canonicalize_display(head + tail)):
            once, _ = redact_for_display(piece, _default_redactor)
            settled = _default_redactor(canonicalize_display(once))
            twice, _ = redact_for_display(once, _default_redactor)

            assert settled == canonicalize_display(
                once
            ), "a scrubbed piece scanned again after canonicalization"
            assert twice == once, "scrubbing a scrubbed piece was not a no-op"


class TestJoinsToACredential:
    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_a_severed_key_is_reported(self, head: str, tail: str) -> None:
        # Premise first: neither half is a credential ALONE, which is exactly why
        # scrubbing each piece cannot see this and the CUT is what has to be right.
        # Asserted so a fixture that stops straddling fails loudly instead of
        # passing on a case it does not exercise.
        assert _default_redactor(head) == head, "the head half must be clean alone"
        assert _default_redactor(tail) == tail, "the tail half must be clean alone"

        assert joins_to_a_credential(head, tail, _default_redactor)

    @pytest.mark.parametrize(
        ("head", "tail"),
        [
            pytest.param("plain prose ending here", " and continuing there", id="prose"),
            pytest.param("emphasis **spanning", " the cut** is harmless", id="emphasis-span"),
            pytest.param("a [link](https://ex.test/a,b) then", " more prose", id="whole-link"),
            pytest.param("", "AKIAIOSFODNN7EXAMPLE is scrubbed here", id="key-wholly-in-tail"),
            pytest.param("AKIAIOSFODNN7EXAMPLE is scrubbed here", "", id="key-wholly-in-head"),
        ],
    )
    def test_a_harmless_cut_is_allowed(self, head: str, tail: str) -> None:
        # The allow direction. A key that lies wholly inside one side is redacted by
        # that side's own pass, so it must NOT be reported here -- reporting it would
        # walk the cut back for a boundary that severs nothing, and a guard that
        # refuses everything delivers nothing.
        assert not joins_to_a_credential(head, tail, _default_redactor)

    def test_a_cut_that_closes_a_link_is_reported(self) -> None:
        # Caught ONLY by canonicalising the concatenation: each half alone is an
        # unfinished link, and only together do they form a link that collapses to
        # its label, putting the two halves of the key side by side.
        head = "[AKIA](https://ex.test/a,b"
        tail = ")IOSFODNN7EXAMPLE"
        assert joins_to_a_credential(head, tail, _default_redactor)

    def test_a_cut_inside_a_link_target_is_reported(self) -> None:
        # Caught ONLY by canonicalising each side and then joining. Completing the
        # link makes the concatenation collapse to the label, so the key inside the
        # URL disappears from that reading -- while on screen each half is an
        # unfinished link whose URL text stays visible, and the reader reads through.
        head = "[l](https://ex.test/x/AKIAIOSF"
        tail = "ODNN7EXAMPLE)"
        assert joins_to_a_credential(head, tail, _default_redactor)

    @pytest.mark.parametrize(
        ("head", "tail", "seen_by_the_join"),
        [
            pytest.param(
                "[AKIA](https://ex.test/a,b", ")IOSFODNN7EXAMPLE", True, id="cut-closes-a-link"
            ),
            pytest.param(
                "[l](https://ex.test/x/AKIAIOSF", "ODNN7EXAMPLE)", False, id="cut-inside-a-url"
            ),
        ],
    )
    def test_neither_reading_of_a_join_contains_the_other(
        self, head: str, tail: str, seen_by_the_join: bool
    ) -> None:
        # Why BOTH readings are scanned rather than one. Canonicalising the
        # concatenation is the wider reading for delimiter runs, which concatenation
        # can only extend; canonicalising each side first is wider wherever
        # canonicalising DROPS text, which is what a link does to its target. Each
        # shape here is found by exactly one reading, so dropping either reading
        # ships that shape. Pinned so the day one reading starts covering the other,
        # CI says so instead of the guard quietly narrowing.
        head_safe = redact_for_display(head, _default_redactor)[0]
        tail_safe = redact_for_display(tail, _default_redactor)[0]
        joined = canonicalize_display(head_safe + tail_safe)
        on_screen = canonicalize_display(head_safe) + canonicalize_display(tail_safe)

        assert (_default_redactor(joined) != joined) is seen_by_the_join
        assert (_default_redactor(on_screen) != on_screen) is not seen_by_the_join


class TestSafeSplitOffset:
    def test_prose_cuts_at_the_limit(self) -> None:
        text = "just some ordinary prose with nothing secret in it at all"
        assert safe_split_offset(text, 20, _default_redactor) == 20

    def test_text_within_the_limit_is_not_cut(self) -> None:
        text = "short"
        assert safe_split_offset(text, 999, _default_redactor) == len(text)

    def test_a_non_positive_limit_yields_nothing(self) -> None:
        assert safe_split_offset("anything", 0, _default_redactor) == 0

    def test_the_offset_moves_back_off_a_severed_key(self) -> None:
        head, tail = "[AKIA](https://ex.test/a,b)", "IOSFODNN7EXAMPLE"
        pad = "x" * 40
        text = pad + head + tail + " tail prose"
        limit = len(pad) + len(head)

        offset = safe_split_offset(text, limit, _default_redactor)

        assert 0 < offset <= len(pad), "the cut must land before the key begins"
        assert not joins_to_a_credential(text[:offset], text[offset:], _default_redactor)

    def test_the_search_is_logarithmic_not_linear(self) -> None:
        # The cost bound is the reason the candidates step back exponentially: this
        # runs on attacker-influenced text on every outgoing frame. Counting the
        # redaction passes is what pins it -- a linear walk would take ~2000 here.
        calls = 0

        def counting_redactor(text: str) -> str:
            nonlocal calls
            calls += 1
            return _default_redactor(text)

        key = "AKIAIOSFODNN7EXAMPLE"
        text = "x" * 2000 + key[:8] + key[8:] + " tail prose"
        limit = 2000 + 8

        offset = safe_split_offset(text, limit, counting_redactor)

        assert offset <= 2000
        # Four candidates (the limit, then 1, 2, 4, 8 back) at a handful of passes
        # each. The ceiling is deliberately loose: the property is the ORDER, and a
        # linear walk cannot fit under it.
        assert calls < 60, calls


class TestBreakCredentialSeam:
    """The other half of the cut: the message ABOVE is already frozen.

    ``safe_split_offset`` decides where a cut may fall, which is available only to
    whoever makes the cut. Once the message above is sent it cannot be taken back,
    so a later writer that replaces the text BELOW it owns the boundary and has
    only that text left to fix.
    """

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_every_straddle_shape_is_closed(self, head: str, tail: str) -> None:
        # Premise: each side is clean alone, which is why neither message's own
        # scan can see this.
        assert _default_redactor(head) == head, "the head half must be clean alone"
        assert _default_redactor(tail) == tail, "the tail half must be clean alone"

        safe, broken = break_credential_seam(head, tail, _default_redactor)

        assert broken, "an open seam must report that text was withheld"
        assert not joins_to_a_credential(
            head, safe, _default_redactor
        ), f"the reader can still rejoin a key: {safe!r}"

    @pytest.mark.parametrize(
        ("prior", "text"),
        [
            pytest.param("plain prose ending here", "and continuing there", id="prose"),
            pytest.param("", "AKIAIOSFODNN7EXAMPLE alone", id="no-message-above"),
            pytest.param("AKIAIOSFODNN7EXAMPLE alone", "", id="nothing-below"),
            pytest.param("ends in AKIAIOSF", "a whole paragraph of prose", id="half-key-above"),
        ],
    )
    def test_a_closed_seam_is_left_alone(self, prior: str, text: str) -> None:
        # The allow direction, and it carries the cost argument: a guard that
        # rewrites every message delivers a redaction tag on ordinary prose.
        assert break_credential_seam(prior, text, _default_redactor) == (text, False)

    def test_the_tag_alone_can_be_absorbed_so_the_search_keeps_going(self) -> None:
        # Inserting the tag is the FIRST candidate, not the answer. Here the text
        # below opens with the ``)`` that closes a link the message above left
        # open, so the tag lands inside the link target and the join collapses it
        # away -- putting the key's halves side by side again.
        prior, text = "[AKIA](https://ex.test/a,b", ")IOSFODNN7EXAMPLE"
        absorbed = CREDENTIAL_SEAM_TAG + text
        assert joins_to_a_credential(
            prior, absorbed, _default_redactor
        ), "premise: inserting the tag alone does not close this seam"

        safe, broken = break_credential_seam(prior, text, _default_redactor)

        assert broken
        assert safe.startswith(CREDENTIAL_SEAM_TAG)
        assert not joins_to_a_credential(prior, safe, _default_redactor)
        assert ")" not in safe, "the character that closes the link must be withheld"

    def test_the_withheld_run_is_reported_as_a_redaction(self) -> None:
        # The tag is the redactor's own, so the channel's existing per-message
        # tally counts it and the turn's notice tells the user something was held
        # back. A private marker would be a silent gap.
        safe, _ = break_credential_seam("ends in AKIAIOSF", "ODNN7EXAMPLE rest", _default_redactor)
        assert count_redaction_tags(safe)[0] == 1, safe

    def test_a_closed_seam_costs_one_scan(self) -> None:
        # The cost claim the sinks rest on: this runs on every outgoing frame, and
        # every ordinary frame has a clean seam. One ``joins_to_a_credential`` is
        # four redaction passes over prose -- each side once, then the two
        # readings -- and nothing walks.
        calls = 0

        def counting_redactor(text: str) -> str:
            nonlocal calls
            calls += 1
            return _default_redactor(text)

        prior = "a whole paragraph of ordinary prose, ending in a full stop."
        text = "another paragraph, carrying on from it with nothing secret."

        assert break_credential_seam(prior, text, counting_redactor) == (text, False)
        assert calls <= 6, calls

    def test_nothing_of_the_text_survives_when_nothing_else_works(self) -> None:
        # The last candidate is the tag alone, which cannot extend a key: it is a
        # fixed point of the scan, and the message above was scrubbed on its own
        # before it was sent.
        safe, broken = break_credential_seam("ends in AKIAIOSF", "ODNN7EXAMPLE", _default_redactor)
        assert broken
        assert not joins_to_a_credential("ends in AKIAIOSF", safe, _default_redactor)
