"""Reliable sync protocol (``#lzsync``).

Delivery-reliability over the ``Snapshot`` / ``Delta`` / ``CrdtSync`` planes
(``lazily-spec`` § Reliable Sync): gap recovery, at-least-once outbox, and
OR-set / LWW liveness cells. The correctness backstop is ``lazily-formal``
``ReliableSync.lean``; the cross-language pins are
``lazily-spec/conformance/reliable-sync/``.

Three pure-protocol pieces (identical logic in every binding, no I/O / clock /
storage engine baked in):

- :class:`ResyncCoordinator` — receiver-side decision function over the inbound
  frame stream (``Apply`` / ``RequestSnapshot`` / ``Ignore``), multi-epoch-span
  aware.
- :class:`DurableOutbox` — sender-side at-least-once contract (append-before-
  send, ack-through, replay-from-cursor). Ships :class:`InMemoryOutbox` as the
  default; a host plugs a durable store (agent-doc: SQLite) behind the ABC, and
  the crash-replay conformance test exercises a reference file-backed impl.
- :class:`OrSet` / :class:`WireLwwRegister` — the liveness cells that ride the
  CrdtSync plane.

The reverse-channel control frames are :class:`~lazily.ipc.ResyncRequest` and
:class:`~lazily.ipc.OutboxAck` — variants on the same framed, codec-negotiated,
bidirectional message plane as ``Snapshot`` / ``Delta`` / ``CrdtSync``, so they
share one encode/decode path, one demux point, one FFI kind, and one in-band
order. They match the ``conformance/reliable-sync/`` fixtures and round-trip
through the JSON codec like the state frames.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .ipc import Delta, IpcMessage, OutboxAck, ResyncRequest, WireStamp


if TYPE_CHECKING:
    from collections.abc import Iterable


__all__ = [
    "Clock",
    "DriverError",
    "DurableOutbox",
    "InMemoryOutbox",
    "InMemoryStore",
    "OrSet",
    "Outbox",
    "OutboxStore",
    "Progress",
    "ResyncAction",
    "ResyncActionKind",
    "ResyncCoordinator",
    "SnapshotProvider",
    "SqliteOutbox",
    "SqliteStore",
    "SyncDriver",
    "WireLwwRegister",
]


# ---------------------------------------------------------------------------
# ResyncCoordinator — receiver-side decision function
# ---------------------------------------------------------------------------


class ResyncActionKind(Enum):
    """The three receiver decisions for an inbound frame."""

    APPLY = "apply"
    REQUEST_SNAPSHOT = "request_snapshot"
    IGNORE = "ignore"


@dataclass(frozen=True)
class ResyncAction:
    """Receiver decision for an inbound frame (spec § ResyncCoordinator).

    - ``Apply`` — apply the frame and advance the receiver epoch.
    - ``RequestSnapshot`` — a gap was detected; request a fresh ``Snapshot``
      covering ``from_epoch`` (the receiver's current ``last_epoch``).
    - ``Ignore`` — drop the frame (already-applied re-delivery, malformed, a
      duplicate request suppressed while resyncing, or a reverse-channel control
      frame arriving at a data receiver).
    """

    kind: ResyncActionKind
    from_epoch: int | None = None

    @classmethod
    def apply(cls) -> ResyncAction:
        return cls(ResyncActionKind.APPLY)

    @classmethod
    def request_snapshot(cls, from_epoch: int) -> ResyncAction:
        return cls(ResyncActionKind.REQUEST_SNAPSHOT, from_epoch=from_epoch)

    @classmethod
    def ignore(cls) -> ResyncAction:
        return cls(ResyncActionKind.IGNORE)

    @property
    def is_apply(self) -> bool:
        return self.kind is ResyncActionKind.APPLY

    @property
    def is_request_snapshot(self) -> bool:
        return self.kind is ResyncActionKind.REQUEST_SNAPSHOT

    @property
    def is_ignore(self) -> bool:
        return self.kind is ResyncActionKind.IGNORE


class ResyncCoordinator:
    """Receiver-side reliable-sync coordinator.

    Holds ``last_epoch`` (the highest epoch fully applied) and a ``resyncing``
    flag (a ``RequestSnapshot`` is outstanding until a covering ``Snapshot``
    lands, so further ahead-of-cursor deltas are ignored instead of
    re-requested).

    :meth:`ingest` advances ``last_epoch`` on ``Apply`` — the caller MUST fold
    the frame's ops into its projection on ``Apply``. This mirrors the
    ``ReliableSync.step`` Lean model.
    """

    __slots__ = ("_last_epoch", "_resyncing")

    def __init__(self, last_epoch: int = 0) -> None:
        self._last_epoch = last_epoch
        self._resyncing = False

    @classmethod
    def with_epoch(cls, last_epoch: int) -> ResyncCoordinator:
        """A coordinator that has already applied through ``last_epoch``."""
        return cls(last_epoch)

    @property
    def last_epoch(self) -> int:
        """The highest epoch fully applied."""
        return self._last_epoch

    @property
    def is_resyncing(self) -> bool:
        """Whether a resync request is outstanding (awaiting a snapshot)."""
        return self._resyncing

    def ingest_delta(self, delta: Delta) -> ResyncAction:
        """Classify + fold an inbound :class:`~lazily.ipc.Delta`.

        On ``Apply`` this advances ``last_epoch`` to ``delta.epoch``
        (multi-epoch-span aware) and clears ``resyncing``.
        """
        if delta.base_epoch == self._last_epoch:
            # Contiguous. Accept any span >= 1; reject an empty/backward epoch.
            if delta.epoch >= delta.base_epoch + 1:
                self._last_epoch = delta.epoch
                self._resyncing = False
                return ResyncAction.apply()
            return ResyncAction.ignore()
        if delta.base_epoch < self._last_epoch:
            # Already applied — a re-delivery (outbox replay / retry). Idempotent.
            return ResyncAction.ignore()
        # Gap: base_epoch > last_epoch. Request a covering snapshot once.
        if self._resyncing:
            return ResyncAction.ignore()
        self._resyncing = True
        return ResyncAction.request_snapshot(self._last_epoch)

    def ingest_snapshot(self, snapshot_epoch: int) -> ResyncAction:
        """Adopt a ``Snapshot`` at ``snapshot_epoch`` — a full-state frame
        always applies, setting ``last_epoch`` and clearing ``resyncing``."""
        self._last_epoch = snapshot_epoch
        self._resyncing = False
        return ResyncAction.apply()

    def ingest(self, msg: IpcMessage) -> ResyncAction:
        """Classify an inbound :class:`~lazily.ipc.IpcMessage`.

        ``CrdtSync`` is handled by the CRDT plane, and the reverse-channel
        control frames (``ResyncRequest`` / ``OutboxAck``) are for the *sender*'s
        driver, not this data receiver, so both are ``Ignore``d here.
        """
        if msg.snapshot is not None:
            return self.ingest_snapshot(msg.snapshot.epoch)
        if msg.delta is not None:
            return self.ingest_delta(msg.delta)
        return ResyncAction.ignore()

    def ack(self) -> IpcMessage:
        """The :class:`~lazily.ipc.OutboxAck` control frame advertising this
        receiver's resume cursor on reconnect (and for periodic retention
        advance)."""
        return IpcMessage.of_outbox_ack(OutboxAck(through_epoch=self._last_epoch))


# ---------------------------------------------------------------------------
# DurableOutbox — sender-side at-least-once contract
# ---------------------------------------------------------------------------


class DurableOutbox(ABC):
    """Sender-side at-least-once outbox contract (spec § DurableOutbox).

    Every frame is durably :meth:`append`ed **before** it is sent, retained
    until the peer proves receipt (:meth:`ack_through`), and
    :meth:`replay_from` a reconnect cursor re-sends everything the peer has not
    yet acked. Combined with the receiver's idempotent ``Ignore`` of
    already-applied deltas, this is at-least-once delivery with exactly-once
    effect.
    """

    @abstractmethod
    def append(self, epoch: int, msg: IpcMessage) -> None:
        """Persist ``msg`` at ``epoch`` before it is handed to the transport."""

    @abstractmethod
    def ack_through(self, epoch: int) -> None:
        """The peer proved receipt through ``epoch``; retained frames ``<=
        epoch`` MAY be pruned."""

    @abstractmethod
    def replay_from(self, cursor: int) -> list[tuple[int, IpcMessage]]:
        """Retained frames with ``epoch > cursor``, in ascending epoch order."""

    @abstractmethod
    def retained_epochs(self) -> list[int]:
        """Epochs still retained (not yet acked), ascending — for
        diagnostics/tests."""


class OutboxStore(Protocol):
    """Dumb ordered byte storage for :class:`Outbox`.

    Serialization, cursor monotonicity, pruning, and replay ordering stay in the
    shared protocol; persistent adapters implement only these five operations.
    """

    def put(self, epoch: int, frame: bytes) -> None: ...

    def delete_through(self, epoch: int) -> None: ...

    def scan_after(self, cursor: int) -> list[tuple[int, bytes]]: ...

    def load_cursor(self) -> int: ...

    def save_cursor(self, epoch: int) -> None: ...


class Outbox[S: OutboxStore](DurableOutbox):
    """Storage-independent append/ack/prune/replay protocol."""

    __slots__ = ("_acked_through", "_store")

    def __init__(self, store: S) -> None:
        self._store = store
        self._acked_through = store.load_cursor()

    @property
    def acked_through(self) -> int:
        """The highest peer acknowledgement loaded or observed."""
        self._acked_through = max(self._acked_through, self._store.load_cursor())
        return self._acked_through

    @property
    def store(self) -> S:
        return self._store

    def append(self, epoch: int, msg: IpcMessage) -> None:
        self._store.put(epoch, msg.encode_json())

    def ack_through(self, epoch: int) -> None:
        target = max(epoch, self._acked_through, self._store.load_cursor())
        if target > self._acked_through:
            self._store.save_cursor(target)
            self._acked_through = target
        self._store.delete_through(target)

    def replay_from(self, cursor: int) -> list[tuple[int, IpcMessage]]:
        effective_cursor = max(cursor, self.acked_through)
        return [
            (epoch, IpcMessage.decode_json(frame))
            for epoch, frame in self._store.scan_after(effective_cursor)
        ]

    def retained_epochs(self) -> list[int]:
        return [epoch for epoch, _frame in self._store.scan_after(self.acked_through)]


class InMemoryStore:
    """Ordered process-local :class:`OutboxStore`."""

    __slots__ = ("_cursor", "_entries")

    def __init__(self) -> None:
        self._entries: dict[int, bytes] = {}
        self._cursor = 0

    def put(self, epoch: int, frame: bytes) -> None:
        self._entries[epoch] = bytes(frame)

    def delete_through(self, epoch: int) -> None:
        self._entries = {e: frame for e, frame in self._entries.items() if e > epoch}

    def scan_after(self, cursor: int) -> list[tuple[int, bytes]]:
        return [
            (epoch, self._entries[epoch])
            for epoch in sorted(self._entries)
            if epoch > cursor
        ]

    def load_cursor(self) -> int:
        return self._cursor

    def save_cursor(self, epoch: int) -> None:
        self._cursor = max(self._cursor, epoch)


class InMemoryOutbox(Outbox[InMemoryStore]):
    """The default outbox, durable for the current process lifetime."""

    def __init__(self) -> None:
        super().__init__(InMemoryStore())


OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS reliable_sync_outbox (
    document_hash TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    frame_json BLOB NOT NULL,
    PRIMARY KEY (document_hash, epoch)
);
CREATE TABLE IF NOT EXISTS reliable_sync_outbox_cursor (
    document_hash TEXT PRIMARY KEY,
    acked_through INTEGER NOT NULL DEFAULT 0
);
"""


