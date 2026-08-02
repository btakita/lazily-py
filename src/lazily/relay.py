"""RelayCell backpressure plan (#relaycell), Phases 2-6 — the Python port.

See ``lazily-spec/docs/relaycell.md`` and ``relaycell-backpressure-analysis.md``.
A :class:`RelayCell` is an *algebra-typed conflating relay*: it accumulates a
fast ingress into a **hot head** (a :class:`~lazily.merge.MergePolicy` fold),
bounds it with a reactive :class:`BackpressurePolicy`, and lets a slow egress
**drain** the coalesced window. The converged egress state is independent of the
drain schedule whenever the merge ``⊕`` is associative (the ``relay_converges``
invariant, pinned in ``LazilyFormal.Relay``).

Phase 2 ``RelayCell`` + ``BackpressurePolicy`` · Phase 3 ``SpillStore`` ·
Phase 4 ``Transport`` · Phase 5 ``Outbox``/``Inbox`` roles · Phase 6
``RatePolicy``/``WindowPolicy``/``ExpiryPolicy``/``PriorityStorage``/
``KeyedRelay``. Time is a logical clock (a monotone tick) so behaviour is
deterministic and portable.

It is a *composite*, not a new node: the hot head is a :class:`~lazily.cell.Cell`
and its ``depth``/``is_full``/``is_empty`` reads are demand-driven
:func:`~lazily.slot.slot` handles, so an unobserved relay costs ``N·⊕`` and no
more (the merge cost law).
"""

from __future__ import annotations


__all__ = [
    "BackpressurePolicy",
    "BoundDim",
    "ExpiryPolicy",
    "FramedTransport",
    "InProcTransport",
    "Inbox",
    "IngressOutcome",
    "KeyedRelay",
    "Outbox",
    "Overflow",
    "PriorityStorage",
    "RatePolicy",
    "RelayCell",
    "RelayConfigError",
    "SpillMode",
    "SpillPage",
    "SpillStore",
    "Transport",
    "WindowPolicy",
]

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .cell import Cell
from .slot import Slot


if TYPE_CHECKING:
    from collections.abc import Iterator

    from .merge import MergePolicy


# -- Phase 2: RelayCell + BackpressurePolicy ---------------------------------


class BoundDim(Enum):
    """What a bound measures (analysis §4.4). The core meters ``COUNT``."""

    COUNT = "Count"
    BYTES = "Bytes"
    KEYS = "Keys"
    AGE = "Age"


class Overflow(Enum):
    """The action taken when the hot head crosses ``high_water`` (analysis §4.4)."""

    #: Refuse ingress; the producer backpressures (observes ``is_full``). Lossless.
    BLOCK = "Block"
    #: Discard the incoming op. Lossy.
    DROP_NEWEST = "DropNewest"
    #: Reset the window to the incoming op, discarding what accumulated. Lossy.
    DROP_OLDEST = "DropOldest"
    #: Keep merging — the coalescence *is* the bound. Requires ``policy.conflates``.
    CONFLATE = "Conflate"
    #: Page the accumulated window to a durable tail (Phase 3 ``SpillStore``).
    SPILL = "Spill"


class IngressOutcome(Enum):
    """The outcome of a single ``ingress`` op."""

    #: Merged into an empty window (window depth was 0).
    ACCEPTED = "Accepted"
    #: Merged into a non-empty window (coalesced with prior ops).
    CONFLATED = "Conflated"
    #: Dropped by ``DROP_NEWEST``/``DROP_OLDEST`` overflow.
    DROPPED = "Dropped"
    #: Refused by ``BLOCK`` overflow; the producer must retry after a drain.
    BLOCKED = "Blocked"


class RelayConfigError(Exception):
    """A relay construction/merge-swap was rejected (analysis §4.3 flag check).

    Raised when ``CONFLATE`` overflow is chosen for a non-conflating policy
    (``RawFifo``): coalescence cannot bound a policy whose order and multiplicity
    are meaning.
    """

    #: Marker for the sole current rejection (mirrors the JS error code).
    CONFLATE_NOT_BOUNDING = "ConflateNotBounding"


