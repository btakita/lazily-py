"""Queue-family shells for the thread-safe and async execution flavors.

The queue, topic, and work-queue algebras are already independent of the
reactive graph in Python: :class:`QueueStorage` owns FIFO state, while the
single-threaded cells own the memoized reader-kind handles.  Python contexts
use a type-erased ``dict`` graph, so a flavor shell can reuse that algebra while
still minting a fresh set of reader handles for every instance.

Nothing in this family is async-coloured.  Reads, cursor advances, and lease
decisions return plain values and never await.  The async classes therefore
have the same synchronous Core surface as their single-threaded counterparts,
matching the settled Rust reference and the existing ``AsyncReactiveMap``
precedent.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, TypeVar

from .queue import (
    QueueCell,
    QueuePopError,
    QueuePushError,
    QueueReaderHandles,
    QueueStorage,
    TopicCell,
    TopicDurability,
    TopicSnapshot,
    TopicSubscribeOutcome,
    TopicSubscriptionSnapshot,
    VecDequeStorage,
)
from .thread_safe import ThreadSafeContext
from .work_queue import (
    WorkQueueCell,
    WorkQueueDeadLetter,
    WorkQueueDelivery,
    WorkQueueItem,
    WorkQueueReaderHandles,
)


if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = [
    "AsyncQueueCell",
    "AsyncTopicCell",
    "AsyncWorkQueueCell",
    "ThreadSafeQueueCell",
    "ThreadSafeTopicCell",
    "ThreadSafeWorkQueueCell",
]

T = TypeVar("T")
R = TypeVar("R")


class _Serialized:
    """Run a shell operation under one re-entrant execution-flavor boundary."""

    __slots__ = ("_mutex",)

    def __init__(self, mutex: Any | None = None) -> None:
        self._mutex = mutex if mutex is not None else threading.RLock()

    def _run(self, operation: Callable[[], R]) -> R:
        with self._mutex:
            return operation()


class _QueueFlavor[T](_Serialized):
    __slots__ = ("_inner", "ctx")

    def __init__(
        self,
        ctx: dict,
        *,
        capacity: int | None = None,
        storage: QueueStorage[T] | None = None,
        mutex: Any | None = None,
    ) -> None:
        super().__init__(mutex)
        self.ctx = ctx
        self._inner = QueueCell(ctx, capacity=capacity, storage=storage)

    @classmethod
    def with_capacity(cls, ctx: dict, capacity: int) -> _QueueFlavor[T]:
        return cls(ctx, storage=VecDequeStorage.with_capacity(capacity))

    @classmethod
    def with_storage(cls, ctx: dict, storage: QueueStorage[T]) -> _QueueFlavor[T]:
        return cls(ctx, storage=storage)

    def try_push(self, value: T) -> QueuePushError | None:
        return self._run(lambda: self._inner.try_push(value))

    def try_pop(self) -> T | QueuePopError:
        return self._run(self._inner.try_pop)

    def close(self) -> None:
        self._run(self._inner.close)

    def head(self, ctx: Any = None) -> T | None:
        return self._run(lambda: self._inner.head(ctx))

    def len(self, ctx: Any = None) -> int:
        return self._run(lambda: self._inner.len(ctx))

    def is_empty(self, ctx: Any = None) -> bool:
        return self._run(lambda: self._inner.is_empty(ctx))

    def is_full(self, ctx: Any = None) -> bool:
        return self._run(lambda: self._inner.is_full(ctx))

    def is_closed(self, ctx: Any = None) -> bool:
        return self._run(lambda: self._inner.is_closed(ctx))

    def reader_handles(self) -> QueueReaderHandles[T]:
        return self._run(self._inner.reader_handles)

    def capacity(self) -> int | None:
        return self._run(self._inner.capacity)

    def elements(self) -> list[T]:
        return self._run(self._inner.elements)


class ThreadSafeQueueCell[T](_QueueFlavor[T]):
    """A queue whose operations are serialized by a ``ThreadSafeContext``."""

    __slots__ = ("_thread_safe",)

    def __init__(
        self,
        ctx: dict,
        *,
        capacity: int | None = None,
        storage: QueueStorage[T] | None = None,
        ts: ThreadSafeContext | None = None,
    ) -> None:
        self._thread_safe = ts if ts is not None else ThreadSafeContext()
        super().__init__(
            ctx,
            capacity=capacity,
            storage=storage,
            mutex=self._thread_safe.lock,
        )

    @property
    def thread_safe_context(self) -> ThreadSafeContext:
        return self._thread_safe


class AsyncQueueCell[T](_QueueFlavor[T]):
    """The async-graph queue flavor; its storage operations remain synchronous."""


class _TopicFlavor[T](_Serialized):
    __slots__ = ("_inner", "ctx")

    def __init__(
        self,
        ctx: dict,
        snapshot: TopicSnapshot[T] | None = None,
        *,
        mutex: Any | None = None,
    ) -> None:
        super().__init__(mutex)
        self.ctx = ctx
        self._inner = TopicCell(ctx, snapshot)

    @property
    def base_offset(self) -> int:
        return self._run(lambda: self._inner.base_offset)

    @property
    def tail_offset(self) -> int:
        return self._run(lambda: self._inner.tail_offset)

    def subscribe(
        self,
        subscriber_id: str,
        durability: TopicDurability = TopicDurability.Durable,
    ) -> TopicSubscribeOutcome:
        return self._run(lambda: self._inner.subscribe(subscriber_id, durability))

    def reconnect(self, subscriber_id: str) -> None:
        self._run(lambda: self._inner.reconnect(subscriber_id))

    def disconnect(self, subscriber_id: str) -> None:
        self._run(lambda: self._inner.disconnect(subscriber_id))

    def publish(self, value: T) -> int:
        return self._run(lambda: self._inner.publish(value))

    def read_untracked(self, subscriber_id: str) -> list[T]:
        return self._run(lambda: self._inner.read_untracked(subscriber_id))

    def read_stream(self, subscriber_id: str) -> list[T]:
        return self._run(lambda: self._inner.read_stream(subscriber_id))

    def read(self, subscriber_id: str) -> T | None:
        return self._run(lambda: self._inner.read(subscriber_id))

    def advance(self, subscriber_id: str, count: int = 1) -> T | None:
        return self._run(lambda: self._inner.advance(subscriber_id, count))

    def gc(self) -> int:
        return self._run(self._inner.gc)

    def restart(self, subscriber_id: str | None = None) -> None:
        del subscriber_id
        self._run(self._inner.restart)

    def elements(self) -> list[T]:
        return self._run(self._inner.elements)

    def subscription(self, subscriber_id: str) -> TopicSubscriptionSnapshot | None:
        return self._run(lambda: self._inner.subscription(subscriber_id))

    def reader_handle(self, subscriber_id: str) -> Any:
        return self._run(lambda: self._inner.reader_handle(subscriber_id))

    def snapshot(self) -> TopicSnapshot[T]:
        return self._run(self._inner.snapshot)


class ThreadSafeTopicCell[T](_TopicFlavor[T]):
    """A broadcast topic serialized by a ``ThreadSafeContext``."""

    __slots__ = ("_thread_safe",)

    def __init__(
        self,
        ctx: dict,
        snapshot: TopicSnapshot[T] | None = None,
        *,
        ts: ThreadSafeContext | None = None,
    ) -> None:
        self._thread_safe = ts if ts is not None else ThreadSafeContext()
        super().__init__(ctx, snapshot, mutex=self._thread_safe.lock)

    @property
    def thread_safe_context(self) -> ThreadSafeContext:
        return self._thread_safe


class AsyncTopicCell[T](_TopicFlavor[T]):
    """The async-graph broadcast topic; cursor operations stay synchronous."""


class _WorkQueueFlavor[T](_Serialized):
    __slots__ = ("_inner", "ctx", "max_deliveries", "visibility_timeout")

    def __init__(
        self,
        ctx: dict,
        *,
        visibility_timeout: int,
        max_deliveries: int,
        mutex: Any | None = None,
    ) -> None:
        super().__init__(mutex)
        self.ctx = ctx
        self.visibility_timeout = visibility_timeout
        self.max_deliveries = max_deliveries
        self._inner = WorkQueueCell(
            ctx,
            visibility_timeout=visibility_timeout,
            max_deliveries=max_deliveries,
        )

    def push(self, value: T) -> int:
        return self._run(lambda: self._inner.push(value))

    def claim(self, worker: str, now: int) -> WorkQueueDelivery[T] | None:
        return self._run(lambda: self._inner.claim(worker, now))

    def ack(self, worker: str, delivery_id: int) -> bool:
        return self._run(lambda: self._inner.ack(worker, delivery_id))

    def nack(self, worker: str, delivery_id: int) -> bool:
        return self._run(lambda: self._inner.nack(worker, delivery_id))

    def reap_expired(self, now: int) -> int:
        return self._run(lambda: self._inner.reap_expired(now))

    def pending_len(self) -> int:
        return self._run(self._inner.pending_len)

    def is_empty(self) -> bool:
        return self._run(self._inner.is_empty)

    def in_flight_len(self) -> int:
        return self._run(self._inner.in_flight_len)

    def dead_letter_len(self) -> int:
        return self._run(self._inner.dead_letter_len)

    def reader_handles(self) -> WorkQueueReaderHandles:
        return self._run(self._inner.reader_handles)

    def pending_items(self) -> list[WorkQueueItem[T]]:
        return self._run(self._inner.pending_items)

    def in_flight_deliveries(self) -> list[WorkQueueDelivery[T]]:
        return self._run(self._inner.in_flight_deliveries)

    def dead_letter_items(self) -> list[WorkQueueDeadLetter[T]]:
        return self._run(self._inner.dead_letter_items)


class ThreadSafeWorkQueueCell[T](_WorkQueueFlavor[T]):
    """A competing-consumer queue serialized by a ``ThreadSafeContext``."""

    __slots__ = ("_thread_safe",)

    def __init__(
        self,
        ctx: dict,
        *,
        visibility_timeout: int,
        max_deliveries: int,
        ts: ThreadSafeContext | None = None,
    ) -> None:
        self._thread_safe = ts if ts is not None else ThreadSafeContext()
        super().__init__(
            ctx,
            visibility_timeout=visibility_timeout,
            max_deliveries=max_deliveries,
            mutex=self._thread_safe.lock,
        )

    @property
    def thread_safe_context(self) -> ThreadSafeContext:
        return self._thread_safe


class AsyncWorkQueueCell[T](_WorkQueueFlavor[T]):
    """The async-graph work queue; lease decisions stay synchronous."""
