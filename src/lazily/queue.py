"""Reactive queue — ``QueueCell`` SPSC/MPSC + pluggable ``QueueStorage`` backend.

The Python counterpart of ``lazily-spec/cell-model.md`` § "Reactive queues" and
the Lean ``LazilyFormal.QueueCell`` formal model. A :class:`QueueCell` is a FIFO
collection composed of reactive cells — **not a new cell kind** — that adds
queue semantics (push to tail, pop from head) to the reactive graph.

It is specified as a **single-producer, single-consumer (SPSC)** primitive;
**MPSC** (multi-producer) is a *usage rule* on the same primitive — multiple
producers push inside a :func:`lazily.batch` boundary, and the batch serializes
the pushes into a deterministic order. There is no separate ``MPSCQueueCell``
type.

Shell vs storage
----------------

The reactive shell owns the reader-kind version cells (``head`` / ``len`` /
``is_empty`` / ``is_full`` / ``closed``) and the invalidation logic; it is
storage-agnostic. The storage backend owns the actual FIFO data structure and is
pluggable via the :class:`QueueStorage` protocol. The default
:class:`VecDequeStorage` is an unbounded deque; a bounded variant exposes
reactive backpressure via ``is_full``.

Reader-kind invalidation
------------------------

Invalidation is scoped to **reader kind**, not to individual positions. A push
invalidates ``len`` / ``is_empty`` readers (and ``head`` when transitioning from
empty, and ``is_full`` when transitioning to capacity); a pop invalidates
``head`` / ``len`` / ``is_empty`` readers (and ``is_full`` when transitioning off
capacity). The head reader observes the *current* head value — after a pop, the
head reader sees the next element (or ``None``), not a stale value.

This reader-kind independence is implemented for free by the existing ``!=``
(PartialEq) guard on :class:`lazily.cell.Cell`: after each op the shell re-derives
each reader-kind cell from the storage and writes it back, and a cell whose value
did not change is not invalidated.

Example
-------

>>> from lazily import QueueCell, batch
>>> ctx = {}
>>> q: QueueCell[str] = QueueCell(ctx)
>>> _ = q.try_push("a")
>>> _ = q.try_push("b")
>>> q.head()
'a'
>>> q.len()
2
>>> q.try_pop()
'a'
>>> q.try_pop()
'b'
>>> q.is_empty()
True
>>> # MPSC: multiple producers push inside one batch → one invalidation pass.
>>> batch(lambda: (q.try_push("p1"), q.try_push("p2"), q.try_push("p3")))
>>> q.len()
3
"""

from __future__ import annotations


__all__ = [
    "QueueCell",
    "QueuePopError",
    "QueuePushError",
    "QueueReaderHandles",
    "QueueStorage",
    "TopicCell",
    "TopicDurability",
    "TopicSnapshot",
    "TopicSubscribeOutcome",
    "TopicSubscriptionSnapshot",
    "VecDequeStorage",
]

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .batch import batch
from .cell import Cell
from .slot import Slot


if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class QueuePushError:
    """Failure reasons for :meth:`QueueStorage.try_push` / :meth:`QueueCell.try_push`.

    ``Full`` and ``Closed`` are the two observable rejection reasons
    distinguished by the shell's contract (``lazily-spec/cell-model.md`` §
    "Storage backend contract"). Neither changes queue state, so neither
    invalidates any reader.

    Used as singletons: ``QueuePushError.Full`` / ``QueuePushError.Closed``.
    """

    Full: QueuePushError
    Closed: QueuePushError

    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"QueuePushError.{self.label}"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, QueuePushError) and other.label == self.label

    def __hash__(self) -> int:
        return hash(("QueuePushError", self.label))