class BackpressurePolicy:
    """Reactive backpressure limits (analysis §4.4). Every field is a
    :class:`~lazily.cell.Cell`, so an operator or an adaptive controller retunes
    it live and dependent relays react. Hysteresis (``high_water`` ≠
    ``low_water``) prevents flapping.
    """

    __slots__ = ("ctx", "dimension", "high_water", "low_water", "overflow")

    def __init__(
        self,
        ctx: dict,
        dimension: BoundDim,
        high_water: int,
        low_water: int,
        overflow: Overflow,
    ) -> None:
        self.ctx = ctx
        self.dimension: Cell[BoundDim] = Cell(ctx, dimension)
        self.high_water: Cell[int] = Cell(ctx, high_water)
        self.low_water: Cell[int] = Cell(ctx, low_water)
        self.overflow: Cell[Overflow] = Cell(ctx, overflow)


class RelayCell[T]:
    """The algebra-typed conflating relay (Phase 2, in-proc core).

    The hot head is a cell; ``depth``/``is_full``/``is_empty`` are demand-driven
    slots, so an unobserved relay costs ``N·⊕`` and no more (the merge cost law).
    """

    __slots__ = (
        "_ctx",
        "_depth",
        "_head",
        "_is_empty",
        "_is_full",
        "_merge_policy",
        "_pending",
        "_policy",
    )

    def __init__(
        self,
        ctx: dict,
        policy: BackpressurePolicy,
        merge_policy: MergePolicy[T],
    ) -> None:
        if policy.overflow.get() == Overflow.CONFLATE and not merge_policy.conflates:
            raise RelayConfigError(RelayConfigError.CONFLATE_NOT_BOUNDING)
        self._ctx = ctx
        self._policy = policy
        self._merge_policy = merge_policy
        # Hot head: current window's coalesced value (``None`` = empty window).
        self._head: Cell[T | None] = Cell(ctx, None)
        # Ops merged into the current window since the last drain (the Count bound).
        self._pending: Cell[int] = Cell(ctx, 0)
        self._depth = Slot(lambda c: c.read(self._pending))
        self._is_full = Slot(
            lambda c: self._depth(c) >= c.read(self._policy.high_water)
        )
        self._is_empty = Slot(lambda c: c.read(self._head) is None)

    @property
    def policy(self) -> BackpressurePolicy:
        return self._policy

    @property
    def merge_policy(self) -> MergePolicy[T]:
        return self._merge_policy

    def overflow_is_legal(self) -> bool:
        """Whether the current overflow choice is legal for the policy — a runtime
        guard mirroring construction (the overflow cell is reactive)."""
        return (
            self._policy.overflow.get() != Overflow.CONFLATE
            or self._merge_policy.conflates
        )

    # Demand-driven readers ---------------------------------------------------

    def depth(self, ctx: Any = None) -> int:
        """Current window depth (``Count``).

        Pass the caller's :class:`~lazily.compute.Compute` view (``ctx``) when
        reading inside a reactive body to value-thread the dependency edge; omit
        it for an untracked top-level read (``#lzcellkernel`` bare-read removal).
        """
        return self._depth(self._ctx if ctx is None else ctx)

    def is_full(self, ctx: Any = None) -> bool:
        """Window is at/over ``high_water``. See :meth:`depth` on ``ctx``."""
        return self._is_full(self._ctx if ctx is None else ctx)

    def is_empty(self, ctx: Any = None) -> bool:
        """Window is empty (nothing to drain). See :meth:`depth` on ``ctx``."""
        return self._is_empty(self._ctx if ctx is None else ctx)

    def depth_slot(self):
        """The ``depth`` reader slot (for wiring into effects/computations)."""
        return self._depth

    def is_full_slot(self):
        """The ``is_full`` reader slot."""
        return self._is_full

    def is_empty_slot(self):
        """The ``is_empty`` reader slot."""
        return self._is_empty

    # Ingress / egress --------------------------------------------------------

    def _read_full(self) -> bool:
        return self._pending.get() >= self._policy.high_water.get()

    def _merge_into_head(self, op: T) -> None:
        cur = self._head.get()
        nxt = op if cur is None else self._merge_policy.merge(cur, op)
        self._head.set(nxt)

    def ingress(self, op: T) -> IngressOutcome:
        """Ingest one op. Applies the reactive overflow policy when the window is
        at ``high_water``; otherwise merges the op into the hot head under the
        policy."""
        was_empty = self._pending.get() == 0
        if self._read_full():
            overflow = self._policy.overflow.get()
            if overflow == Overflow.BLOCK:
                return IngressOutcome.BLOCKED
            if overflow == Overflow.DROP_NEWEST:
                return IngressOutcome.DROPPED
            if overflow == Overflow.DROP_OLDEST:
                # Discard the accumulated window, restart from this op.
                self._head.set(op)
                self._pending.set(1)
                return IngressOutcome.DROPPED
            # CONFLATE keeps merging (the coalescence is the bound); SPILL is
            # Phase 3 and, until wired, degrades to CONFLATE for a bounding
            # policy. Both fall through to the merge below.
        self._merge_into_head(op)
        self._pending.set(self._pending.get() + 1)
        return IngressOutcome.ACCEPTED if was_empty else IngressOutcome.CONFLATED

    def drain(self) -> T | None:
        """Drain the coalesced window: take the hot head's value and reset the
        window. Returns ``None`` for an empty window. ``relay_converges``
        guarantees the egress fold equals the flat fold of every ingested op, for
        any drain schedule."""
        cur = self._head.get()
        if cur is not None:
            self._head.set(None)
            self._pending.set(0)
        return cur

    def peek(self) -> T | None:
        """Peek the current coalesced window without draining."""
        return self._head.get()