class SqliteStore:
    """SQLite :class:`OutboxStore`, namespaced by document hash."""

    __slots__ = ("_connection", "document_hash")

    def __init__(self, path: str | Path, document_hash: str) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self.document_hash = document_hash
        self._connection.executescript(OUTBOX_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def put(self, epoch: int, frame: bytes) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO reliable_sync_outbox "
                "(document_hash, epoch, frame_json) VALUES (?, ?, ?)",
                (self.document_hash, epoch, frame),
            )

    def delete_through(self, epoch: int) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM reliable_sync_outbox "
                "WHERE document_hash = ? AND epoch <= ?",
                (self.document_hash, epoch),
            )

    def scan_after(self, cursor: int) -> list[tuple[int, bytes]]:
        rows = self._connection.execute(
            "SELECT epoch, frame_json FROM reliable_sync_outbox "
            "WHERE document_hash = ? AND epoch > ? ORDER BY epoch ASC",
            (self.document_hash, cursor),
        )
        return [(int(epoch), bytes(frame)) for epoch, frame in rows]

    def load_cursor(self) -> int:
        row = self._connection.execute(
            "SELECT acked_through FROM reliable_sync_outbox_cursor "
            "WHERE document_hash = ?",
            (self.document_hash,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def save_cursor(self, epoch: int) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO reliable_sync_outbox_cursor "
                "(document_hash, acked_through) VALUES (?, ?) "
                "ON CONFLICT(document_hash) DO UPDATE SET "
                "acked_through = MAX(acked_through, excluded.acked_through)",
                (self.document_hash, epoch),
            )


class SqliteOutbox(Outbox[SqliteStore]):
    """Durable SQLite outbox ready for restart/reconnect replay."""

    def __init__(self, path: str | Path, document_hash: str) -> None:
        super().__init__(SqliteStore(path, document_hash))

    def close(self) -> None:
        self.store.close()


# ---------------------------------------------------------------------------
# Liveness cells (OR-set / LWW) on the CrdtSync plane
# ---------------------------------------------------------------------------


class OrSet:
    """An observed-remove set (OR-set) liveness cell.

    Models one entry's presence via add/remove tags: a ``(doc, pid)`` is
    *present* iff some add-tag is not shadowed by a remove that observed it.
    This gives the add-wins-over-stale-remove bias liveness needs (a re-open
    concurrent with a lagging close keeps the doc open). The :meth:`join` is the
    union of both tag sets, so it is a semilattice — out-of-order and duplicate
    delivery converge (``ReliableSync.joinOR_*``,
    ``orset_add_wins_over_stale_remove``).
    """

    __slots__ = ("_adds", "_removes")

    def __init__(self) -> None:
        self._adds: set[str] = set()
        self._removes: set[str] = set()

    def add(self, tag: str) -> None:
        """Add a presence tag (an editor open / attach event mints a fresh
        tag)."""
        self._adds.add(tag)

    def remove_observed(self, tags: Iterable[str]) -> None:
        """Remove, observing ``tags`` — only the add-tags this remove saw are
        shadowed."""
        self._removes.update(tags)

    def present(self) -> bool:
        """Whether the entry is currently present (some add-tag not
        shadowed)."""
        return bool(self._adds - self._removes)

    def join(self, other: OrSet) -> None:
        """Join another replica's OR-set (union of adds and of removes)."""
        self._adds |= other._adds
        self._removes |= other._removes

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OrSet):
            return NotImplemented
        return self._adds == other._adds and self._removes == other._removes

    def __hash__(self) -> int:  # pragma: no cover - identity-mutable, rarely hashed
        return hash((frozenset(self._adds), frozenset(self._removes)))