class QueuePopError:
    """Failure reasons for :meth:`QueueStorage.try_pop` / :meth:`QueueCell.try_pop`.

    ``Empty`` and ``Closed`` are distinct observable signals: ``Empty`` means
    "try again later," ``Closed`` means "the producer is done and the queue is
    drained."

    Used as singletons: ``QueuePopError.Empty`` / ``QueuePopError.Closed``.
    """

    Empty: QueuePopError
    Closed: QueuePopError

    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"QueuePopError.{self.label}"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, QueuePopError) and other.label == self.label

    def __hash__(self) -> int:
        return hash(("QueuePopError", self.label))


QueuePushError.Full = QueuePushError("Full")
QueuePushError.Closed = QueuePushError("Closed")
QueuePopError.Empty = QueuePopError("Empty")
QueuePopError.Closed = QueuePopError("Closed")


@runtime_checkable
class QueueStorage[T](Protocol):
    """Pluggable FIFO storage backend for a :class:`QueueCell`.

    The shell / storage split keeps the reactive shell storage-agnostic: the
    shell owns the demand-driven reader-kinds and invalidation logic, the backend
    owns the actual FIFO data structure. The default backend is
    :class:`VecDequeStorage` (unbounded deque); future backends include a
    consensus-backed store or external-broker adapters.

    **Minimal required contract:** ``try_push`` / ``try_pop`` / ``len`` /
    ``is_closed`` / ``close``. ``peek`` and ``capacity`` are **optional
    capabilities** — a raw-channel-style backend that satisfies only the five
    required methods is fully conforming; it simply has no ``head`` reader (no
    ``peek``) and no ``is_full`` reader (unbounded). A conforming backend MUST
    also:

    1. **FIFO order** — ``try_pop`` returns elements in ``try_push`` order.
    2. **Cardinality compatibility** — its native producer/consumer shape is a
       superset of the shell's required shape.
    3. **Bounded contract (optional)** — a bounded backend exposes
       :meth:`capacity` as a non-``None`` int and ``try_push`` returns
       :attr:`QueuePushError.Full` at capacity.
    4. **Position identity** — invalidation is phrased over reader kind, not
       storage indices.
    """

    def try_push(self, value: T) -> QueuePushError | None:
        """Append ``value`` to the tail. Returns ``Full`` if bounded and at
        capacity, or ``Closed`` if the queue is closed. Returns ``None`` on
        success. On error the queue state is unchanged."""
        ...

    def try_pop(self) -> Any:
        """Remove and return the head element. Returns :attr:`QueuePopError.Empty`
        if open and empty, or :attr:`QueuePopError.Closed` if closed and empty.
        Pop on a closed *non-empty* queue drains (returns the next element)."""
        ...

    def peek(self) -> Any:
        """**Optional capability.** Peek the current head element without
        removing it, ``None`` when empty. The shell derives its ``head`` reader
        from this; a backend without ``peek`` has no ``head`` reader."""
        ...

    def len(self) -> int:
        """Current number of buffered elements. **Required.**"""
        ...

    def capacity(self) -> int | None:
        """**Optional capability.** Bounded capacity, or ``None`` for an
        unbounded backend (the default when the method is absent)."""
        ...

    def is_closed(self) -> bool:
        """Whether the queue has been closed. Close is terminal."""
        ...

    def close(self) -> None:
        """Close the queue. Idempotent. After close, ``try_push`` returns
        ``Closed``; ``try_pop`` continues to drain and returns ``Closed`` only
        once empty."""
        ...