# -- Phase 3: SpillStore -----------------------------------------------------


class SpillMode(Enum):
    """How spilled windows are laid out on the durable tail (analysis §6)."""

    #: Merge each spilled window into the open page until it fills — minimizes
    #: disk (keep-latest / semilattice). One page holds a coalesced run.
    COMPACT_ON_WRITE = "CompactOnWrite"
    #: Append each spilled window as its own page — preserves increments for an
    #: accumulating (non-idempotent) policy that must not double-count.
    APPEND_COMPACT = "AppendCompact"


@dataclass
class SpillPage[T]:
    """One immutable cold page: a coalesced window summary plus its manifest entry."""

    id: int
    summary: T
    bytes: int


class SpillStore[T]:
    """A paged durable tail for a :class:`RelayCell` (Phase 3, in-memory reference
    backend). Holds a hot page in RAM plus immutable cold pages, a bounded
    manifest, an egress cursor, and ack-before-reclaim. Memory is
    ``O(hot) + O(manifest)``."""

    __slots__ = (
        "_acked",
        "_next_id",
        "_open_fill",
        "_pages",
        "merge_policy",
        "mode",
        "page_size",
    )

    def __init__(
        self, mode: SpillMode, page_size: int, merge_policy: MergePolicy[T]
    ) -> None:
        self.mode = mode
        self.page_size = max(1, page_size)
        self.merge_policy = merge_policy
        self._pages: list[SpillPage[T]] = []
        self._open_fill = 0
        self._next_id = 0
        self._acked = 0  # pages acked from the front (reclaimable)

    def spill(self, window: T, bytes: int) -> None:
        """Spill one coalesced window summary to the durable tail. ``APPEND_COMPACT``
        always opens a new page; ``COMPACT_ON_WRITE`` merges into the open page
        until it reaches ``page_size``, then seals it."""
        if self.mode not in (SpillMode.APPEND_COMPACT, SpillMode.COMPACT_ON_WRITE):
            # `SpillMode` is a local policy knob, never decoded from a peer, so
            # an unrecognised mode is a caller defect. The chain below used to
            # treat "not APPEND_COMPACT" as "COMPACT_ON_WRITE", which would
            # silently apply merge-on-write semantics to a mode that had not
            # asked for them.
            raise ValueError(f"unknown spill mode: {self.mode!r}")
        if self.mode == SpillMode.APPEND_COMPACT:
            self._push_page(window, bytes)
        elif self._open_fill >= self.page_size or not self._pages:
            self._push_page(window, bytes)
            self._open_fill = 1
        else:
            last = self._pages[-1]
            last.summary = self.merge_policy.merge(last.summary, window)
            last.bytes += bytes
            self._open_fill += 1

    def _push_page(self, summary: T, bytes: int) -> None:
        self._pages.append(SpillPage(self._next_id, summary, bytes))
        self._next_id += 1

    def manifest(self) -> list[tuple[int, int]]:
        """The manifest: ``(page_id, bytes)`` for every live page (bounded metadata)."""
        return [(p.id, p.bytes) for p in self._pages]

    def pending_pages(self) -> list[SpillPage[T]]:
        """Pages the egress has not yet acked (at/after the ack cursor)."""
        return self._pages[self._acked :]

    def page_count(self) -> int:
        return len(self._pages)

    def ack_through(self, id: int) -> None:
        """Ack every page through ``id`` (inclusive), advancing the reclaim cursor."""
        while self._acked < len(self._pages) and self._pages[self._acked].id <= id:
            self._acked += 1

    def reclaim(self) -> None:
        """Drop acked pages (durable reclaim). Manifest/cursor stay consistent."""
        if self._acked > 0:
            del self._pages[: self._acked]
            self._acked = 0

    def fold_pages(self, s0: T) -> T:
        """Fold every live cold page (oldest first) into ``s0``."""
        acc = s0
        for p in self._pages:
            acc = self.merge_policy.merge(acc, p.summary)
        return acc

    def reconstruct(self, s0: T, hot: T | None) -> T:
        """Reconstruction (``spill_lossless``). Fold the cold tail then the hot
        head — reproduces the flat fold of every op the relay ever ingested."""
        cold = self.fold_pages(s0)
        return cold if hot is None else self.merge_policy.merge(cold, hot)

    def replay_unacked(self, downstream: T) -> T:
        """Crash replay. After recovery the egress re-delivers every unacked page
        from the ack cursor into ``downstream``. For an idempotent policy
        re-applying an already-delivered page is a no-op
        (``spill_replay_idempotent``), so at-least-once replay converges."""
        acc = downstream
        for p in self.pending_pages():
            acc = self.merge_policy.merge(acc, p.summary)
        return acc