def _stamp_key(stamp: WireStamp) -> tuple[int, int, int]:
    """Total-order key ``(wall_time, logical, peer)`` for a :class:`WireStamp`."""
    return (stamp.wall_time, stamp.logical, stamp.peer)


class WireLwwRegister[V]:
    """A last-writer-wins register liveness cell (per-pid ``alive``, owner
    lease).

    Keyed by :class:`~lazily.ipc.WireStamp` (``(wall_time, logical, peer)``
    total order): the highest stamp wins, so an OS process-exit write (``alive
    = False`` at a fresh stamp) dominates a stale re-assert. Join is the
    stamp-max, a semilattice (``ReliableSync.joinReg_*``).
    """

    __slots__ = ("_stamp", "_value")

    def __init__(self, stamp: WireStamp, value: V) -> None:
        self._stamp = stamp
        self._value = value

    @property
    def value(self) -> V:
        """The current value."""
        return self._value

    @property
    def stamp(self) -> WireStamp:
        """The current decisive stamp."""
        return self._stamp

    def set(self, stamp: WireStamp, value: V) -> None:
        """Write ``value`` at ``stamp`` iff it dominates the current stamp."""
        if _stamp_key(stamp) > _stamp_key(self._stamp):
            self._stamp = stamp
            self._value = value

    def join(self, other: WireLwwRegister[V]) -> None:
        """Join another replica's register (keep the higher stamp)."""
        if _stamp_key(other._stamp) > _stamp_key(self._stamp):
            self._stamp = other._stamp
            self._value = other._value


