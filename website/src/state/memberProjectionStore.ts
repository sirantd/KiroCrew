/**
 * Framework-free per-member projection store.
 *
 * Holds the latest projected value per (slug, key), fed by two WebSocket
 * frames and a roster baseline. Four invariants keep it correct against
 * replays, races, and server restarts:
 *
 *   - generation-then-seq-wins: apply() compares (stateVersion, seq) in that
 *     order, so an older generation loses outright and within one generation a
 *     replayed or out-of-order seq is a no-op. A deletion arrives at an advanced
 *     generation, which is what lets it outrank a row sitting at a high seq.
 *   - a deletion RETAINS NOTHING: a null value drops the row rather than holding
 *     a tombstone, because a re-enabled app picks its own stateVersion and cannot
 *     know which generation the server advanced to -- a tombstone would discard
 *     its real updates. Refusing a publish from the RETIRED generation is not
 *     this layer's job: teardown revokes the grant before deleting rows and the
 *     publish path commits behind a generation fence, so the server does not
 *     send one.
 *   - an ATTRIBUTABLE baseline never truncates: while the roster stands behind
 *     a slug's sequence, seed() only applies values, so a live frame that raced
 *     ahead of the baseline keeps winning.
 *   - an UNATTRIBUTABLE baseline (asOfSeq < 0) clears the slug and refuses its
 *     frames. That sentinel is not a sequence, so nothing can be compared
 *     against it, and the state it would otherwise leave on screen belongs to
 *     whichever member last held the slug. See seed().
 *   - truncate only from the subscribed frame: rows with seq > lastSeq are
 *     dropped ONLY when the server tells us (members_subscribed), which is the
 *     one moment we learn a torn tail was rolled back after a restart.
 *
 * faceOf() exposes a useSyncExternalStore-shaped view per (slug, key) whose
 * snapshot is referentially stable until that row actually changes.
 */

/** One held projection: the value, where it sits in the order, and its version. */
interface Row {
  value: unknown
  seq: number
  /**
   * The row's generation, which orders BEFORE seq. The server already publishes
   * on this pair — a lower stateVersion is refused outright and an equal one
   * requires the seq to advance — so comparing only the seq here would be a
   * weaker rule than the one the rows were written under.
   *
   * Rows that predate the field, and every built-in key, read as 0, which makes
   * the comparison collapse to plain higher-seq-wins for them.
   */
  stateVersion: number
}

/** The useSyncExternalStore-shaped view for a single (slug, key). */
export interface ProjectionFace {
  subscribe(listener: () => void): () => void
  getSnapshot(): unknown | undefined
}

export class MemberProjectionStore {
  private readonly rows = new Map<string, Map<string, Row>>()
  private readonly listeners = new Map<string, Set<() => void>>()
  /**
   * Slugs the server has declared UNATTRIBUTABLE, and whose frames are therefore
   * refused until it says otherwise. See {@link seed} for why a slug lands here.
   */
  private readonly unattributable = new Set<string>()
  private static faceKey(slug: string, key: string): string {
    return slug + '\u0000' + key
  }

  private notify(slug: string, key: string): void {
    const set = this.listeners.get(MemberProjectionStore.faceKey(slug, key))
    if (!set) return
    for (const fn of set) fn()
  }

  /**
   * Apply one projected value. Ordering is (stateVersion, seq), in that order:
   * an older generation loses outright, and within one generation higher-seq-wins
   * as before, so an equal-seq replay and a stale frame both drop. Otherwise
   * store it and notify the (slug, key) face.
   *
   * A NULL value is a deletion, and it DROPS the row rather than holding a
   * tombstone. Holding one would be the mirror defect: the deletion arrives at an
   * advanced generation so that a concurrent older publish cannot resurrect the
   * card, but a retained tombstone at that generation would then outrank the real
   * updates of a re-enabled app -- and a re-enabled app cannot know which
   * generation to publish past, because the server advanced it, not the app. With
   * the row gone there is nothing left to lose against, and the next publish
   * simply populates a fresh row.
   *
   * A frame for a slug the roster declared unattributable is DROPPED. The roster
   * is the only surface that checks whether a slug names one member; the live
   * publish and the on-connect replay both read a snapshot straight out of the
   * service, so a frame can arrive for a slug the roster has already refused to
   * attribute. Refusing here is what makes the refusal reach the screen, and it
   * needs no audit of every future emitter.
   */
  apply(slug: string, key: string, value: unknown, seq: number, stateVersion = 0): void {
    if (this.unattributable.has(slug)) return
    let byKey = this.rows.get(slug)
    const existing = byKey?.get(key)
    if (existing) {
      if (stateVersion < existing.stateVersion) return
      if (stateVersion === existing.stateVersion && seq <= existing.seq) return
    }
    if (value === null) {
      // Won the comparison, so the deletion is current. Drop the row entirely.
      if (!byKey) return
      if (!byKey.delete(key)) return
      if (byKey.size === 0) this.rows.delete(slug)
      this.notify(slug, key)
      return
    }
    if (!byKey) {
      byKey = new Map<string, Row>()
      this.rows.set(slug, byKey)
    }
    byKey.set(key, { value, seq, stateVersion })
    this.notify(slug, key)
  }