# -- Phase 4: Transport ------------------------------------------------------
#
# Transport abstracts ingress/egress delivery so the mechanism is pluggable. A
# RelayCell is written once against Transport; the merge algebra — not the
# transport — guarantees converged state (transport_independent), so transports
# may differ across bindings and still converge.


@runtime_checkable
class Transport[T](Protocol):
    """The delivery seam: buffer ops (``deliver``), hand them over in frames
    (``poll``), and report backlog (``has_pending``)."""

    def deliver(self, op: T) -> None: ...

    def poll(self) -> list[T]: ...

    def has_pending(self) -> bool: ...


class InProcTransport[T]:
    """InProc — direct delivery: every buffered op is handed over in one frame."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf: list[T] = []

    def deliver(self, op: T) -> None:
        self._buf.append(op)

    def poll(self) -> list[T]:
        out = self._buf
        self._buf = []
        return out

    def has_pending(self) -> bool:
        return len(self._buf) > 0


class FramedTransport[T]:
    """A framed transport — models CrossThread/Ipc/Ws: ops are delivered in
    bounded frames of at most ``frame_size`` (an MTU / batch boundary). Different
    ``frame_size``s are different framings of the same op stream."""

    __slots__ = ("_buf", "frame_size")

    def __init__(self, frame_size: int) -> None:
        self._buf: list[T] = []
        self.frame_size = max(1, frame_size)

    def deliver(self, op: T) -> None:
        self._buf.append(op)

    def poll(self) -> list[T]:
        n = min(self.frame_size, len(self._buf))
        frame = self._buf[:n]
        del self._buf[:n]
        return frame

    def has_pending(self) -> bool:
        return len(self._buf) > 0


# -- Phase 5: Outbox / Inbox roles -------------------------------------------
#
# RelayCell is direction-neutral; Outbox and Inbox are role facades (typed
# constructors with direction-appropriate defaults), not reimplementations. They
# differ in the backpressure-propagation contract. A network link is
# Outbox → Transport → Inbox.


class Outbox[T]:
    """The app → transport send side (analysis §4.7). Backpressures the local
    producer directly via ``is_full``. Default overflow ``CONFLATE`` (state
    broadcast)."""

    __slots__ = ("_relay",)

    def __init__(
        self,
        ctx: dict,
        high_water: int,
        merge_policy: MergePolicy[T],
        *,
        dimension: BoundDim = BoundDim.COUNT,
        overflow: Overflow = Overflow.CONFLATE,
    ) -> None:
        policy = BackpressurePolicy(
            ctx, dimension, high_water, high_water // 2, overflow
        )
        self._relay: RelayCell[T] = RelayCell(ctx, policy, merge_policy)

    def send(self, op: T) -> IngressOutcome:
        """The local producer sends an op. A ``BLOCKED`` outcome is the producer's
        backpressure signal — it should await a drain before retrying."""
        return self._relay.ingress(op)

    def drain(self) -> T | None:
        """The transport drains the coalesced window for egress."""
        return self._relay.drain()

    def is_full(self, ctx: Any = None) -> bool:
        """The producer-facing backpressure signal (window at/over the watermark).

        Pass the caller's :class:`~lazily.compute.Compute` view to track the read
        inside a reactive body; omit for an untracked top-level read."""
        return self._relay.is_full(ctx)

    def is_full_slot(self):
        return self._relay.is_full_slot()

    def relay(self) -> RelayCell[T]:
        """Access the underlying relay (for wiring extra egress stages)."""
        return self._relay


class Inbox[T]:
    """The transport → app receive side (analysis §4.7). Cannot block the remote
    directly; backpressure is a **credit meter** the app replenishes."""

    __slots__ = ("_credits", "_max_credits", "_relay")

    def __init__(
        self,
        ctx: dict,
        high_water: int,
        max_credits: int,
        merge_policy: MergePolicy[T],
        *,
        overflow: Overflow = Overflow.CONFLATE,
    ) -> None:
        policy = BackpressurePolicy(
            ctx, BoundDim.COUNT, high_water, high_water // 2, overflow
        )
        self._relay: RelayCell[T] = RelayCell(ctx, policy, merge_policy)
        self._credits = max_credits
        self._max_credits = max_credits

    def ready(self) -> bool:
        """Whether the transport may deliver another message (a credit is
        available). When ``False``, the transport must stop reading → the remote
        throttles."""
        return self._credits > 0

    def credits(self) -> int:
        """Credits currently available to the remote."""
        return self._credits

    def receive(self, op: T) -> IngressOutcome:
        """The transport delivers a received op. Consumes a credit; the caller
        MUST have checked :meth:`ready` (a delivery without credit still applies
        but drives ``credits`` to zero, signalling the remote to stop)."""
        self._credits = max(0, self._credits - 1)
        return self._relay.ingress(op)

    def consume(self, replenish: int) -> T | None:
        """The app consumes the coalesced window and replenishes ``replenish``
        credits (up to the budget), re-opening the remote's flow."""
        out = self._relay.drain()
        self._credits = min(self._credits + replenish, self._max_credits)
        return out


