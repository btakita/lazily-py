"""Portable, caller-driven stdlib primitives.

The clock and wait seams are supplied by the caller.  Importing this module
therefore starts no scheduler, creates no event loop, and adds no runtime
dependency.  The async adapter is an ordinary ``async def`` over awaitables.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


MAX_U64 = (1 << 64) - 1
OperationState = Literal["pending", "completed", "unavailable"]
CancellationState = Literal["pending", "cancelled", "unavailable"]


def _as_u64(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value < 0 or value > MAX_U64:
        raise ValueError(f"{name} must be in the uint64 range")
    return value


async def _invoke_async[T](factory: Callable[[], Awaitable[T]]) -> T:
    return await factory()


class TimerError(ValueError):
    """Typed timer construction/observation failure."""

    def __init__(self, reason: Literal["deadline_overflow", "clock_regression"]):
        super().__init__(reason)
        self.reason = reason


def checked_deadline(now: int, duration: int) -> int:
    """Return ``now + duration`` in the canonical unsigned 64-bit domain."""
    start = _as_u64(now, "now")
    span = _as_u64(duration, "duration")
    if span > MAX_U64 - start:
        raise TimerError("deadline_overflow")
    return start + span


@dataclass(frozen=True, slots=True)
class TimerObservation:
    outcome: Literal["pending", "fired", "unavailable"]
    deadline: int | None = None
    fired_at: int | None = None
    reason: str | None = None


class Timer:
    """Deterministic single-shot timer driven by :meth:`observe` calls."""

    def __init__(self, now: int, duration: int):
        self._deadline = checked_deadline(now, duration)
        self._last_now = now
        self._fired_at: int | None = None
        self._lock = RLock()

    @property
    def deadline(self) -> int:
        return self._deadline

    def observe(self, now: int) -> TimerObservation:
        with self._lock:
            now = _as_u64(now, "now")
            if self._fired_at is not None:
                return TimerObservation("fired", fired_at=self._fired_at)
            if now < self._last_now:
                return TimerObservation(
                    "unavailable",
                    deadline=self._deadline,
                    reason="clock_regression",
                )
            self._last_now = now
            if now >= self._deadline:
                self._fired_at = now
                return TimerObservation("fired", fired_at=now)
            return TimerObservation("pending", deadline=self._deadline)


@dataclass(frozen=True, slots=True)
class TimeoutOperation[T]:
    state: OperationState
    value: T | None = None

    @classmethod
    def pending(cls) -> TimeoutOperation[T]:
        return cls("pending")

    @classmethod
    def completed(cls, value: T) -> TimeoutOperation[T]:
        return cls("completed", value)

    @classmethod
    def unavailable(cls) -> TimeoutOperation[T]:
        return cls("unavailable")


@dataclass(frozen=True, slots=True)
class TimeoutObservation[T]:
    outcome: Literal["pending", "completed", "timed_out", "cancelled", "unavailable"]
    deadline: int | None = None
    value: T | None = None
    reason: str | None = None


class Timeout[T]:
    """Deadline-bounded operation with deterministic precedence and latching."""

    def __init__(self, now: int, duration: int):
        self._deadline = checked_deadline(now, duration)
        self._last_now = now
        self._terminal: TimeoutObservation[T] | None = None
        self._lock = RLock()

    @property
    def deadline(self) -> int:
        return self._deadline

    def poll(
        self,
        now: int,
        operation: Callable[[], TimeoutOperation[T]],
        cancellation: Callable[[], CancellationState],
    ) -> TimeoutObservation[T]:
        """Poll callable adapters exactly once each before the deadline."""
        if not callable(operation) or not callable(cancellation):
            raise TypeError("operation and cancellation must be callable")
        with self._lock:
            ready = self._pre_poll(now)
            if ready is not None:
                return ready
            op = operation()
            cancel = cancellation()
            if not isinstance(op, TimeoutOperation):
                raise TypeError("operation must return TimeoutOperation")
            return self._resolve(op, cancel)

    async def poll_async(
        self,
        now: int,
        operation: Callable[[], Awaitable[TimeoutOperation[T]]],
        cancellation: Callable[[], Awaitable[CancellationState]],
    ) -> TimeoutObservation[T]:
        """Await caller-owned adapters without importing an async runtime."""
        if not callable(operation) or not callable(cancellation):
            raise TypeError("operation and cancellation must be callable")
        with self._lock:
            ready = self._pre_poll(now)
            if ready is not None:
                return ready
        op, cancel = await asyncio.gather(
            _invoke_async(operation),
            _invoke_async(cancellation),
        )
        with self._lock:
            if not isinstance(op, TimeoutOperation):
                raise TypeError("operation must return TimeoutOperation")
            return self._resolve(op, cancel)

    def _pre_poll(self, now: int) -> TimeoutObservation[T] | None:
        now = _as_u64(now, "now")
        if self._terminal is not None:
            return self._terminal
        if now < self._last_now:
            return self._latch(
                TimeoutObservation("unavailable", reason="clock_regression")
            )
        self._last_now = now
        if now >= self._deadline:
            return self._latch(TimeoutObservation("timed_out"))
        return None

    def _resolve(
        self, operation: TimeoutOperation[T], cancellation: CancellationState
    ) -> TimeoutObservation[T]:
        if self._terminal is not None:
            return self._terminal
        if operation.state == "completed":
            return self._latch(TimeoutObservation("completed", value=operation.value))
        if operation.state == "unavailable":
            return self._latch(
                TimeoutObservation("unavailable", reason="operation_unavailable")
            )
        if cancellation == "cancelled":
            return self._latch(TimeoutObservation("cancelled"))
        if cancellation == "unavailable":
            return self._latch(
                TimeoutObservation("unavailable", reason="cancellation_unavailable")
            )
        if cancellation != "pending":
            raise ValueError(f"unknown cancellation state: {cancellation}")
        return TimeoutObservation("pending", deadline=self._deadline)

    def _latch(self, value: TimeoutObservation[T]) -> TimeoutObservation[T]:
        self._terminal = value
        return value


@dataclass(frozen=True, slots=True)
class RevisionBarrierObservation:
    outcome: Literal[
        "pending", "satisfied", "timed_out", "cancelled", "unavailable", "disposed"
    ]
    revision: int
    generation: int
    reason: str | None = None


class RevisionBarrier:
    """Revision-authoritative barrier with a separate wake generation."""

    def __init__(
        self,
        revision: int,
        required_revision: int,
        deadline: int | None = None,
    ):
        self._revision = _as_u64(revision, "revision")
        self._required_revision = _as_u64(required_revision, "required_revision")
        self._deadline = None if deadline is None else _as_u64(deadline, "deadline")
        self._generation = 0
        self._last_now: int | None = None
        self._terminal: tuple[str, str | None] | None = None
        self._lock = RLock()

    def observe(
        self,
        now: int,
        predicate: bool,
        cancellation: Callable[[], CancellationState],
    ) -> RevisionBarrierObservation:
        if not callable(cancellation):
            raise TypeError("cancellation must be callable")
        with self._lock:
            ready = self._begin_observe(now)
            if ready is not None:
                return ready
            if predicate and self._revision >= self._required_revision:
                return self._latch("satisfied")
            state = cancellation()
            return self._finish_cancellation(state)

    async def observe_async(
        self,
        now: int,
        predicate: bool,
        cancellation: Callable[[], Awaitable[CancellationState]],
    ) -> RevisionBarrierObservation:
        """Await a caller-owned cancellation seam; no event loop is created."""
        if not callable(cancellation):
            raise TypeError("cancellation must be callable")
        with self._lock:
            ready = self._begin_observe(now)
            if ready is not None:
                return ready
            if predicate and self._revision >= self._required_revision:
                return self._latch("satisfied")
        state = await cancellation()
        with self._lock:
            return self._finish_cancellation(state)

    def register_recheck(
        self, now: int, observed_revision: int, predicate: bool
    ) -> RevisionBarrierObservation:
        with self._lock:
            observed_revision = _as_u64(observed_revision, "observed_revision")
            ready = self._begin_observe(now)
            if ready is not None:
                return ready
            self._accept_revision(observed_revision)
            if predicate and self._revision >= self._required_revision:
                return self._latch("satisfied")
            return self._snapshot()

    def advance(self, revision: int, predicate: bool) -> RevisionBarrierObservation:
        with self._lock:
            revision = _as_u64(revision, "revision")
            if self._terminal is not None:
                return self._snapshot()
            self._accept_revision(revision)
            if predicate and self._revision >= self._required_revision:
                return self._latch("satisfied")
            return self._snapshot()

    def dispose(self) -> RevisionBarrierObservation:
        with self._lock:
            if self._terminal is None:
                return self._latch("disposed")
            return self._snapshot()

    def receipt(self, _key: str) -> RevisionBarrierObservation:
        """Wake receipts are deliberately not revision authority."""
        with self._lock:
            return self._snapshot()

    def _accept_revision(self, revision: int) -> None:
        if revision > self._revision:
            self._revision = revision
            self._generation += 1

    def _begin_observe(self, now: int) -> RevisionBarrierObservation | None:
        now = _as_u64(now, "now")
        if self._terminal is not None:
            return self._snapshot()
        if self._last_now is not None and now < self._last_now:
            return self._latch("unavailable", "clock_regression")
        self._last_now = now
        if self._deadline is not None and now >= self._deadline:
            return self._latch("timed_out")
        return None

    def _finish_cancellation(
        self,
        state: CancellationState,
    ) -> RevisionBarrierObservation:
        if self._terminal is not None:
            return self._snapshot()
        if state == "cancelled":
            return self._latch("cancelled")
        if state == "unavailable":
            return self._latch("unavailable", "cancellation_unavailable")
        if state != "pending":
            raise ValueError(f"unknown cancellation state: {state}")
        return self._snapshot()

    def _latch(
        self, outcome: str, reason: str | None = None
    ) -> RevisionBarrierObservation:
        self._terminal = (outcome, reason)
        return self._snapshot()

    def _snapshot(self) -> RevisionBarrierObservation:
        outcome, reason = self._terminal or ("pending", None)
        return RevisionBarrierObservation(
            outcome,  # type: ignore[arg-type]
            self._revision,
            self._generation,
            reason,
        )
