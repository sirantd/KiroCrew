"""Mid-turn queue receipts — the single collapsing "⏳ Queued (N): …" bubble.

A message that arrives while a turn is running is either folded into that turn
as a steer or queued for after it. Queued messages get ONE receipt bubble that
is edited in place as the burst grows, then flipped to a durable record when the
turn drains it ("▶️ Now answering") or cancelled ("🛑 Cancelled"). Two channels
grew the same subsystem independently -- Telegram and Discord, ~560 duplicated
lines -- and this module is the half of it that is genuinely channel-neutral.

What lives here and what does NOT:

* HERE -- the receipt registry, its lock, and the three lifecycle transitions
  (create/grow, flip-to-answering, finalize-cancelled). These are pure
  bookkeeping over an opaque message id, and every line of them was identical
  across the two channels apart from the address type and the send call.
* NOT here -- ``_handle_busy`` and ``_drain_queue``. They re-enter the channel's
  own ``handle_message`` (whose signature differs per channel: route/chat_id/
  thread vs user_id/channel_id/thread_id) and they own the ``_active_renderers``
  registry. Sharing them would need a ``run_turn`` callback that buys nothing
  and couples this module to turn execution.

Channels reach the transitions through :class:`ReceiptSurface`, whose address is
bound at CONSTRUCTION -- so nothing below ever sees a ``chat_id``, a ``thread``
or a ``channel_id``, which is what let the five address-shaped divergences
between the two copies collapse to zero.

Dependency direction is ``<channel> -> messaging`` (never the reverse), matching
``messaging/dispatch.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Verbatim items shown in a receipt before "…and N more". A large mid-turn
#: burst would otherwise grow the rendered receipt past a channel's message
#: limit; the count prefix still reflects the true total.
RECEIPT_MAX_ITEMS = 5

#: Instant, no-extra-bubble acknowledgement that a mid-turn steer was accepted
#: and folded into the running turn (not merely "seen" — 👀 reads as passive).
STEER_ACK_EMOJI = "🫡"

#: Upper bound on how many queued messages collapse into a single combined turn.
#: A single human will not realistically burst past this mid-turn; anything beyond
#: stays queued and drains after the next turn. Lives here rather than in each
#: dispatcher because it bounds the same collapse in every channel that carries
#: the queue, and three copies had already drifted apart by comment alone.
MAX_COLLAPSE = 50

#: What a receipt SHOWS for a message whose only content was an upload. An
#: attachment-only message has no text, and a blank line in the bubble reads as a
#: message the queue lost. Lives here because every channel that ingests files
#: needs the same substitution in both receipt transitions.
ATTACHMENT_PLACEHOLDER = "[attachment]"


def short(text: str, limit: int = 40) -> str:
    """Collapse whitespace and truncate for compact receipt display."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def receipt_text(
    texts: list[str],
    *,
    answering: bool = False,
    cancelled: bool = False,
) -> str:
    """Render the single collapsing receipt for ``texts`` (order preserved).

    Only the first :data:`RECEIPT_MAX_ITEMS` are listed verbatim; the count
    prefix still reflects the true total.
    """
    count = len(texts)
    items = " · ".join(f"“{short(t)}”" for t in texts[:RECEIPT_MAX_ITEMS])
    if count > RECEIPT_MAX_ITEMS:
        items += f" · …and {count - RECEIPT_MAX_ITEMS} more"
    if cancelled:
        return f"🛑 Cancelled ({count}): {items}"
    if answering:
        return f"▶️ Now answering ({count}): {items}"
    return f"⏳ Queued ({count}): {items}"


@dataclass(frozen=True)
class ReceiptLine:
    """One queued message as the receipt lists it: whose it is, and what it shows.

    The owner is the token :func:`kiro_crew.messaging.queue_drain.owner_token` builds,
    the same value the queue entry itself carries, so the bubble and the queue agree
    about who queued what. Empty for a producer that cannot name its principal, which
    makes the line nobody's to withdraw.
    """

    owner: str
    text: str