# -- Phase 6: extra reactive policies ----------------------------------------
#
# Each policy is an optional reactive stage composed onto a relay egress; they
# only change where/when a relay flushes or which ops survive. Time is a logical
# clock (a monotone tick) — a binding drives tick/advance from its own runtime
# timer.


class RatePolicy:
    """Case 9 — rate-limited egress (token bucket). A drain is permitted only when
    a token is available. Refilled ``refill_per_tick`` tokens per logical tick,
    capped at ``capacity``."""

    __slots__ = ("_tokens", "capacity", "refill_per_tick")

    def __init__(self, capacity: int, refill_per_tick: int) -> None:
        self.capacity = capacity
        self._tokens = capacity
        self.refill_per_tick = refill_per_tick

    def tokens(self) -> int:
        return self._tokens

    def try_egress(self) -> bool:
        """Try to consume one token for an egress; returns ``True`` if paced through."""
        if self._tokens > 0:
            self._tokens -= 1
            return True
        return False

    def tick(self) -> None:
        """Advance the logical clock, refilling the bucket (saturating at capacity)."""
        self._tokens = min(self._tokens + self.refill_per_tick, self.capacity)


class WindowPolicy:
    """Case 8 — time-windowed coalescence (debounce/throttle). Flushes when it
    reaches ``window_ops`` ops or on an explicit ``tick``. Because a window is
    just a flush group, associativity keeps the converged state unchanged."""

    __slots__ = ("_pending", "window_ops")

    def __init__(self, window_ops: int) -> None:
        self.window_ops = max(1, window_ops)
        self._pending = 0

    def on_ingress(self) -> bool:
        """Record one ingress; returns ``True`` when the window is full and should
        flush."""
        self._pending += 1
        if self._pending >= self.window_ops:
            self._pending = 0
            return True
        return False

    def tick(self) -> bool:
        """The debounce/throttle interval elapsed: flush whatever is pending."""
        if self._pending > 0:
            self._pending = 0
            return True
        return False