  /**
   * Seed a slug's baseline from the roster block. Each key is applied at asOfSeq
   * through apply(), so a live frame that already advanced the row past asOfSeq
   * keeps winning.
   *
   * An OMITTED key is a statement, not an absence of one: the block answers for
   * the whole slug, so a key it does not carry is one the roster would not serve
   * again, and a cached row left behind renders a card for something that is no
   * longer there. An EMPTY block is the same statement about every key at once,
   * which is what a shared-slug collision produces for the slug both members
   * claim. Keeping whatever is cached would, for a collision, keep the OTHER
   * member's projection -- so the page would show one member's data under a slug
   * the server refuses to attribute, and a stale read of that kind looks
   * identical to a live one.
   *
   * Two empty blocks arrive, and the DIFFERENCE IS THE SEQUENCE:
   *
   * * ``asOfSeq >= 0`` is a real position: the log was read and holds no
   *   projections yet. Higher-seq-wins applies exactly as it does per key, so a
   *   row a live frame already carried past this point is newer and is kept.
   * * ``asOfSeq < 0`` is not a position at all. It is the sentinel the roster
   *   emits when it will not attribute the slug -- a collision, a header naming
   *   another member, or a read that failed -- and every real row's seq is above
   *   it, so comparing them keeps the whole cache instead of dropping it: the
   *   clear fails on precisely the case it exists for. The slug is therefore
   *   cleared UNCONDITIONALLY and marked unattributable, which also refuses the
   *   live frames that reach {@link apply} from the publish and replay paths,
   *   neither of which checks attribution. The mark lifts the moment the roster
   *   sends a baseline carrying a real sequence.
   *
   * The sentinel governs the whole block, not just an empty one: a value carried
   * at a sequence the server would not stand behind is not a baseline, so it is
   * dropped rather than applied at a negative seq that every later frame beats.
   */
  seed(
    slug: string,
    values: { [key: string]: unknown },
    asOfSeq: number,
    stateVersions: { [key: string]: number } = {},
    seqs: { [key: string]: number } = {},
  ): void {
    if (asOfSeq < 0) {
      this.unattributable.add(slug)
      const byKey = this.rows.get(slug)
      if (!byKey) return
      for (const key of [...byKey.keys()]) {
        byKey.delete(key)
        this.notify(slug, key)
      }
      this.rows.delete(slug)
      return
    }
    this.unattributable.delete(slug)
    const keys = Object.keys(values)
    const present = new Set(keys)
    // A baseline answers for what it OMITS as well as for what it carries. A
    // contributed key the roster does not serve is gone -- its app's row was
    // deleted with the app, or the app may no longer publish that key -- and a
    // cached row left behind renders a card for something no reader would be
    // served again. Membership is asked of a Set rather than the object, so a key
    // that happens to name an inherited property is treated like any other.
    //
    // Bounded by asOfSeq for the same reason every key in this block is: a row a
    // live frame already carried PAST this read is newer than the baseline that
    // omits it, so it stands. An empty block is this rule's k = 0 case, which is
    // why it needs no branch of its own.
    const byKey = this.rows.get(slug)
    if (byKey) {
      for (const [key, row] of [...byKey]) {
        if (present.has(key)) continue
        if (row.seq > asOfSeq) continue
        byKey.delete(key)
        this.notify(slug, key)
      }
      if (byKey.size === 0) this.rows.delete(slug)
    }
    if (keys.length === 0) return
    for (const key of keys) {
      // Each key at its OWN seq and generation, never the response's. Both belong to
      // the row rather than to this reply: a contributed row's seq is the
      // contributor's fold position, which TRAILS asOfSeq, so seeding at asOfSeq
      // would make the gate in apply() drop that contributor's next live push and
      // freeze the card at its baseline -- which is exactly why the backend sends
      // these two maps beside the values. A built-in key appears in neither map and
      // correctly falls back to asOfSeq and generation 0.
      this.apply(slug, key, values[key], seqs[key] ?? asOfSeq, stateVersions[key] ?? 0)
    }
  }