@dataclass
class QueueReceipt:
    """The single, in-place receipt bubble tracking messages queued mid-turn.

    ``msg_id`` is deliberately opaque (``Any``): Telegram message ids are ints
    and Discord's are strings, the two are never interleaved in one process, and
    a generic parameter would add ceremony without catching a real mixup -- the
    id is only ever handed straight back to the surface that produced it.

    One registry entry serves a whole session key, and under
    ``messaging.dm_scope = "unified"`` that key spans several chats, so the
    lines on one bubble can belong to several principals while ``msg_id``
    addresses a message in exactly one of their conversations --
    ``opened_by``'s. Two rules follow from that pairing: a transition may only
    render lines back to the principal they came from (:meth:`withdraw` returns
    exactly the caller's own), and only ``opened_by`` may be handed this
    ``msg_id``, because in anybody else's conversation the same number is
    another message.

    ``opened_on`` is what makes the second rule enforceable rather than merely
    stated. The surface a transition is handed is built from the ARRIVING
    message, so under a shared key it addresses whoever spoke last -- not
    necessarily this bubble's chat. Every write that targets ``msg_id`` is
    therefore addressed to the bubble rather than to the caller, and the four
    transitions reach that in three different ways, which is worth naming because
    only one of them is a field lookup:

    * an owed record, and a whole-session cancel that names no principal, read
      ``opened_on`` outright -- neither can assume its caller is the opener;
    * the end-of-turn flip is already the bubble's chat by construction: the drain
      takes the address from the queued entry's own origin, and the bubble was
      posted into the chat of whoever queued first;
    * a caller-scoped cancel is already the opener's by its ``opened_by`` guard,
      and writes nothing when the caller is somebody else.

    A grow by a different principal is the one case with no correct address at all
    -- the caller's chat holds no bubble -- so it records the line and writes
    nothing. That is safe only because a grow owes no record: the message is still
    queued, and the next render shows the whole list. ``opened_on`` also decides
    where a fallback POST lands, which matters more than any edit: the owed record
    quotes ``opened_by``'s own text, so the one conversation it may appear in is
    theirs.
    """

    msg_id: Any
    opened_by: str = ""
    #: The surface the bubble was OPENED on -- the only address ``msg_id`` is
    #: valid in, and the only one an owed record may be written or posted
    #: through. Held for the entry's life, which a surface supports: it is a
    #: closure over its channel's long-lived client plus the bound address, and
    #: carries no per-request state. ``None`` only for an entry built without
    #: one, which cannot be written to at all rather than falling back to a
    #: caller's surface.
    opened_on: ReceiptSurface | None = None
    lines: list[ReceiptLine] = field(default_factory=list)
    #: The FINAL record this bubble still owes, set when that edit did not land.
    #:
    #: An entry carrying one is TERMINAL: its messages have already left the queue,
    #: so it is not grown and not flipped again -- the next mid-turn message opens a
    #: fresh bubble rather than putting answered text back under "Queued". The body
    #: travels WITH the entry because the record owed is the one that transition
    #: computed; recomputing it later would write whatever the later transition
    #: happened to be instead.
    final_body: str | None = None

    @property
    def owes_record(self) -> bool:
        """Whether this entry is terminal, still owing a record it could not write."""
        return self.final_body is not None

    @property
    def texts(self) -> list[str]:
        """What the bubble shows, in order. What :func:`receipt_text` renders."""
        return [line.text for line in self.lines]

    def withdraw(self, owner: str) -> list[str]:
        """Drop *owner*'s lines and return what they showed, in order.

        An empty *owner* drops nothing, matching the queue-side predicate: a caller that
        cannot name its principal withdraws nothing rather than everybody's lines.
        """
        if not owner:
            return []
        taken = [line.text for line in self.lines if line.owner == owner]
        if taken:
            self.lines = [line for line in self.lines if line.owner != owner]
        return taken