class ExpiryPolicy:
    """Case 10 — TTL / deadline expiry. Drops elements whose age exceeds ``ttl``
    against a logical clock. Lossy-by-age (explicit); used to shed cold data."""

    __slots__ = ("_now", "ttl")

    def __init__(self, ttl: int) -> None:
        self.ttl = ttl
        self._now = 0

    def advance(self, by: int) -> None:
        self._now += by

    def now(self) -> int:
        return self._now

    def is_live(self, stamped_at: int) -> bool:
        """Whether an element stamped at ``stamped_at`` is still live (not expired)."""
        return self._now - stamped_at <= self.ttl

    def retain_live[T](self, batch: list[tuple[int, T]]) -> list[T]:
        """Retain only the live elements of a ``(ts, value)`` batch (drop the aged
        tail)."""
        return [v for ts, v in batch if self.is_live(ts)]


class PriorityStorage[T]:
    """Case 11 — priority egress. Ingress carries a priority; egress pops the
    highest priority first (not FIFO), FIFO within equal priority. Reordering, so
    sound for a commutative merge downstream."""

    __slots__ = ("_items", "_seq")

    def __init__(self) -> None:
        # (priority, seq, value); seq breaks ties FIFO within a priority.
        self._items: list[tuple[int, int, T]] = []
        self._seq = 0

    def push(self, priority: int, value: T) -> None:
        self._items.append((priority, self._seq, value))
        self._seq += 1

    def pop(self) -> T | None:
        """Pop the highest-priority element (FIFO within equal priority)."""
        if not self._items:
            return None
        best = 0
        for i in range(1, len(self._items)):
            a_pri, a_seq, _ = self._items[i]
            b_pri, b_seq, _ = self._items[best]
            if a_pri > b_pri or (a_pri == b_pri and a_seq < b_seq):
                best = i
        _, _, value = self._items.pop(best)
        return value

    def __len__(self) -> int:
        return len(self._items)

    def is_empty(self) -> bool:
        return len(self._items) == 0


class KeyedRelay[K, T]:
    """Case 18 — keyed sharding. ``N`` independent relays keyed by ``K``; an op
    routes to its key's shard. Merging across shards requires a commutative merge.
    The converged per-key state equals a single relay per key."""

    __slots__ = ("_shards", "ctx", "high_water", "merge_policy", "overflow")

    def __init__(
        self,
        ctx: dict,
        high_water: int,
        overflow: Overflow,
        merge_policy: MergePolicy[T],
    ) -> None:
        self.ctx = ctx
        self.high_water = high_water
        self.overflow = overflow
        self.merge_policy = merge_policy
        self._shards: dict[K, RelayCell[T]] = {}

    def ingress(self, key: K, op: T) -> IngressOutcome:
        """Route ``op`` to ``key``'s shard, creating the shard on first use."""
        relay = self._shards.get(key)
        if relay is None:
            policy = BackpressurePolicy(
                self.ctx,
                BoundDim.COUNT,
                self.high_water,
                self.high_water // 2,
                self.overflow,
            )
            relay = RelayCell(self.ctx, policy, self.merge_policy)
            self._shards[key] = relay
        return relay.ingress(op)

    def drain(self, key: K) -> T | None:
        """Drain a key's coalesced window."""
        relay = self._shards.get(key)
        return None if relay is None else relay.drain()

    def keys(self) -> Iterator[K]:
        return iter(self._shards.keys())