# ---------------------------------------------------------------------------
# SyncDriver — full-duplex reliable-sync loop
# ---------------------------------------------------------------------------


class Clock(Protocol):
    """Monotonic clock seam (spec § SyncDriver — policy injected, no runtime in
    core).

    The driver never *schedules* itself; the host calls :meth:`SyncDriver.tick`
    on its own cadence and supplies wall-free monotonic millis so the driver can
    timestamp progress and expose a stall signal without owning a clock source.
    """

    def now_millis(self) -> int:
        """Milliseconds from an arbitrary fixed origin; monotonic,
        non-decreasing."""
        ...


class SnapshotProvider(Protocol):
    """Sender-side answer to a peer's ``ResyncRequest`` (spec § SyncDriver).

    When a receiver detects a gap it can no longer close from retained deltas,
    it asks for a covering ``Snapshot``; the host plugs its projection in here to
    produce one at ``epoch >= from_epoch``. This is the app-supplied half of the
    ``resync_convergence`` guarantee (drop the delta suffix, adopt the snapshot).
    """

    def snapshot(self, from_epoch: int) -> IpcMessage:
        """A full-state ``Snapshot`` :class:`~lazily.ipc.IpcMessage` covering
        ``from_epoch`` (its ``epoch`` MUST be ``>= from_epoch``)."""
        ...