  /**
   * Drop this slug's rows whose seq > lastSeq and notify them. Called ONLY
   * from the members_subscribed frame: the server may have truncated a torn
   * tail after a restart, and this is where the client learns of it.
   *
   * ANSWERS whether anything was dropped, which the caller needs. Dropping the
   * row is right -- a row above the server's seq records something that did not
   * happen -- but it leaves the card with no value at all, when the truth is
   * whatever the value was at `lastSeq`. This store is a cache and cannot
   * synthesise that, so the only honest repair is for the caller to refetch the
   * authoritative baseline; a silent drop renders a blank card that is
   * indistinguishable from a member who has no such projection.
   */
  truncate(slug: string, lastSeq: number): boolean {
    const byKey = this.rows.get(slug)
    if (!byKey) return false
    let dropped = false
    for (const [key, row] of byKey) {
      if (row.seq > lastSeq) {
        byKey.delete(key)
        this.notify(slug, key)
        dropped = true
      }
    }
    if (byKey.size === 0) this.rows.delete(slug)
    return dropped
  }

  /**
   * Apply a members_subscribed frame as the authoritative baseline for this
   * connection.
   *
   * `lastSeqs` carries EVERY slug the server holds a readable log for, so a slug
   * the client still caches and the frame omits is one the server cannot serve:
   * its header was damaged or unreadable, or the member is gone. Leaving those
   * rows cached lets them override the empty roster baseline, so the page shows a
   * member's pre-restart state indefinitely -- the stale read looks identical to a
   * live one. Dropped rather than kept, which is the same choice `truncate` makes
   * for a row that ran ahead of the server: the server's view wins.
   */
  truncateAll(lastSeqs: { [slug: string]: number }): boolean {
    let dropped = false
    for (const slug of Object.keys(lastSeqs)) {
      if (this.truncate(slug, lastSeqs[slug])) dropped = true
    }
    // Snapshot the slugs before mutating, since dropping edits `this.rows`.
    for (const slug of [...this.rows.keys()]) {
      if (Object.prototype.hasOwnProperty.call(lastSeqs, slug)) continue
      const byKey = this.rows.get(slug)
      if (!byKey) continue
      for (const key of [...byKey.keys()]) {
        byKey.delete(key)
        this.notify(slug, key)
        dropped = true
      }
      this.rows.delete(slug)
    }
    return dropped
  }

  /**
   * A useSyncExternalStore-shaped view of one (slug, key). getSnapshot returns
   * the SAME Row.value reference until the row changes, which
   * useSyncExternalStore requires to avoid an infinite render loop.
   */
  faceOf(slug: string, key: string): ProjectionFace {
    const faceKey = MemberProjectionStore.faceKey(slug, key)
    return {
      subscribe: (listener: () => void): (() => void) => {
        let set = this.listeners.get(faceKey)
        if (!set) {
          set = new Set<() => void>()
          this.listeners.set(faceKey, set)
        }
        set.add(listener)
        return () => {
          const s = this.listeners.get(faceKey)
          if (!s) return
          s.delete(listener)
          if (s.size === 0) this.listeners.delete(faceKey)
        }
      },
      // Reads the live row each call; the stored value reference only changes
      // when apply() replaces the Row, so identity is stable between changes.
      getSnapshot: (): unknown | undefined => this.rows.get(slug)?.get(key)?.value,
    }
  }

  /** Read one held value (test/consumer helper). */
  get(slug: string, key: string): unknown | undefined {
    return this.rows.get(slug)?.get(key)?.value
  }

  /** Whether any row is held for this slug. */
  has(slug: string): boolean {
    return this.rows.has(slug)
  }

  /** Drop all rows and listeners (tests). */
  clear(): void {
    this.rows.clear()
    this.listeners.clear()
  }
}

/** Process-wide singleton the WebSocket layer feeds and hooks read. */
export const memberProjectionStore = new MemberProjectionStore()
