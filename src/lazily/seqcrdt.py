"""Move-aware sequence CRDT — ``SeqCrdt``.

The Python counterpart of ``lazily-spec/cell-model.md`` § "Move-aware sequence
order". Sibling order under concurrency is a separate **composition** above
per-cell value merge: a move-aware sequence CRDT (fractional-index positions
tiebroken by peer). It conforms when:

- a move is a **single LWW reassignment** of an element's position — not
  delete + reinsert — so two concurrent moves of the same element converge to
  the later one **without duplication**;
- a concurrent move + value-edit of one element both apply (position and value
  are independent registers); and
- removal is an LWW tombstone.

Each element is three independent LWW registers — value, position, deleted —
each stamped by an HLC. Order is the lexicographic total order on
``(frac, peer)``. This is the order layer beneath keyed reconciliation; it lives
only at the multi-writer boundary.

The executable reference behind ``conformance/collections/seqcrdt_convergence.json``.
"""

from __future__ import annotations


__all__ = ["SeqCrdt", "SeqElement", "SeqStamp"]


from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Position:
    """A fractional-index position: ``frac`` byte key + ``peer`` tiebreak.

    Order is the lexicographic total order on ``(frac, peer)``. ``frac`` is a
    tuple of ints (a byte-string-like key); a position between two siblings is a
    fresh key strictly between their frac keys.
    """

    frac: tuple[int, ...]
    peer: int

    def __lt__(self, other: Position) -> bool:
        if self.frac == other.frac:
            return self.peer < other.peer
        return self.frac < other.frac

    def __le__(self, other: Position) -> bool:
        return self == other or self < other


@dataclass(frozen=True, slots=True, order=True)
class SeqStamp:
    """A hybrid-logical-clock stamp — the total order ``(wall, logical, peer)``.

    Field order IS the comparison order, so the generated ``__lt__``/``__gt__``
    already give the (wall, logical) causal order with ``peer`` as the final
    tiebreaker. That last component is not decoration: LWW adopts only on
    strictly-greater, so two replicas that could mint an identical triple would
    each refuse the other's write and diverge permanently (``#lzzigforkhlcpeer``).
    """

    wall: int
    logical: int
    peer: int


#: The stamp every register sorts above — the "never written" floor. Used for
#: ``deleted``, which starts un-stamped.
_ZERO_STAMP = SeqStamp(0, 0, 0)


@dataclass(slots=True)
class _SeqClock:
    """A caller-driven hybrid logical clock — the causal position only.

    Deliberately does NOT hold the peer. The peer lives on the replica and is
    stamped in at :meth:`SeqCrdt._stamp` time, which is what makes it
    structurally impossible for a fork to inherit the SOURCE's tiebreaker: there
    is no peer here to copy. lazily-zig shipped that exact bug — a fork that
    carried the whole clock, peer included — while fixing the carry itself
    (``#lzzigforkhlcpeer``).

    Never reads the system clock: every mutator takes ``now``, so replay is
    deterministic and skew is expressible in a test.
    """

    last_wall: int = 0
    last_logical: int = 0

    def send(self, now: int) -> tuple[int, int]:
        """Advance for a local event at caller-supplied wall time ``now``.

        The result is strictly greater than every position this clock has minted
        or observed, so a local write can never lose to state the replica
        already holds — including when ``now`` runs backwards.
        """
        if now > self.last_wall:
            self.last_wall = now
            self.last_logical = 0
        else:
            self.last_logical += 1
        return self.last_wall, self.last_logical

    def recv(self, remote: SeqStamp, now: int) -> None:
        """Advance past an observed ``remote`` stamp at wall time ``now``."""
        wall = max(self.last_wall, remote.wall, now)
        if wall == self.last_wall and wall == remote.wall:
            self.last_logical = max(self.last_logical, remote.logical) + 1
        elif wall == self.last_wall:
            self.last_logical += 1
        elif wall == remote.wall:
            self.last_logical = remote.logical + 1
        else:
            self.last_logical = 0
        self.last_wall = wall

    def copy(self) -> _SeqClock:
        return _SeqClock(self.last_wall, self.last_logical)


@dataclass
class SeqElement[V]:
    """One sequence element — three independent LWW registers.

    ``value``, ``position``, and ``deleted`` are each stamped by an HLC; a move
    is a single LWW reassignment of ``position``, a value edit a single LWW
    reassignment of ``value``, and removal an LWW tombstone on ``deleted``.
    """

    id: str
    value: V
    value_stamp: SeqStamp
    position: Position
    position_stamp: SeqStamp
    deleted: bool = False
    deleted_stamp: SeqStamp = field(default=_ZERO_STAMP)

    def max_stamp(self) -> SeqStamp:
        """The greatest of this element's three register stamps."""
        return max(self.value_stamp, self.position_stamp, self.deleted_stamp)