class VecDequeStorage[T]:
    """The reference :class:`QueueStorage` backend — a deque-backed FIFO,
    optionally bounded.

    The unbounded form (the default) is what :class:`QueueCell` uses; the bounded
    form (:meth:`with_capacity`) exposes reactive backpressure via the shell's
    ``is_full`` reader. The overflow policy is **reject** — ``try_push`` at
    capacity returns :attr:`QueuePushError.Full` (elements are never silently
    dropped); other backends may choose block / drop-oldest / drop-newest.
    """

    __slots__ = ("_capacity", "_closed", "_elements")

    def __init__(self, capacity: int | None = None) -> None:
        if capacity is not None and capacity <= 0:
            raise ValueError("VecDequeStorage capacity must be > 0")
        self._elements: deque[T] = deque()
        self._capacity = capacity
        self._closed = False

    @classmethod
    def unbounded(cls) -> VecDequeStorage[T]:
        """Create an unbounded storage (no capacity limit)."""
        return cls()

    @classmethod
    def with_capacity(cls, capacity: int) -> VecDequeStorage[T]:
        """Create a bounded storage that rejects pushes once it holds
        ``capacity`` elements."""
        return cls(capacity=capacity)

    def try_push(self, value: T) -> QueuePushError | None:
        if self._closed:
            return QueuePushError.Closed
        if self._capacity is not None and len(self._elements) >= self._capacity:
            return QueuePushError.Full
        self._elements.append(value)
        return None

    def try_pop(self) -> T | QueuePopError:
        if self._elements:
            return self._elements.popleft()
        return QueuePopError.Closed if self._closed else QueuePopError.Empty

    def peek(self) -> T | None:
        return self._elements[0] if self._elements else None

    def len(self) -> int:
        return len(self._elements)

    def capacity(self) -> int | None:
        return self._capacity

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    def elements(self) -> list[T]:
        """Snapshot the buffered elements in FIFO order. Non-reactive — for
        snapshot/serde and conformance-fixture verification."""
        return list(self._elements)


class QueueReaderHandles[T]:
    """Handles to all five reader-kinds of a :class:`QueueCell`, for effects that
    need to subscribe to several reader kinds at once. The four derived
    reader-kinds are demand-driven Slots; ``closed`` is a Cell (a direct input)."""

    __slots__ = ("closed", "head", "is_empty", "is_full", "len")

    def __init__(
        self,
        head: Slot[dict, dict, T | None],
        len: Slot[dict, dict, int],
        is_empty: Slot[dict, dict, bool],
        is_full: Slot[dict, dict, bool],
        closed: Cell[bool],
    ) -> None:
        self.head = head
        self.len = len
        self.is_empty = is_empty
        self.is_full = is_full
        self.closed = closed