class ReceiptSurface(Protocol):
    """One conversation's receipt bubble, with its address already bound.

    Implementations close over whatever addresses their channel needs (Telegram
    binds ``chat_id`` AND the forum ``thread``; Discord binds ``channel_id``), so
    forum routing and channel addressing stay entirely channel-local.
    """

    #: Channel name for log lines only ("telegram" / "discord").
    label: str

    async def send_receipt(self, body: str) -> Any | None:
        """Post a new receipt bubble. Returns an opaque message id, or None."""

    async def edit_receipt(self, msg_id: Any, body: str) -> bool:
        """Rewrite the receipt in place. Returns whether the edit LANDED.

        A channel client answers a refusal with ``False`` rather than an exception:
        a rate-limited chat, or a bubble past the per-message edit cap Webex
        documents, is an ordinary non-2xx answer. Returning it is what lets the
        registry tell "the bubble now shows this" from "the bubble still shows the
        old text", which is the difference between a durable record and a bubble
        stranded reading "⏳ Queued".

        An implementation that cannot tell may return ``None``; that is silence,
        not a reported failure, and :meth:`ReceiptQueue._edit` treats it as landed.
        May also raise, which IS a reported failure.
        """


class ReceiptQueue:
    """Owns the per-session receipt registry, its lock, and the transitions.

    The lock is deliberately CALLER-HELD and exposed as :attr:`lock` rather than
    taken inside each method. Holding it across BOTH the enqueue and the receipt
    bookkeeping is what makes the subsystem race-free against the end-of-turn
    drain, which takes the same lock across dequeue + flip: the drain either sees
    a message queued WITH its receipt or sees neither yet -- never a half state
    that would orphan a bubble. ``/stop`` holds it across clear_queue + finalize
    for the same reason. Hiding the lock inside these methods would silently
    reintroduce that race, which is why the ``_locked`` suffixes stay in the
    public names: ugly, and load-bearing.
    """

    def __init__(self) -> None:
        self._receipts: dict[str, QueueReceipt] = {}
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """The lock callers MUST hold across compound operations (see class doc)."""
        return self._lock

    def has_receipt(self, session_key: str) -> bool:
        """Whether a LIVE receipt exists for this session.

        An entry still owing a record is terminal, not live: it cannot be grown, and
        the next mid-turn message opens a fresh bubble rather than joining it.
        """
        receipt = self._receipts.get(session_key)
        return receipt is not None and not receipt.owes_record

    async def create_or_grow_locked(
        self,
        session_key: str,
        surface: ReceiptSurface,
        display_text: str,
        owner: str = "",
    ) -> None:
        """Create the receipt, or append to it and edit in place.

        ``display_text`` is what the receipt SHOWS, which is not always the raw
        message: a file-capable channel substitutes :data:`ATTACHMENT_PLACEHOLDER`
        for an attachment-only message so the bubble is not blank. Caller MUST hold
        :attr:`lock`, and MUST have already enqueued the message under that same
        hold.

        ``owner`` is who queued this one line -- the same token the queue entry carries
        -- so a later ``/stop`` for one principal can withdraw that person's lines and
        leave the rest alone. Pass the value the entry was tagged with; empty means the
        line is nobody's to withdraw.
        """
        receipt = self._receipts.get(session_key)
        line = ReceiptLine(owner=owner, text=display_text)
        if receipt is not None and receipt.owes_record:
            # Terminal: those messages already left the queue. Growing it would put
            # answered text back under "Queued" beside this new one, so the record it
            # owes is written first and the key released only once that lands -- this
            # entry is that bubble's only handle. The write goes to the bubble's OWN
            # conversation, not this caller's: under a shared key the arriving message
            # may be a different principal's, and the record quotes the opener's text.
            if await self._write_record(receipt):
                del self._receipts[session_key]
                receipt = None
            else:
                return
        if receipt is None:
            msg_id = await surface.send_receipt(receipt_text([display_text]))
            if msg_id is not None:
                self._receipts[session_key] = QueueReceipt(
                    msg_id=msg_id, opened_by=owner, opened_on=surface, lines=[line]
                )
            return
        receipt.lines.append(line)
        if owner != receipt.opened_by:
            # A different principal under a shared key. There is no address this edit
            # could use: ``msg_id`` names a message in the opener's chat and nowhere
            # else, while THIS caller's chat holds no bubble, so editing through the
            # caller would rewrite whatever unrelated message happens to hold that
            # number there. Recording the line and writing nothing is safe because a
            # grow owes no record: the message is still QUEUED, which is what
            # ``lines`` tracks, and the next same-principal grow or the flip renders
            # the whole list. Which conversation a shared bubble belongs to is the
            # registry key's own question, not this one's.
            return
        # A refused grow needs no record and no terminal state: this message is still
        # QUEUED, which is exactly what ``lines`` tracks, so the registry and the queue
        # still agree and the next message's edit re-renders the whole list. Only a
        # transition whose messages have already LEFT the queue can strand a bubble.
        await self._edit(surface, receipt.msg_id, receipt_text(receipt.texts))

    async def flip_answering_locked(
        self,
        session_key: str,
        surface: ReceiptSurface,
        answered: list[str],
        deferred: int = 0,
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record.

        Drops the live entry so the next mid-turn burst opens a fresh receipt.
        ``answered`` is the subset this turn actually answers (the drain caps it),
        so a burst past the cap does not overstate the turn; ``deferred`` (>0 only
        past the cap) is noted so the remainder is not silently implied. Caller
        MUST hold :attr:`lock` across dequeue + this call.
        """
        receipt = self._receipts.pop(session_key, None)
        if receipt is None:
            return
        if receipt.owes_record:
            # Already terminal from an earlier refused transition. Retry THAT record --
            # recomputing it here would write this transition's words over what actually
            # happened -- through the bubble's own conversation, and keep the entry
            # until it lands.
            if not await self._write_record(receipt):
                self._receipts[session_key] = receipt
            return
        body = receipt_text(answered, answering=True)
        if deferred:
            body += f" · +{deferred} deferred"
        if not await self._edit(surface, receipt.msg_id, body):
            # These messages have LEFT the queue, so nothing else will ever revisit this
            # bubble: dropped now it reads "⏳ Queued" for good. Kept, it is terminal and
            # carries the record it owes, which the next transition writes.
            receipt.final_body = body
            self._receipts[session_key] = receipt

    async def finish_cancelled_locked(
        self, session_key: str, surface: ReceiptSurface, owner: str = ""
    ) -> None:
        """Finalize the receipt to a "🛑 Cancelled" record, if present.

        Caller MUST hold :attr:`lock` across clear_queue + this call.

        ``owner`` names the ONE principal whose messages were cleared, and then only
        that person's lines are withdrawn from the record. Whether anything is written
        depends on who opened the bubble:

        * the caller opened it -- it finalizes as cancelled over the caller's OWN
          withdrawn lines. Not over what remains: those lines belong to other
          principals, and this is their sender's conversation only by coincidence of
          who queued first.
        * somebody else opened it -- nothing is written, because ``msg_id`` belongs to
          that person's conversation and in the caller's the same number is another
          message entirely.

        The entry is dropped once it owes nothing. It is RETAINED in exactly one case:
        the finalizing edit did not land, so the entry carries the record it owes and
        is terminal. That is safe against a later drain for the same reason the
        addressing rule holds -- an owed record is written through
        :attr:`QueueReceipt.opened_on`, the surface the bubble was opened on, so no
        transition is ever handed an id minted in a different chat. A retained entry is
        not live and is never grown, so a later burst opens a fresh bubble rather than
        joining this one; which conversation a shared bubble belongs to is the registry
        key's own question.

        Omitted, the whole receipt is finalized, which is what the whole-session callers
        mean: the queue they cleared was all of it.
        """
        receipt = self._receipts.get(session_key)
        if receipt is None:
            return
        if receipt.owes_record:
            # This bubble already owes a record from an earlier transition -- those
            # messages left the queue THEN, not in this clear. Writing "Cancelled" over
            # an owed "Now answering" would say the opposite of what happened, and
            # permanently. Retry what is owed, through the bubble's own conversation
            # rather than this caller's, and leave the entry until it lands.
            if await self._write_record(receipt):
                self._receipts.pop(session_key, None)
            return
        if owner:
            withdrawn = receipt.withdraw(owner)
            if not withdrawn:
                return
            self._receipts.pop(session_key, None)
            if owner == receipt.opened_by:
                body = receipt_text(withdrawn, cancelled=True)
                if not await self._edit(surface, receipt.msg_id, body):
                    receipt.final_body = body
                    self._receipts[session_key] = receipt
            return
        self._receipts.pop(session_key, None)
        body = receipt_text(receipt.texts, cancelled=True)
        # Addressed to the bubble, not to this caller: a whole-session clear names no
        # principal (a caller that means "the queue was all of it"), so under a shared
        # key it can arrive from someone who did not open the bubble. Unlike a grow this
        # record is TERMINAL -- those messages have left the queue, nothing will revisit
        # the bubble, and a bubble left reading "⏳ Queued" for cleared messages is wrong
        # for good -- so it is written rather than skipped.
        target = receipt.opened_on or surface
        if not await self._edit(target, receipt.msg_id, body):
            receipt.final_body = body
            self._receipts[session_key] = receipt

    async def _edit(self, surface: ReceiptSurface, msg_id: Any, body: str) -> bool:
        """Rewrite the bubble to *body*. Returns whether the write LANDED.

        A raise and a reported ``False`` are the same answer here: the bubble still
        shows its old text. Only an explicit ``False`` counts as reported failure --
        a surface that answers ``None`` has not reported one, and reading silence as
        failure would keep every receipt in the registry for good.
        """
        try:
            return await surface.edit_receipt(msg_id, body) is not False
        except Exception:
            logger.debug("%s: queue receipt edit failed", surface.label, exc_info=True)
            return False

    async def _write_record(self, receipt: QueueReceipt) -> bool:
        """Put the record *receipt* owes onto its bubble. Returns whether it landed.

        Writes through ``receipt.opened_on`` and NEVER through a caller's surface.
        Under a shared session key the surface handed to a transition belongs to
        whoever spoke last, and both writes here are addressed to the bubble's own
        conversation: the edit targets ``msg_id``, which is another message entirely
        in anybody else's chat, and the fallback POSTS a body quoting
        ``opened_by``'s text, which is theirs to read and nobody else's. An entry
        with no bound surface is not written at all for the same reason -- there is
        no address to fall back to, only the wrong one.

        Editing first is what keeps the record in the bubble the reader is already
        looking at. When the bubble refuses edits the record is POSTED instead: past
        Webex's documented per-message edit cap no edit of that id will ever land, so
        retrying alone would leave the record owed for the life of the process. The
        stale bubble still reads "⏳ Queued" and the posted record says what happened,
        which together are true; a silent bubble alone is not. The post goes to the
        bubble's own send address, so on a channel with forum Topics the record lands
        in the Topic the bubble is in rather than the parent chat.
        """
        body = receipt.final_body
        if body is None:
            return True
        surface = receipt.opened_on
        if surface is None:
            return False
        if await self._edit(surface, receipt.msg_id, body):
            return True
        try:
            return await surface.send_receipt(body) is not None
        except Exception:
            logger.debug("%s: queue receipt record post failed", surface.label, exc_info=True)
            return False