class IpcSink(Protocol):
    """Transport sink for IPC messages (spec § SyncDriver transport seam).

    :meth:`send` returns ``True`` on success and ``False`` on a transport
    failure — a failed send is retained-and-stalled by the driver, not raised.
    """

    def send(self, message: IpcMessage) -> bool:
        """Send one IPC protocol message; ``True`` on success."""
        ...


class IpcSource(Protocol):
    """Transport source for IPC messages (spec § SyncDriver transport seam).

    :meth:`recv` returns the next message, ``None`` when the source is currently
    exhausted, or raises to signal a read failure (surfaced as
    :class:`DriverError`).
    """

    def recv(self) -> IpcMessage | None:
        """Receive the next IPC message, or ``None`` when exhausted."""
        ...


@dataclass
class Progress:
    """What one :meth:`SyncDriver.tick` accomplished (spec § SyncDriver).

    ``applied`` are the inbound ``Snapshot`` / ``Delta`` / ``CrdtSync`` frames
    the host MUST fold into its projection this tick — the driver has already
    advanced the receiver cursor for them, so folding is the caller's remaining
    obligation.
    """

    #: Data frames pushed to the sink this tick (fresh enqueues + replays).
    sent: int = 0
    #: Inbound frames the host must fold into its projection (``Apply``ed).
    applied: list[IpcMessage] = field(default_factory=list)
    #: A gap was detected inbound and a ``ResyncRequest`` was emitted.
    resync_requested: bool = False
    #: Inbound ``ResyncRequest``s answered with a provider snapshot this tick.
    snapshots_served: int = 0
    #: The peer's ack cursor after this tick (outbox retention / resume point).
    peer_acked_through: int = 0
    #: Outbox frames still unacked (retained for reconnect replay).
    retained: int = 0


class DriverError(Exception):
    """A transport error surfaced by :meth:`SyncDriver.tick`.

    A *sink* failure is not fatal — the frame is retained in the outbox and
    replayed on the next :meth:`SyncDriver.on_reconnect`, per the spec's
    retain-on-fail / resync-on-reconnect loop shape — so it is reported as a
    stall, not an error. Only a *source* read failure is raised as an error
    (``kind == "Source"``), signalling the host to re-establish the transport
    and call :meth:`SyncDriver.on_reconnect`.
    """

    def __init__(self, kind: str, error: object) -> None:
        super().__init__(f"{kind}: {error!r}")
        self.kind = kind
        self.error = error

    @classmethod
    def source(cls, error: object) -> DriverError:
        """The inbound source failed to read; the host should reconnect."""
        return cls("Source", error)