class QueueCell[T]:
    """A reactive FIFO queue — SPSC primitive with an MPSC usage rule.

    The reactive shell wraps a pluggable :class:`QueueStorage` backend (default
    :class:`VecDequeStorage`); the shell owns the reader-kind version cells
    (``head`` / ``len`` / ``is_empty`` / ``is_full`` / ``closed``) and
    invalidates by reader kind — a push to a non-empty queue does NOT invalidate
    the ``head`` reader, a pop does.

    All reactive reads (``head`` / ``len`` / ``is_empty`` / ``is_full`` /
    ``closed``) subscribe to the corresponding reader-kind cell when read inside
    a :class:`~lazily.slot.Slot` / :class:`~lazily.signal.Computed` /
    :class:`~lazily.effect.Effect`, so derived computeds recompute exactly when
    their reader kind changes (reader-kind independence).
    """

    __slots__ = (
        "_capacity",
        "_closed",
        "_head",
        "_is_empty",
        "_is_full",
        "_len",
        "_storage",
        "ctx",
    )

    ctx: dict

    def __init__(
        self,
        ctx: dict,
        *,
        capacity: int | None = None,
        storage: QueueStorage[T] | None = None,
    ) -> None:
        self.ctx = ctx
        self._storage: QueueStorage[T] = (
            storage if storage is not None else VecDequeStorage(capacity=capacity)
        )
        storage_ = self._storage
        # `capacity` and `peek` are optional storage capabilities (Phase 0
        # #relaycell): a raw-channel backend exposes neither. Cache capacity once
        # (it is contractually fixed) and resolve `peek` to a no-op when absent.
        cap_fn = getattr(storage_, "capacity", None)
        self._capacity: int | None = cap_fn() if cap_fn is not None else None
        peek_fn = getattr(storage_, "peek", None)

        # Reader-kinds are demand-driven memoized Slots (were eagerly-set Cells):
        # each derives from storage on first read after invalidation and is reset
        # only on an op that provably changes it (see `_invalidate_readers`). This
        # gives reader-kind independence without deriving anything eagerly — an
        # unobserved op does no derivation. `closed` stays a Cell (it is a direct
        # input, set by `close`, not a derived value). py's Slot `reset` is lazy
        # (pop cache + notify), so a reader with no Effect subscriber pays no
        # eager work — store-without-cascade is inherent in the Slot model.
        capacity_ = self._capacity
        self._head: Slot[dict, dict, T | None] = Slot(
            lambda _ctx: peek_fn() if peek_fn is not None else None
        )
        self._len: Slot[dict, dict, int] = Slot(lambda _ctx: storage_.len())
        self._is_empty: Slot[dict, dict, bool] = Slot(lambda _ctx: storage_.len() == 0)
        self._is_full: Slot[dict, dict, bool] = Slot(
            lambda _ctx: capacity_ is not None and storage_.len() >= capacity_
        )
        self._closed: Cell[bool] = Cell(ctx, storage_.is_closed())

    @classmethod
    def with_capacity(cls, ctx: dict, capacity: int) -> QueueCell[T]:
        """Create a bounded queue with ``capacity``. Exposes reactive
        backpressure via :meth:`is_full`: a pop that transitions full → not-full
        invalidates ``is_full`` readers."""
        return cls(ctx, storage=VecDequeStorage.with_capacity(capacity))

    @classmethod
    def with_storage(cls, ctx: dict, storage: QueueStorage[T]) -> QueueCell[T]:
        """Build a queue over an arbitrary :class:`QueueStorage` backend. The
        shell initializes its reader-kind cells from the backend's current
        state."""
        return cls(ctx, storage=storage)

    def _invalidate_readers(
        self, len_before: int, len_after: int, head_changed: bool
    ) -> None:
        """Reset exactly the reader-kind Slots whose derived value changed on a
        successful op that took the queue from ``len_before`` to ``len_after``.
        No reader value is derived here — a reset only pops the Slot's cache (and
        notifies its subscribers), so each re-derives lazily on its next read;
        an unobserved reader with no subscriber pays effectively nothing.

        ``head_changed`` is passed by the caller because head depends on op
        *direction*, not just ``len`` (a pop always changes head; a push changes
        it only from empty) — so head invalidation needs no ``peek``. ``closed``
        is never touched here: it changes only via :meth:`close`.
        """
        ctx = self.ctx
        cap = self._capacity

        def resets() -> None:
            # `len` always changes on a successful op.
            self._len.reset(ctx)
            if (len_before == 0) != (len_after == 0):
                self._is_empty.reset(ctx)
            if cap is not None and (len_before >= cap) != (len_after >= cap):
                self._is_full.reset(ctx)
            if head_changed:
                self._head.reset(ctx)

        # Batch the resets: a push/pop is a single atomic op, so its reader-kinds
        # must transition together. `batch` coalesces subscribing Effects'
        # reruns to the boundary so an observer never sees a partial state (e.g.
        # `len` bumped before `is_full` flips). Outside a batch nothing eager
        # runs (Slot reset is lazy); only Effect subscribers rerun, once.
        batch(resets)

    # -- mutators ------------------------------------------------------- #

    def try_push(self, value: T) -> QueuePushError | None:
        """Append ``value`` to the tail of the queue.

        Returns :attr:`QueuePushError.Full` if bounded and at capacity (reject
        policy), or :attr:`QueuePushError.Closed` if the queue is closed. Returns
        ``None`` on success. On error the queue state is unchanged and no reader
        is invalidated.
        """
        len_before = self._storage.len()
        result = self._storage.try_push(value)
        if result is None:
            # Head changes on a push only when the queue was empty.
            self._invalidate_readers(len_before, len_before + 1, len_before == 0)
        return result

    def try_pop(self) -> T | QueuePopError:
        """Remove and return the head element.

        Returns :attr:`QueuePopError.Empty` if open and empty, or
        :attr:`QueuePopError.Closed` if closed and empty. Pop on a closed
        *non-empty* queue drains (returns the next element).
        """
        len_before = self._storage.len()
        result = self._storage.try_pop()
        if not isinstance(result, QueuePopError):
            # A successful pop always advances head and decrements len.
            self._invalidate_readers(len_before, len_before - 1, True)
        return result

    def close(self) -> None:
        """Close the queue. Idempotent — closing an already-closed queue is a
        no-op (no invalidation). Terminal — once closed, a queue cannot be
        reopened. Invalidates the ``closed`` reader only on the false → true
        transition."""
        if self._storage.is_closed():
            return
        self._storage.close()
        self._closed.value = True

    # -- reactive reader-kind reads ------------------------------------- #

    def head(self, ctx: Any = None) -> T | None:
        """Reactive read of the current head value. ``None`` when the queue is
        empty. A reader is invalidated when the head value *changes* — every
        pop, and a push only when transitioning from empty. Trivially ``None``
        when the backend has no ``peek`` capability.

        Pass the caller's :class:`~lazily.compute.Compute` view (``ctx``) to
        value-thread the dependency edge inside a reactive body; omit it for an
        untracked top-level read (``#lzcellkernel`` bare-read removal)."""
        return self._head(self.ctx if ctx is None else ctx)

    def len(self, ctx: Any = None) -> int:
        """Reactive read of the number of buffered elements. Invalidated
        whenever the count changes (every successful push/pop). See :meth:`head`
        on ``ctx``."""
        return self._len(self.ctx if ctx is None else ctx)

    def is_empty(self, ctx: Any = None) -> bool:
        """Reactive emptiness check. Invalidated only on the empty ↔ non-empty
        transition. See :meth:`head` on ``ctx``."""
        return self._is_empty(self.ctx if ctx is None else ctx)

    def is_full(self, ctx: Any = None) -> bool:
        """Reactive fullness check (only meaningful when the backend is bounded).
        Invalidated on the full ↔ not-full transition — this is the backpressure
        signal. For an unbounded backend this is always ``False`` and never
        invalidates. See :meth:`head` on ``ctx``."""
        return self._is_full(self.ctx if ctx is None else ctx)

    def is_closed(self, ctx: Any = None) -> bool:
        """Reactive read of the closed flag. Invalidated only on the open →
        closed transition. See :meth:`head` on ``ctx``."""
        if ctx is None:
            return self._closed.value
        return ctx.read(self._closed)

    # -- reader-kind cell handles (advanced wiring) --------------------- #

    def reader_handles(self) -> QueueReaderHandles[T]:
        """Handles to the reader-kinds, for effects that subscribe to multiple
        reader kinds at once. The four derived reader-kinds are demand-driven
        Slots; ``closed`` is a Cell (a direct input)."""
        return QueueReaderHandles(
            head=self._head,
            len=self._len,
            is_empty=self._is_empty,
            is_full=self._is_full,
            closed=self._closed,
        )

    # -- non-reactive storage access ------------------------------------ #

    def capacity(self) -> int | None:
        """The backend's capacity, or ``None`` if unbounded. Cached at
        construction (capacity is a fixed backend property)."""
        return self._capacity

    def elements(self) -> list[T]:
        """Snapshot the buffered elements in FIFO order. Non-reactive — for
        debugging, snapshot/serde, and conformance-fixture verification. There
        is no reactive random-access ``queue[N]`` reader; per-position
        reactivity is the domain of :class:`~lazily.collection.SourceMap`, not
        :class:`QueueCell`."""
        storage = self._storage
        getter: Callable[[], list[T]] | None = getattr(storage, "elements", None)
        if getter is not None:
            return getter()
        raise TypeError(
            f"storage backend {type(storage).__name__} does not expose a non-reactive "
            "`elements()` snapshot; use a VecDequeStorage-backed QueueCell for fixture "
            "verification"
        )