def _frac_between(lo: tuple[int, ...], hi: tuple[int, ...]) -> tuple[int, ...]:
    """A frac key strictly between two ordered keys ``lo < hi``.

    Treats past-end of ``lo`` as ``0`` and past-end of ``hi`` as ``256`` so a
    gap always exists eventually. The result shares the common prefix of
    ``lo`` / ``hi`` and places a mid value in the first gap, so it is strictly
    greater than ``lo`` and strictly less than ``hi``.
    """
    i = 0
    while True:
        a = lo[i] if i < len(lo) else 0
        b = hi[i] if i < len(hi) else 256
        if b - a > 1:
            mid = a + (b - a) // 2
            return (*lo[:i], mid)
        i += 1


def _frac_after(lo: tuple[int, ...]) -> tuple[int, ...]:
    """A frac key strictly greater than ``lo`` (unbounded upper — append)."""
    return (*lo, 128)


class SeqCrdt[V]:
    """A move-aware sequence CRDT.

    ``insert_back`` / ``insert_front`` mint an element between the end (or
    front) and its neighbor. ``move_after`` is a single LWW reassignment of an
    element's position — concurrent moves converge to the later stamp without
    duplication. ``set_value`` is an independent LWW reassignment of the value
    register, so a concurrent move + value-edit both apply. ``remove`` is an LWW
    tombstone.
    """

    __slots__ = ("_clock", "_elements", "peer")

    def __init__(self, peer: int) -> None:
        self.peer = peer
        self._clock = _SeqClock()
        self._elements: dict[str, SeqElement[V]] = {}

    # -- seed / clone / fork -------------------------------------------- #

    @classmethod
    def seed(cls, peer: int, inserts: list[dict[str, Any]]) -> SeqCrdt[Any]:
        """Construct from a fixture ``seed.inserts`` list."""
        buf: SeqCrdt[Any] = cls(peer)
        for ins in inserts:
            buf._insert_back(ins["id"], ins["value"], ins["now"])
        return buf

    def clone(self) -> SeqCrdt[V]:
        """A deep copy under the SAME peer — a snapshot, not a second writer.

        Carries the clock, because a copy that restarted at zero would regress
        exactly the way a fork would: its next local op could mint a stamp
        behind state it already holds and lose its own write.
        """
        return self.fork(self.peer)

    def fork(self, peer: int) -> SeqCrdt[V]:
        """A deep copy re-owned under ``peer`` — a new minting identity.

        Two independent things, and both matter (``#lzzigforkhlcpeer``):

        The causal POSITION carries. A fork has already observed everything the
        source holds, so a clock restarted at zero would let an ordinary skewed
        ``now`` — below the source's last wall time, which is the entire reason
        hybrid logical clocks exist — mint a stamp causally *behind* state the
        fork already carries. Every register adopts only on strictly-greater, so
        that write is silently dropped by the fork's own state.

        The PEER does not carry. It is the stamp's final tiebreaker, so two
        replicas stamping under one id can mint the identical
        ``(wall, logical, peer)``; a tie means *neither* side adopts and they
        diverge permanently. Here that is structural rather than remembered —
        :class:`_SeqClock` holds no peer, and :meth:`_stamp` reads
        :attr:`peer` off the replica — so reassigning ``peer`` on a
        :meth:`clone` is the same fork as calling this.
        """
        dup: SeqCrdt[V] = SeqCrdt(peer)
        dup._clock = self._clock.copy()
        dup._elements = {
            k: SeqElement(
                e.id,
                e.value,
                e.value_stamp,
                e.position,
                e.position_stamp,
                e.deleted,
                e.deleted_stamp,
            )
            for k, e in self._elements.items()
        }
        return dup

    # -- internal helpers ----------------------------------------------- #

    def _stamp(self, now: int) -> SeqStamp:
        """Mint the next local stamp under THIS replica's peer."""
        wall, logical = self._clock.send(now)
        return SeqStamp(wall, logical, self.peer)

    def _ordered_alive(self) -> list[SeqElement[V]]:
        alive = [e for e in self._elements.values() if not e.deleted]
        alive.sort(key=lambda e: e.position)
        return alive

    def _insert_at(
        self, elem_id: str, value: V, now: int, lo: tuple[int, ...], hi: tuple[int, ...]
    ) -> None:
        frac = _frac_between(lo, hi)
        stamp = self._stamp(now)
        self._elements[elem_id] = SeqElement(
            id=elem_id,
            value=value,
            value_stamp=stamp,
            position=Position(frac, self.peer),
            position_stamp=stamp,
        )

    def _insert_back(self, elem_id: str, value: V, now: int) -> None:
        ordered = self._ordered_alive()
        last_frac = ordered[-1].position.frac if ordered else ()
        frac = _frac_after(last_frac)
        stamp = self._stamp(now)
        self._elements[elem_id] = SeqElement(
            id=elem_id,
            value=value,
            value_stamp=stamp,
            position=Position(frac, self.peer),
            position_stamp=stamp,
        )

    def _insert_front(self, elem_id: str, value: V, now: int) -> None:
        ordered = self._ordered_alive()
        first_frac = ordered[0].position.frac if ordered else (256,)
        frac = _frac_between((), first_frac)
        stamp = self._stamp(now)
        self._elements[elem_id] = SeqElement(
            id=elem_id,
            value=value,
            value_stamp=stamp,
            position=Position(frac, self.peer),
            position_stamp=stamp,
        )

    # -- public mutators ------------------------------------------------ #

    def insert_back(self, elem_id: str, value: V, now: int) -> None:
        self._insert_back(elem_id, value, now)

    def insert_front(self, elem_id: str, value: V, now: int) -> None:
        self._insert_front(elem_id, value, now)

    def move_after(self, elem_id: str, anchor: str, now: int) -> None:
        """A single LWW reassignment of ``elem_id``'s position to just after
        ``anchor``. Concurrent moves converge to the later stamp."""
        ordered = self._ordered_alive()
        anchor_idx = next((i for i, e in enumerate(ordered) if e.id == anchor), None)
        if anchor_idx is None:
            return
        anchor_pos = ordered[anchor_idx].position
        # Next alive element after the anchor is the upper bound; if there is
        # none, append unbounded.
        if anchor_idx + 1 < len(ordered):
            frac = _frac_between(anchor_pos.frac, ordered[anchor_idx + 1].position.frac)
        else:
            frac = _frac_after(anchor_pos.frac)
        elem = self._elements.get(elem_id)
        if elem is None:
            return
        # LWW reassignment: later stamp wins. `_stamp` is strictly greater than
        # anything this replica has minted or observed, so a local move never
        # loses to the state it is moving.
        stamp = self._stamp(now)
        if stamp > elem.position_stamp:
            elem.position = Position(frac, self.peer)
            elem.position_stamp = stamp

    def set_value(self, elem_id: str, value: V, now: int) -> None:
        """An independent LWW reassignment of the value register."""
        elem = self._elements.get(elem_id)
        if elem is None:
            return
        stamp = self._stamp(now)
        if stamp > elem.value_stamp:
            elem.value = value
            elem.value_stamp = stamp

    def remove(self, elem_id: str, now: int) -> None:
        """An LWW tombstone."""
        elem = self._elements.get(elem_id)
        if elem is None:
            return
        stamp = self._stamp(now)
        if stamp > elem.deleted_stamp:
            elem.deleted = True
            elem.deleted_stamp = stamp

    # -- merge ---------------------------------------------------------- #

    def merge(self, other: SeqCrdt[V], now: int | None = None) -> bool:
        """Merge ``other`` into ``self`` (three independent LWW registers).

        Returns whether ``self`` changed. Position/value/deleted are each merged
        by stamp; a concurrent move + value-edit therefore both apply.

        Advances the local clock past every stamp observed here, so a later
        local write is causally ahead of the state just merged in rather than
        racing it.
        """
        now_micros = 0 if now is None else now
        for other_elem in other._elements.values():
            self._clock.recv(other_elem.max_stamp(), now_micros)
        changed = False
        for elem_id, other_elem in other._elements.items():
            existing = self._elements.get(elem_id)
            if existing is None:
                self._elements[elem_id] = SeqElement(
                    other_elem.id,
                    other_elem.value,
                    other_elem.value_stamp,
                    other_elem.position,
                    other_elem.position_stamp,
                    other_elem.deleted,
                    other_elem.deleted_stamp,
                )
                changed = True
                continue
            if other_elem.position_stamp > existing.position_stamp:
                existing.position = other_elem.position
                existing.position_stamp = other_elem.position_stamp
                changed = True
            if other_elem.value_stamp > existing.value_stamp:
                existing.value = other_elem.value
                existing.value_stamp = other_elem.value_stamp
                changed = True
            if other_elem.deleted_stamp > existing.deleted_stamp:
                existing.deleted = other_elem.deleted or existing.deleted
                existing.deleted_stamp = other_elem.deleted_stamp
                changed = True
        return changed

    # -- projections ---------------------------------------------------- #

    def order(self) -> list[str]:
        """Alive element ids in ``(frac, peer)`` order."""
        return [e.id for e in self._ordered_alive()]

    def __len__(self) -> int:
        return sum(1 for e in self._elements.values() if not e.deleted)

    def __contains__(self, elem_id: object) -> bool:
        e = self._elements.get(elem_id)  # type: ignore[arg-type]
        return e is not None and not e.deleted

    def get(self, elem_id: str) -> V | None:
        e = self._elements.get(elem_id)
        return e.value if e is not None and not e.deleted else None
