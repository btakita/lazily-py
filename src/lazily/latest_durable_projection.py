"""Latest-value durable projection authority and reactive shells.

``LatestDurableProjectionCore`` is the graph-agnostic egress state machine from
``lazily-spec``.  It deliberately is not a command queue: pending writes for one
key conflate to the newest epoch, while a claimed write remains the sole
in-flight attempt until its exact generation/epoch token succeeds or fails.

The three shells expose the same synchronous transition surface.  Async colour
belongs to the sink driver which awaits I/O; deciding what may be claimed and
which acknowledgement is authoritative is immediate state-machine work.

Spec: ``lazily-spec/docs/latest-durable-projection.md``.
Formal model: ``lazily-formal`` v0.38.1,
``LazilyFormal.LatestDurableProjection``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .slot import Slot
from .thread_safe import ThreadSafeContext


if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = [
    "AsyncLatestDurableProjection",
    "LatestDurableAck",
    "LatestDurableAckKind",
    "LatestDurableChange",
    "LatestDurableClaim",
    "LatestDurableClaimKind",
    "LatestDurableEnvelope",
    "LatestDurableFailure",
    "LatestDurableFailureKind",
    "LatestDurableKeyState",
    "LatestDurableProjection",
    "LatestDurableProjectionCore",
    "LatestDurableReconnect",
    "LatestDurableReconnectKind",
    "LatestDurableRevision",
    "LatestDurableSnapshot",
    "LatestDurableUpsert",
    "LatestDurableUpsertKind",
    "ThreadSafeLatestDurableProjection",
]


@dataclass(frozen=True, slots=True)
class LatestDurableRevision[V]:
    """A desired projection revision."""

    epoch: int
    value: V


@dataclass(frozen=True, slots=True)
class LatestDurableEnvelope[K, V]:
    """The exact attempt handed to a durable sink."""

    generation: int
    key: K
    epoch: int
    value: V


@dataclass(frozen=True, slots=True)
class LatestDurableKeyState[K, V]:
    """Observable state for one key."""

    key: K
    desired: LatestDurableRevision[V] | None
    inflight: LatestDurableEnvelope[K, V] | None
    durable_through: int | None


@dataclass(frozen=True, slots=True)
class LatestDurableSnapshot[K, V]:
    """Complete observable state in stable key-insertion order."""

    generation: int
    entries: tuple[LatestDurableKeyState[K, V], ...]


@dataclass(frozen=True, slots=True)
class LatestDurableChange:
    """Whether a transition changed the observable snapshot."""

    state: bool = False


class LatestDurableUpsertKind(StrEnum):
    ACCEPTED = "accepted"
    UNCHANGED = "unchanged"
    ALREADY_DURABLE = "already_durable"
    STALE_EPOCH = "stale_epoch"
    EPOCH_CONFLICT = "epoch_conflict"


@dataclass(frozen=True, slots=True)
class LatestDurableUpsert:
    kind: LatestDurableUpsertKind
    durable_through: int | None = None
    current: int | None = None


class LatestDurableClaimKind(StrEnum):
    CLAIMED = "claimed"
    EMPTY = "empty"
    BUSY = "busy"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class LatestDurableClaim[K, V]:
    kind: LatestDurableClaimKind
    envelope: LatestDurableEnvelope[K, V] | None = None
    current: int | None = None


class LatestDurableAckKind(StrEnum):
    ADVANCED = "advanced"
    UNCHANGED = "unchanged"
    UNKNOWN_EPOCH = "unknown_epoch"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class LatestDurableAck:
    kind: LatestDurableAckKind
    durable_through: int | None = None
    current: int | None = None


class LatestDurableFailureKind(StrEnum):
    PENDING = "pending"
    SUPERSEDED = "superseded"
    UNKNOWN_EPOCH = "unknown_epoch"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class LatestDurableFailure:
    kind: LatestDurableFailureKind
    current: int | None = None


class LatestDurableReconnectKind(StrEnum):
    ADVANCED = "advanced"
    UNCHANGED = "unchanged"
    STALE_GENERATION = "stale_generation"


@dataclass(frozen=True, slots=True)
class LatestDurableReconnect:
    kind: LatestDurableReconnectKind
    generation: int | None = None
    requeued: int | None = None
    superseded: int | None = None
    current: int | None = None


@dataclass(slots=True)
class _Entry[K, V]:
    desired: LatestDurableRevision[V] | None = None
    inflight: LatestDurableEnvelope[K, V] | None = None
    durable_through: int | None = None


class LatestDurableProjectionCore[K, V]:
    """Pure keyed latest-durable projection authority."""

    __slots__ = ("_entries", "_generation")

    def __init__(self, generation: int) -> None:
        self._require_epoch(generation, "generation")
        self._generation = generation
        self._entries: dict[K, _Entry[K, V]] = {}

    @staticmethod
    def _require_epoch(value: int, name: str) -> None:
        if value < 0:
            raise ValueError(f"{name} must be non-negative")

    @property
    def generation(self) -> int:
        return self._generation

    def snapshot(self) -> LatestDurableSnapshot[K, V]:
        return LatestDurableSnapshot(
            generation=self._generation,
            entries=tuple(
                LatestDurableKeyState(
                    key=key,
                    desired=entry.desired,
                    inflight=entry.inflight,
                    durable_through=entry.durable_through,
                )
                for key, entry in self._entries.items()
            ),
        )

    def state(self, key: K) -> LatestDurableKeyState[K, V] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        return LatestDurableKeyState(
            key=key,
            desired=entry.desired,
            inflight=entry.inflight,
            durable_through=entry.durable_through,
        )

    def durable_through(self, key: K) -> int | None:
        entry = self._entries.get(key)
        return None if entry is None else entry.durable_through

    def pending_keys(self) -> list[K]:
        return [
            key
            for key, entry in self._entries.items()
            if entry.inflight is None and entry.desired is not None
        ]

    def upsert_desired(
        self, key: K, epoch: int, value: V
    ) -> tuple[LatestDurableChange, LatestDurableUpsert]:
        """Replace the pending desire when ``epoch`` advances."""
        self._require_epoch(epoch, "epoch")
        entry = self._entries.setdefault(key, _Entry())
        if entry.durable_through is not None and epoch <= entry.durable_through:
            return LatestDurableChange(), LatestDurableUpsert(
                LatestDurableUpsertKind.ALREADY_DURABLE,
                durable_through=entry.durable_through,
            )

        retained = [
            candidate
            for candidate in (entry.desired, entry.inflight)
            if candidate is not None
        ]
        if retained:
            newest = max(retained, key=lambda candidate: candidate.epoch)
            if epoch < newest.epoch:
                return LatestDurableChange(), LatestDurableUpsert(
                    LatestDurableUpsertKind.STALE_EPOCH,
                    current=newest.epoch,
                )
            if epoch == newest.epoch:
                kind = (
                    LatestDurableUpsertKind.UNCHANGED
                    if value == newest.value
                    else LatestDurableUpsertKind.EPOCH_CONFLICT
                )
                return LatestDurableChange(), LatestDurableUpsert(kind)

        entry.desired = LatestDurableRevision(epoch, value)
        return LatestDurableChange(True), LatestDurableUpsert(
            LatestDurableUpsertKind.ACCEPTED
        )

    def claim(
        self, key: K, generation: int
    ) -> tuple[LatestDurableChange, LatestDurableClaim[K, V]]:
        """Move the latest pending revision into the sole in-flight slot."""
        self._require_epoch(generation, "generation")
        if generation != self._generation:
            return LatestDurableChange(), LatestDurableClaim(
                LatestDurableClaimKind.STALE_GENERATION,
                current=self._generation,
            )
        entry = self._entries.get(key)
        if entry is None:
            return LatestDurableChange(), LatestDurableClaim(
                LatestDurableClaimKind.EMPTY
            )
        if entry.inflight is not None:
            return LatestDurableChange(), LatestDurableClaim(
                LatestDurableClaimKind.BUSY
            )
        desired = entry.desired
        if desired is None:
            return LatestDurableChange(), LatestDurableClaim(
                LatestDurableClaimKind.EMPTY
            )
        entry.desired = None
        envelope = LatestDurableEnvelope(
            generation=generation,
            key=key,
            epoch=desired.epoch,
            value=desired.value,
        )
        entry.inflight = envelope
        return LatestDurableChange(True), LatestDurableClaim(
            LatestDurableClaimKind.CLAIMED,
            envelope=envelope,
        )

    def ack_applied(
        self, key: K, generation: int, epoch: int
    ) -> tuple[LatestDurableChange, LatestDurableAck]:
        """Record exact sink success for one in-flight revision."""
        self._require_epoch(generation, "generation")
        self._require_epoch(epoch, "epoch")
        if generation != self._generation:
            return LatestDurableChange(), LatestDurableAck(
                LatestDurableAckKind.STALE_GENERATION,
                current=self._generation,
            )
        entry = self._entries.get(key)
        if entry is None:
            return LatestDurableChange(), LatestDurableAck(
                LatestDurableAckKind.UNKNOWN_EPOCH
            )
        if entry.inflight is None or entry.inflight.epoch != epoch:
            if entry.durable_through is not None and epoch <= entry.durable_through:
                return LatestDurableChange(), LatestDurableAck(
                    LatestDurableAckKind.UNCHANGED,
                    durable_through=entry.durable_through,
                )
            return LatestDurableChange(), LatestDurableAck(
                LatestDurableAckKind.UNKNOWN_EPOCH
            )

        entry.inflight = None
        previous = entry.durable_through
        durable = epoch if previous is None else max(previous, epoch)
        entry.durable_through = durable
        kind = (
            LatestDurableAckKind.UNCHANGED
            if previous is not None and previous >= epoch
            else LatestDurableAckKind.ADVANCED
        )
        return LatestDurableChange(True), LatestDurableAck(
            kind,
            durable_through=durable,
        )

    def fail_retryable(
        self, key: K, generation: int, epoch: int
    ) -> tuple[LatestDurableChange, LatestDurableFailure]:
        """Return a failed attempt to pending unless a newer desire superseded it."""
        self._require_epoch(generation, "generation")
        self._require_epoch(epoch, "epoch")
        if generation != self._generation:
            return LatestDurableChange(), LatestDurableFailure(
                LatestDurableFailureKind.STALE_GENERATION,
                current=self._generation,
            )
        entry = self._entries.get(key)
        if entry is None or entry.inflight is None or entry.inflight.epoch != epoch:
            return LatestDurableChange(), LatestDurableFailure(
                LatestDurableFailureKind.UNKNOWN_EPOCH
            )

        inflight = entry.inflight
        entry.inflight = None
        if entry.desired is not None and entry.desired.epoch > inflight.epoch:
            outcome = LatestDurableFailure(LatestDurableFailureKind.SUPERSEDED)
        else:
            entry.desired = LatestDurableRevision(inflight.epoch, inflight.value)
            outcome = LatestDurableFailure(LatestDurableFailureKind.PENDING)
        return LatestDurableChange(True), outcome

    def reconnect(
        self, new_generation: int
    ) -> tuple[LatestDurableChange, LatestDurableReconnect]:
        """Fence the previous sink generation and recover its in-flight work."""
        self._require_epoch(new_generation, "generation")
        if new_generation < self._generation:
            return LatestDurableChange(), LatestDurableReconnect(
                LatestDurableReconnectKind.STALE_GENERATION,
                current=self._generation,
            )
        if new_generation == self._generation:
            return LatestDurableChange(), LatestDurableReconnect(
                LatestDurableReconnectKind.UNCHANGED,
                generation=self._generation,
            )

        self._generation = new_generation
        requeued = 0
        superseded = 0
        for entry in self._entries.values():
            inflight = entry.inflight
            if inflight is None:
                continue
            entry.inflight = None
            if entry.desired is not None and entry.desired.epoch > inflight.epoch:
                superseded += 1
            else:
                entry.desired = LatestDurableRevision(inflight.epoch, inflight.value)
                requeued += 1
        return LatestDurableChange(True), LatestDurableReconnect(
            LatestDurableReconnectKind.ADVANCED,
            generation=new_generation,
            requeued=requeued,
            superseded=superseded,
        )


class LatestDurableProjection[K, V]:
    """Single-threaded reactive latest-durable projection shell."""

    __slots__ = ("_core", "_state", "ctx")

    def __init__(self, ctx: dict, generation: int) -> None:
        self.ctx = ctx
        self._core: LatestDurableProjectionCore[K, V] = LatestDurableProjectionCore(
            generation
        )
        self._state: Slot[dict, dict, LatestDurableSnapshot[K, V]] = Slot(
            callable=lambda _view: self._run(self._core.snapshot)
        )

    def _run[R](self, operation: Callable[[], R]) -> R:
        return operation()

    def _apply(self, change: LatestDurableChange) -> None:
        if change.state:
            self._state.reset(self.ctx)

    @property
    def generation(self) -> int:
        return self._run(lambda: self._core.generation)

    def snapshot(self, ctx: Any = None) -> LatestDurableSnapshot[K, V]:
        """Read the reactive aggregate snapshot."""
        return self._state(self.ctx if ctx is None else ctx)

    def state(self, key: K) -> LatestDurableKeyState[K, V] | None:
        return self._run(lambda: self._core.state(key))

    def durable_through(self, key: K) -> int | None:
        return self._run(lambda: self._core.durable_through(key))

    def pending_keys(self) -> list[K]:
        return self._run(self._core.pending_keys)

    def state_handle(self) -> Slot[dict, dict, LatestDurableSnapshot[K, V]]:
        return self._state

    def upsert_desired(self, key: K, epoch: int, value: V) -> LatestDurableUpsert:
        change, outcome = self._run(
            lambda: self._core.upsert_desired(key, epoch, value)
        )
        self._apply(change)
        return outcome

    def claim(self, key: K, generation: int) -> LatestDurableClaim[K, V]:
        change, outcome = self._run(lambda: self._core.claim(key, generation))
        self._apply(change)
        return outcome

    def ack_applied(self, key: K, generation: int, epoch: int) -> LatestDurableAck:
        change, outcome = self._run(
            lambda: self._core.ack_applied(key, generation, epoch)
        )
        self._apply(change)
        return outcome

    def fail_retryable(
        self, key: K, generation: int, epoch: int
    ) -> LatestDurableFailure:
        change, outcome = self._run(
            lambda: self._core.fail_retryable(key, generation, epoch)
        )
        self._apply(change)
        return outcome

    def reconnect(self, new_generation: int) -> LatestDurableReconnect:
        change, outcome = self._run(lambda: self._core.reconnect(new_generation))
        self._apply(change)
        return outcome


class ThreadSafeLatestDurableProjection[K, V](LatestDurableProjection[K, V]):
    """Latest-durable projection serialized by a ``ThreadSafeContext``."""

    __slots__ = ("_thread_safe",)

    def __init__(
        self,
        ctx: dict,
        generation: int,
        *,
        ts: ThreadSafeContext | None = None,
    ) -> None:
        self._thread_safe = ts if ts is not None else ThreadSafeContext()
        super().__init__(ctx, generation)

    @property
    def thread_safe_context(self) -> ThreadSafeContext:
        return self._thread_safe

    def _run[R](self, operation: Callable[[], R]) -> R:
        with self._thread_safe.lock:
            return operation()


class AsyncLatestDurableProjection[K, V](LatestDurableProjection[K, V]):
    """Async-graph shell; projection transitions themselves remain synchronous."""

    __slots__ = ()