class TopicDurability(str, Enum):
    """Whether a subscription survives disconnect and participates in GC."""

    Durable = "durable"
    Ephemeral = "ephemeral"


class TopicSubscribeOutcome(str, Enum):
    """Result of subscribing a stable subscriber id."""

    Subscribed = "subscribed"
    Reconnected = "reconnected"
    AlreadySubscribed = "already_subscribed"


@dataclass(frozen=True, slots=True)
class TopicSubscriptionSnapshot:
    """Serializable absolute cursor state for one topic subscriber."""

    subscriber_id: str
    cursor: int
    durability: TopicDurability
    connected: bool


@dataclass(frozen=True, slots=True)
class TopicSnapshot[T]:
    """Serializable retained log and subscription state."""

    base_offset: int
    elements: tuple[T, ...]
    subscriptions: tuple[TopicSubscriptionSnapshot, ...]


@dataclass(slots=True)
class _TopicSubscription:
    cursor: int
    durability: TopicDurability
    connected: bool


class TopicCell[T]:
    """Broadcast log with an independent reactive cursor per subscriber."""

    def __init__(self, ctx: dict, snapshot: TopicSnapshot[T] | None = None) -> None:
        self._ctx = ctx
        self._base_offset = 0 if snapshot is None else snapshot.base_offset
        self._elements: deque[T] = deque(() if snapshot is None else snapshot.elements)
        self._subscriptions: dict[str, _TopicSubscription] = {}
        self._readers: dict[str, Slot[dict, dict, list[T]]] = {}
        if self._base_offset < 0:
            raise ValueError("topic base offset must be non-negative")
        if snapshot is not None:
            tail = self.tail_offset
            for saved in snapshot.subscriptions:
                if not self._base_offset <= saved.cursor <= tail:
                    raise ValueError(
                        "topic subscription cursor is outside the retained log"
                    )
                if not isinstance(saved.durability, TopicDurability):
                    raise ValueError("invalid topic subscription durability")
                if not isinstance(saved.connected, bool):
                    raise ValueError("topic connected flag must be boolean")
                if (
                    saved.durability is TopicDurability.Ephemeral
                    and not saved.connected
                ):
                    raise ValueError(
                        "disconnected ephemeral topic subscription must be removed"
                    )
                self._subscriptions[saved.subscriber_id] = _TopicSubscription(
                    saved.cursor, saved.durability, saved.connected
                )
                self._ensure_reader(saved.subscriber_id)

    @property
    def base_offset(self) -> int:
        return self._base_offset

    @property
    def tail_offset(self) -> int:
        return self._base_offset + len(self._elements)

    def _ensure_reader(self, subscriber_id: str) -> Slot[dict, dict, list[T]]:
        reader = self._readers.get(subscriber_id)
        if reader is None:
            reader = Slot(lambda _ctx, sid=subscriber_id: self.read_untracked(sid))
            self._readers[subscriber_id] = reader
        return reader

    def _reset_readers(self, subscriber_ids: list[str]) -> None:
        readers = [self._readers[sid] for sid in subscriber_ids if sid in self._readers]
        if readers:
            batch(lambda: [reader.reset(self._ctx) for reader in readers])

    def subscribe(
        self,
        subscriber_id: str,
        durability: TopicDurability = TopicDurability.Durable,
    ) -> TopicSubscribeOutcome:
        if not isinstance(durability, TopicDurability):
            raise ValueError("invalid topic subscription durability")
        existing = self._subscriptions.get(subscriber_id)
        if existing is not None:
            if existing.connected:
                return TopicSubscribeOutcome.AlreadySubscribed
            if existing.durability is not TopicDurability.Durable:
                raise ValueError("only durable subscriptions can reconnect")
            existing.connected = True
            self._reset_readers([subscriber_id])
            return TopicSubscribeOutcome.Reconnected
        self._subscriptions[subscriber_id] = _TopicSubscription(
            self.tail_offset, durability, True
        )
        self._ensure_reader(subscriber_id)
        return TopicSubscribeOutcome.Subscribed

    def reconnect(self, subscriber_id: str) -> None:
        subscription = self._subscriptions[subscriber_id]
        if subscription.durability is not TopicDurability.Durable:
            raise ValueError("only durable subscriptions can reconnect")
        if not subscription.connected:
            subscription.connected = True
            self._reset_readers([subscriber_id])

    def disconnect(self, subscriber_id: str) -> None:
        subscription = self._subscriptions[subscriber_id]
        if not subscription.connected:
            return
        subscription.connected = False
        if subscription.durability is TopicDurability.Ephemeral:
            del self._subscriptions[subscriber_id]
        self._reset_readers([subscriber_id])

    def publish(self, value: T) -> int:
        """Append a value and invalidate every connected reader independently."""

        offset = self.tail_offset
        self._elements.append(value)
        self._reset_readers(
            [sid for sid, sub in self._subscriptions.items() if sub.connected]
        )
        return offset

    def read_untracked(self, subscriber_id: str) -> list[T]:
        subscription = self._subscriptions.get(subscriber_id)
        if subscription is None or not subscription.connected:
            return []
        start = subscription.cursor - self._base_offset
        return list(self._elements)[start:]

    def read_stream(self, subscriber_id: str) -> list[T]:
        return self._ensure_reader(subscriber_id)(self._ctx)

    def read(self, subscriber_id: str) -> T | None:
        stream = self.read_stream(subscriber_id)
        return stream[0] if stream else None

    def advance(self, subscriber_id: str, count: int = 1) -> int:
        if count < 0:
            raise ValueError("advance count must be non-negative")
        subscription = self._subscriptions[subscriber_id]
        if not subscription.connected or subscription.cursor == self.tail_offset:
            return subscription.cursor
        new_cursor = subscription.cursor + count
        if new_cursor > self.tail_offset:
            raise ValueError("cannot advance beyond the topic tail")
        if new_cursor != subscription.cursor:
            subscription.cursor = new_cursor
            self._reset_readers([subscriber_id])
        return new_cursor

    def gc(self) -> int:
        durable_cursors = [
            sub.cursor
            for sub in self._subscriptions.values()
            if sub.durability is TopicDurability.Durable
        ]
        frontier = min(durable_cursors, default=self.tail_offset)
        removed = frontier - self._base_offset
        for _ in range(removed):
            self._elements.popleft()
        self._base_offset = frontier
        return removed

    def restart(self) -> None:
        """Model a process restart; persisted state and readers are unchanged."""

    def elements(self) -> list[T]:
        return list(self._elements)

    def subscription(self, subscriber_id: str) -> TopicSubscriptionSnapshot | None:
        sub = self._subscriptions.get(subscriber_id)
        if sub is None:
            return None
        return TopicSubscriptionSnapshot(
            subscriber_id, sub.cursor, sub.durability, sub.connected
        )

    def reader_handle(self, subscriber_id: str) -> Slot[dict, dict, list[T]]:
        return self._ensure_reader(subscriber_id)

    def snapshot(self) -> TopicSnapshot[T]:
        subscriptions = tuple(
            TopicSubscriptionSnapshot(sid, sub.cursor, sub.durability, sub.connected)
            for sid, sub in sorted(self._subscriptions.items())
        )
        return TopicSnapshot(self._base_offset, tuple(self._elements), subscriptions)