class SyncDriver:
    """Full-duplex reliable-sync loop driver (spec § SyncDriver).

    One driver drives one peer connection over a caller-supplied
    :class:`IpcSink` / :class:`IpcSource` pair (agent-doc wraps its Unix-domain
    socket). It composes the three pure-protocol pieces into the loop shape the
    spec pins:

    1. **drain** — pop host-enqueued outbound data frames, :meth:`~DurableOutbox.
       append` each to the outbox *before* sending (at-least-once durability),
       send via the sink;
    2. **retain-on-fail** — a send error leaves the frame in the outbox
       (unacked) and stops the drain; it is re-sent on the next reconnect;
    3. **receive** — read inbound frames, route control frames (``OutboxAck`` ->
       advance retention; ``ResyncRequest`` -> answer with a provider snapshot)
       and feed data frames through the :class:`ResyncCoordinator` (``Apply`` ->
       hand to the host + owe an ack; ``RequestSnapshot`` -> emit a
       ``ResyncRequest``; ``Ignore`` -> drop);
    4. **resync-on-reconnect** — :meth:`on_reconnect` replays the unacked outbox
       suffix from the peer's ack cursor and re-advertises our own receiver
       cursor, so a dropped-frame gap converges.

    The driver owns no threads, no clock source, and no storage engine — the
    host injects all three (:class:`Clock`, the transport pair, the outbox) and
    decides the tick cadence. Threading and backoff are host policy.
    """

    def __init__(
        self,
        sink: IpcSink,
        source: IpcSource,
        outbox: DurableOutbox,
        clock: Clock,
        provider: SnapshotProvider,
        last_epoch: int = 0,
    ) -> None:
        self._sink = sink
        self._source = source
        self._outbox = outbox
        self._clock = clock
        self._provider = provider
        self._coordinator = ResyncCoordinator.with_epoch(last_epoch)
        # Host-enqueued outbound data frames staged before append-then-send.
        self._pending: deque[tuple[int, IpcMessage]] = deque()
        # Highest epoch the peer has acked — outbox retention + resume cursor.
        self._peer_acked_through = 0
        # We applied an inbound frame and owe the peer an OutboxAck.
        self._ack_owed = False
        # A reconnect happened; the next tick replays the unacked suffix.
        self._replay_pending = False
        # millis since the last sink send failure; None when the sink is healthy.
        self._stalled_since: int | None = None

    @classmethod
    def new(
        cls,
        sink: IpcSink,
        source: IpcSource,
        outbox: DurableOutbox,
        clock: Clock,
        provider: SnapshotProvider,
    ) -> SyncDriver:
        """A fresh driver at receiver epoch 0 (a ``Snapshot`` seeds the first
        epoch)."""
        return cls(sink, source, outbox, clock, provider, 0)

    @classmethod
    def with_epoch(
        cls,
        sink: IpcSink,
        source: IpcSource,
        outbox: DurableOutbox,
        clock: Clock,
        provider: SnapshotProvider,
        last_epoch: int,
    ) -> SyncDriver:
        """A driver whose receiver has already applied through ``last_epoch``
        (resume)."""
        return cls(sink, source, outbox, clock, provider, last_epoch)

    def enqueue(self, epoch: int, msg: IpcMessage) -> None:
        """Stage an outbound data frame at ``epoch`` for the next tick's drain.

        ``epoch`` is the frame's accepted-event count (``Delta.epoch`` /
        ``Snapshot.epoch``); it becomes the outbox retention key.
        """
        self._pending.append((epoch, msg))

    def on_reconnect(self) -> None:
        """Signal that the transport was re-established; the next :meth:`tick`
        replays the unacked outbox suffix and re-advertises our receiver
        cursor."""
        self._replay_pending = True
        self._ack_owed = True
        self._stalled_since = None

    def last_epoch(self) -> int:
        """The receiver's current applied epoch."""
        return self._coordinator.last_epoch

    def is_stalled(self) -> bool:
        """Whether the sink is currently stalled (last send failed, awaiting
        reconnect)."""
        return self._stalled_since is not None

    def stalled_for(self, now: int) -> int:
        """Millis the sink has been stalled as of ``now``, or ``0`` when healthy
        — a backoff signal for the host scheduler (which owns cadence/backoff
        policy)."""
        if self._stalled_since is None:
            return 0
        return max(0, now - self._stalled_since)

    def outbox(self) -> DurableOutbox:
        """Borrow the underlying outbox (diagnostics / durable-store flush)."""
        return self._outbox

    def tick(self) -> Progress:
        """Run one loop pass. See the class docs for the drain -> retain ->
        receive -> resync shape. Sink failures retain-and-stall (not an error);
        only an inbound source read failure raises :class:`DriverError`."""
        now = self._clock.now_millis()
        progress = Progress()

        # 1. resync-on-reconnect: replay the unacked outbox suffix, oldest first.
        if self._replay_pending:
            self._replay_pending = False
            for _epoch, msg in self._outbox.replay_from(self._peer_acked_through):
                if self._sink.send(msg):
                    progress.sent += 1
                else:
                    self._stalled_since = now
                    # finish the replay after the next reconnect
                    self._replay_pending = True
                    break

        # 2. drain fresh enqueues: append-before-send, retain-and-stop on
        #    failure. A pre-existing stall (a prior failed send, no reconnect
        #    yet) skips the drain entirely — do not push into a sink already
        #    known to be down.
        while self._stalled_since is None and self._pending:
            epoch, msg = self._pending[0]
            self._outbox.append(epoch, msg)
            self._pending.popleft()
            if self._sink.send(msg):
                progress.sent += 1
                self._stalled_since = None
            else:
                # Retained in the outbox (unacked) → replayed on reconnect.
                self._stalled_since = now
                break

        # 3. receive: route control frames + feed data frames to coordinator.
        while True:
            try:
                msg = self._source.recv()
            except Exception as exc:
                raise DriverError.source(exc) from exc
            if msg is None:
                break
            if msg.outbox_ack is not None:
                if msg.outbox_ack.through_epoch > self._peer_acked_through:
                    self._peer_acked_through = msg.outbox_ack.through_epoch
                self._outbox.ack_through(msg.outbox_ack.through_epoch)
            elif msg.resync_request is not None:
                snap = self._provider.snapshot(msg.resync_request.from_epoch)
                if self._sink.send(snap):
                    progress.snapshots_served += 1
                else:
                    self._stalled_since = now
            elif msg.crdt_sync is not None:
                # Idempotent anti-entropy plane — the host folds it directly.
                progress.applied.append(msg)
            elif msg.snapshot is not None or msg.delta is not None:
                # Snapshot / Delta → the reliable-sync coordinator.
                action = self._coordinator.ingest(msg)
                if action.is_apply:
                    self._ack_owed = True
                    progress.applied.append(msg)
                elif action.is_request_snapshot:
                    assert action.from_epoch is not None
                    req = IpcMessage.of_resync_request(
                        ResyncRequest(from_epoch=action.from_epoch)
                    )
                    if self._sink.send(req):
                        progress.resync_requested = True
                    else:
                        self._stalled_since = now
                # Ignore → drop.
            else:
                # `IpcMessage` is a five-variant externally tagged union and
                # `IpcMessage.from_wire` already rejects an unknown tag, so the
                # only value that reaches here is a locally built message with
                # every variant slot unset. The old `else` fed that empty
                # message to the reliable-sync coordinator as if it were a
                # Snapshot/Delta.
                raise ValueError(
                    "IpcMessage carries no Snapshot, Delta, CrdtSync, "
                    "ResyncRequest, nor OutboxAck"
                )

        # 4. advertise our receiver cursor if we applied anything (retry until
        #    sent).
        if self._ack_owed and self._sink.send(self._coordinator.ack()):
            self._ack_owed = False

        progress.peer_acked_through = self._peer_acked_through
        progress.retained = len(self._outbox.retained_epochs())
        return progress
