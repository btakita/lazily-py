"""Canonical queue-family fixtures replayed against all three Python flavors.

Every one of the eleven ``QueueCell`` / ``TopicCell`` / ``WorkQueueCell``
fixtures runs against the single-threaded, thread-safe, and async shells.
Invalidation is read from ``steps[].expected.invalidates`` and measured by a
counter inside the reader's own computed body.  This deliberately avoids the
two false-green shapes found during the ReactiveMap rollout: checking runner
bookkeeping, or accepting a zero-step/zero-matrix replay.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from conformance_assert import (
    assert_key,
    assert_key_set,
    instrument,
    sub_entries,
)

import lazily


if TYPE_CHECKING:
    from collections.abc import Callable


_SPEC = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "collections"
)
_LOCAL = Path(__file__).resolve().parent / "conformance" / "collections"
_SRC = Path(__file__).resolve().parents[1] / "src" / "lazily"

QUEUE_FIXTURES = (
    "queuecell_spsc_push_pop.json",
    "queuecell_popped_head_observation.json",
    "queuecell_mpsc_multi_writer.json",
    "queuecell_bounded_backpressure.json",
    "queuecell_closure_lifecycle.json",
)
TOPIC_FIXTURES = (
    "topiccell_broadcast_cursor_isolation.json",
    "topiccell_durable_replay_gc.json",
    "topiccell_ephemeral_lifecycle.json",
    "topiccell_offline_tail_bounds.json",
)
WORK_QUEUE_FIXTURES = (
    "workqueue_competing_delivery.json",
    "workqueue_lease_deadletter.json",
)
ALL_FIXTURES = QUEUE_FIXTURES + TOPIC_FIXTURES + WORK_QUEUE_FIXTURES


class _Flavor:
    name: str
    queue_cls: type[Any]
    topic_cls: type[Any]
    work_queue_cls: type[Any]


class SyncFlavor(_Flavor):
    name = "sync"
    queue_cls = lazily.QueueCell
    topic_cls = lazily.TopicCell
    work_queue_cls = lazily.WorkQueueCell


class ThreadSafeFlavor(_Flavor):
    name = "thread-safe"
    queue_cls = lazily.ThreadSafeQueueCell
    topic_cls = lazily.ThreadSafeTopicCell
    work_queue_cls = lazily.ThreadSafeWorkQueueCell


class AsyncFlavor(_Flavor):
    name = "async"
    queue_cls = lazily.AsyncQueueCell
    topic_cls = lazily.AsyncTopicCell
    work_queue_cls = lazily.AsyncWorkQueueCell


FLAVORS = (SyncFlavor, ThreadSafeFlavor, AsyncFlavor)

# (primitive, flavor, source marker, shipped)
LEDGER = (
    ("queue", "sync", "class QueueCell", True),
    ("queue", "thread-safe", "class ThreadSafeQueueCell", True),
    ("queue", "async", "class AsyncQueueCell", True),
    ("topic", "sync", "class TopicCell", True),
    ("topic", "thread-safe", "class ThreadSafeTopicCell", True),
    ("topic", "async", "class AsyncTopicCell", True),
    ("work-queue", "sync", "class WorkQueueCell", True),
    ("work-queue", "thread-safe", "class ThreadSafeWorkQueueCell", True),
    ("work-queue", "async", "class AsyncWorkQueueCell", True),
)


def _load(name: str) -> dict[str, Any]:
    path = _SPEC / name
    if not path.exists():
        path = _LOCAL / name
    if not path.exists():
        pytest.skip(f"canonical collections fixture not found: {name}")
    return instrument(json.loads(path.read_text()), name=name)


class _Counter:
    """A reactive reader whose own compute body records recomputation."""

    def __init__(self, ctx: dict, body: Callable[[Any], Any]) -> None:
        self.count = 0

        def compute(view: Any) -> Any:
            self.count += 1
            return body(view)

        self._node = lazily.computed(ctx, compute)

    def drive(self) -> int:
        self._node.get()
        return self.count


def _assert_invalidation(
    where: str,
    readers: dict[str, _Counter],
    baseline: dict[str, int],
    expected: Any,
) -> None:
    # DESCEND into the matrix (#lzsubblockkeyset) rather than taking its value
    # and walking it here: the child tracker owns the matrix's own key set, so a
    # reader the corpus adds to it fails as unconsumed instead of being compared
    # by nothing. Every entry is booked read AND asserted on the way through.
    invalidates = dict(sub_entries(expected, "invalidates", where=where))
    # An explicit empty object is the canonical "no reader invalidates"
    # matrix.  Assert every already-minted reader in that case so `{}` is not
    # treated as "skip invalidation checks".
    wanted = invalidates or dict.fromkeys(readers, False)
    for name, should_invalidate in wanted.items():
        assert name in readers, f"{where}: fixture names unknown reader {name!r}"
        after = readers[name].drive()
        delta = after - baseline[name]
        assert delta == int(should_invalidate), (
            f"{where}: reader {name!r} recomputed {delta} times; "
            f"expected invalidates={should_invalidate}"
        )


def _queue_initial(ctx: dict, cls: type[Any], initial: dict[str, Any]) -> Any:
    capacity = initial.get("capacity")
    storage = (
        lazily.VecDequeStorage.with_capacity(capacity)
        if capacity is not None
        else lazily.VecDequeStorage()
    )
    queue = cls(ctx, storage=storage)
    for value in initial.get("elements", []):
        assert queue.try_push(value) is None
    if initial.get("closed"):
        queue.close()
    return queue


def _queue_readers(ctx: dict, queue: Any) -> dict[str, _Counter]:
    return {
        "head": _Counter(ctx, lambda view: queue.head(view)),
        "len": _Counter(ctx, lambda view: queue.len(view)),
        "is_empty": _Counter(ctx, lambda view: queue.is_empty(view)),
        "is_full": _Counter(ctx, lambda view: queue.is_full(view)),
        "closed": _Counter(ctx, lambda view: queue.is_closed(view)),
    }


def _queue_return(result: Any) -> Any:
    if isinstance(result, (lazily.QueuePopError, lazily.QueuePushError)):
        return result.label
    return result


def _assert_queue_state(where: str, queue: Any, expected: dict[str, Any]) -> None:
    observations = {
        "elements": queue.elements,
        "head": queue.head,
        "len": queue.len,
        "is_empty": queue.is_empty,
        "is_full": queue.is_full,
        "closed": queue.is_closed,
    }
    for name, observe in observations.items():
        if name in expected:
            assert_key(expected, name, observe(), where=where)


def _run_queue(flavor: type[_Flavor], fixture_name: str) -> tuple[int, int]:
    fixture = _load(fixture_name)
    ctx: dict = {}
    queue = _queue_initial(ctx, flavor.queue_cls, fixture["initial"])
    readers = _queue_readers(ctx, queue)
    for reader in readers.values():
        reader.drive()

    steps = fixture.get("steps") or []
    assert steps, f"{flavor.name} {fixture_name}: fixture has no steps"
    matrices = 0
    for index, step in enumerate(steps):
        where = f"{flavor.name} {fixture_name} step {index}"
        assert "invalidates" not in step, (
            f"{where}: invalidates is at step level; expected.invalidates is canonical"
        )
        expected = step.get("expected")
        assert expected is not None, f"{where}: expected block is missing"
        assert "invalidates" in expected, f"{where}: invalidation matrix is missing"
        # The matrix is descended into and compared inside `_assert_invalidation`.
        matrices += 1

        baseline = {name: reader.drive() for name, reader in readers.items()}
        op = step["op"]
        kind = op["type"]
        result: Any = None
        if kind in {"push", "try_push"}:
            result = _queue_return(queue.try_push(op["value"]))
        elif kind in {"pop", "try_pop"}:
            result = _queue_return(queue.try_pop())
        elif kind == "close":
            queue.close()
        elif kind == "batch":
            inner_ops = op.get("ops") or []
            assert inner_ops, f"{where}: batch contains no operations"

            def apply_batch(
                _ops: list[dict[str, Any]] = inner_ops,
                _where: str = where,
            ) -> None:
                for inner in _ops:
                    assert inner["type"] == "push", (
                        f"{_where}: unsupported batch op {inner['type']!r}"
                    )
                    assert queue.try_push(inner["value"]) is None

            lazily.batch(apply_batch)
        else:
            raise AssertionError(f"{where}: unknown queue op {kind!r}")

        if "returns" in step:
            assert result == step["returns"], (
                f"{where}: returns {result!r}, expected {step['returns']!r}"
            )
        _assert_invalidation(where, readers, baseline, expected)
        _assert_queue_state(where, queue, expected)

    return len(steps), matrices


def _topic_snapshot(initial: dict[str, Any]) -> lazily.TopicSnapshot[str]:
    subscriptions = tuple(
        lazily.TopicSubscriptionSnapshot(
            subscriber_id,
            int(saved["cursor"]),
            lazily.TopicDurability(saved["durability"]),
            bool(saved["connected"]),
        )
        for subscriber_id, saved in sorted(initial.get("subscriptions", {}).items())
    )
    return lazily.TopicSnapshot(
        int(initial.get("base_offset", 0)),
        tuple(initial.get("elements", [])),
        subscriptions,
    )


def _topic_subscriber_ids(fixture: dict[str, Any]) -> set[str]:
    ids = set(fixture["initial"].get("subscriptions", {}))
    for step in fixture.get("steps", []):
        op = step.get("op", {})
        subscriber = op.get("subscriber")
        if subscriber is not None:
            ids.add(subscriber)
        expected = step.get("expected", {})
        ids.update(expected.get("subscriptions", {}))
        ids.update(expected.get("invalidates", {}))
    return ids


def _topic_readers(
    ctx: dict, topic: Any, subscriber_ids: set[str]
) -> dict[str, _Counter]:
    readers: dict[str, _Counter] = {}
    for subscriber_id in sorted(subscriber_ids):
        handle = topic.reader_handle(subscriber_id)
        readers[subscriber_id] = _Counter(
            ctx, lambda view, reader=handle: tuple(reader(view))
        )
    return readers


def _assert_topic_state(where: str, topic: Any, expected: dict[str, Any]) -> None:
    assert_key(expected, "base_offset", topic.base_offset, where=where)
    assert_key(expected, "elements", topic.elements(), where=where)

    if "subscriptions" in expected:
        # The KEY SET first (#lzsubblockkeyset): the subscriber ids the fixture
        # names must be exactly the ones the topic really holds, in both
        # directions. Then descend per subscriber, because each entry is an
        # object of its own and reading `cursor`/`durability`/`connected` by
        # name would leave a fourth field compared by nothing.
        actual_ids = {saved.subscriber_id for saved in topic.snapshot().subscriptions}
        assert_key_set(expected, "subscriptions", actual_ids, where=where)
        subscriptions = expected.sub("subscriptions")
        for subscriber_id in sorted(subscriptions):
            want = subscriptions.sub(subscriber_id)
            actual = topic.subscription(subscriber_id)
            assert actual is not None, f"{where}: missing subscriber {subscriber_id!r}"
            assert_key(want, "cursor", actual.cursor, where=where)
            assert_key(want, "durability", actual.durability.value, where=where)
            assert_key(want, "connected", actual.connected, where=where)

    if "reads" in expected:
        for subscriber_id, want in sub_entries(expected, "reads", where=where):
            assert topic.read_stream(subscriber_id) == want, (
                f"{where}: subscriber {subscriber_id!r} read mismatch"
            )


def _run_topic(flavor: type[_Flavor], fixture_name: str) -> tuple[int, int]:
    fixture = _load(fixture_name)
    ctx: dict = {}
    topic = flavor.topic_cls(ctx, _topic_snapshot(fixture["initial"]))
    readers = _topic_readers(ctx, topic, _topic_subscriber_ids(fixture))
    for reader in readers.values():
        reader.drive()

    steps = fixture.get("steps") or []
    assert steps, f"{flavor.name} {fixture_name}: fixture has no steps"
    matrices = 0
    for index, step in enumerate(steps):
        where = f"{flavor.name} {fixture_name} step {index}"
        assert "invalidates" not in step, (
            f"{where}: invalidates is at step level; expected.invalidates is canonical"
        )
        expected = step.get("expected")
        assert expected is not None, f"{where}: expected block is missing"
        assert "invalidates" in expected, f"{where}: invalidation matrix is missing"
        # The matrix is descended into and compared inside `_assert_invalidation`.
        matrices += 1

        baseline = {name: reader.drive() for name, reader in readers.items()}
        op = step["op"]
        kind = op["type"]
        result: Any = None
        if kind == "publish":
            result = topic.publish(op["value"])
        elif kind == "advance":
            result = topic.advance(op["subscriber"])
        elif kind == "subscribe":
            topic.subscribe(
                op["subscriber"],
                lazily.TopicDurability(op.get("durability", "durable")),
            )
        elif kind == "reconnect":
            topic.reconnect(op["subscriber"])
        elif kind == "disconnect":
            topic.disconnect(op["subscriber"])
        elif kind == "restart":
            topic.restart()
        elif kind == "gc":
            result = topic.gc()
        else:
            raise AssertionError(f"{where}: unknown topic op {kind!r}")

        if "returns" in step:
            assert result == step["returns"], (
                f"{where}: returns {result!r}, expected {step['returns']!r}"
            )
        _assert_invalidation(where, readers, baseline, expected)
        _assert_topic_state(where, topic, expected)

    return len(steps), matrices


def _work_queue_readers(ctx: dict, queue: Any) -> dict[str, _Counter]:
    handles = queue.reader_handles()
    return {
        "pending_len": _Counter(ctx, lambda view: handles.pending_len(view)),
        "is_empty": _Counter(ctx, lambda view: handles.is_empty(view)),
        "in_flight_len": _Counter(ctx, lambda view: handles.in_flight_len(view)),
        "dead_letter_len": _Counter(ctx, lambda view: handles.dead_letter_len(view)),
    }


def _work_return(value: Any) -> Any:
    if isinstance(value, lazily.WorkQueueDelivery):
        return asdict(value)
    return value


def _work_items(queue: Any) -> list[dict[str, Any]]:
    return [asdict(item) for item in queue.pending_items()]


def _work_deliveries(queue: Any) -> list[dict[str, Any]]:
    return [asdict(delivery) for delivery in queue.in_flight_deliveries()]


def _work_dead_letters(queue: Any) -> list[dict[str, Any]]:
    result = []
    for dead in queue.dead_letter_items():
        encoded = asdict(dead)
        encoded["reason"] = dead.reason.value
        result.append(encoded)
    return result


def _assert_work_state(where: str, queue: Any, expected: dict[str, Any]) -> None:
    assert_key(expected, "pending", _work_items(queue), where=where)
    assert_key(expected, "in_flight", _work_deliveries(queue), where=where)
    assert_key(expected, "dead_letters", _work_dead_letters(queue), where=where)
    assert_key(
        expected,
        "reads",
        {
            "pending_len": queue.pending_len(),
            "is_empty": queue.is_empty(),
            "in_flight_len": queue.in_flight_len(),
            "dead_letter_len": queue.dead_letter_len(),
        },
        where=where,
    )


def _run_work_queue(flavor: type[_Flavor], fixture_name: str) -> tuple[int, int]:
    fixture = _load(fixture_name)
    initial = fixture["initial"]
    assert initial["pending"] == [], (
        f"{fixture_name}: unsupported non-empty initial.pending"
    )
    assert initial["in_flight"] == [], (
        f"{fixture_name}: unsupported non-empty initial.in_flight"
    )
    assert initial["dead_letters"] == [], (
        f"{fixture_name}: unsupported non-empty initial.dead_letters"
    )
    visibility_timeout = initial["visibility_timeout"]
    max_deliveries = initial["max_deliveries"]
    ctx: dict = {}
    queue = flavor.work_queue_cls(
        ctx,
        visibility_timeout=visibility_timeout,
        max_deliveries=max_deliveries,
    )
    readers = _work_queue_readers(ctx, queue)
    for reader in readers.values():
        reader.drive()

    steps = fixture.get("steps") or []
    assert steps, f"{flavor.name} {fixture_name}: fixture has no steps"
    matrices = 0
    for index, step in enumerate(steps):
        where = f"{flavor.name} {fixture_name} step {index}"
        assert "invalidates" not in step, (
            f"{where}: invalidates is at step level; expected.invalidates is canonical"
        )
        expected = step.get("expected")
        assert expected is not None, f"{where}: expected block is missing"
        assert "invalidates" in expected, f"{where}: invalidation matrix is missing"
        # The matrix is descended into and compared inside `_assert_invalidation`.
        matrices += 1

        baseline = {name: reader.drive() for name, reader in readers.items()}
        op = step["op"]
        kind = op["type"]
        if kind == "push":
            result: Any = queue.push(op["value"])
        elif kind == "claim":
            result = queue.claim(op["worker"], op["now"])
        elif kind == "ack":
            result = queue.ack(op["worker"], op["delivery_id"])
        elif kind == "nack":
            result = queue.nack(op["worker"], op["delivery_id"])
        elif kind == "reap_expired":
            result = queue.reap_expired(op["now"])
        else:
            raise AssertionError(f"{where}: unknown work-queue op {kind!r}")

        assert _work_return(result) == step.get("returns"), (
            f"{where}: returns {_work_return(result)!r}, "
            f"expected {step.get('returns')!r}"
        )
        _assert_invalidation(where, readers, baseline, expected)
        _assert_work_state(where, queue, expected)

    return len(steps), matrices


def test_ledger_matches_all_nine_shipped_flavors() -> None:
    sources = "\n".join(path.read_text() for path in _SRC.rglob("*.py"))
    assert sources, "read no sources from src/lazily; ledger check is vacuous"
    assert len(LEDGER) == 9, "ledger must cover primitive x flavor (3 x 3)"
    assert all(shipped for *_, shipped in LEDGER), (
        "Python now ships all queue-family flavors; staged skips are stale"
    )
    for primitive, flavor, marker, shipped in LEDGER:
        assert (marker in sources) is shipped, (
            f"{primitive}/{flavor}: source marker {marker!r} and ledger disagree"
        )


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
@pytest.mark.parametrize("fixture_name", QUEUE_FIXTURES)
def test_queue_fixture_all_flavors(flavor: type[_Flavor], fixture_name: str) -> None:
    steps, matrices = _run_queue(flavor, fixture_name)
    assert steps > 0 and matrices == steps


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
@pytest.mark.parametrize("fixture_name", TOPIC_FIXTURES)
def test_topic_fixture_all_flavors(flavor: type[_Flavor], fixture_name: str) -> None:
    steps, matrices = _run_topic(flavor, fixture_name)
    assert steps > 0 and matrices == steps


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
@pytest.mark.parametrize("fixture_name", WORK_QUEUE_FIXTURES)
def test_work_queue_fixture_all_flavors(
    flavor: type[_Flavor], fixture_name: str
) -> None:
    steps, matrices = _run_work_queue(flavor, fixture_name)
    assert steps > 0 and matrices == steps


def test_thread_safe_family_serializes_concurrent_mutators() -> None:
    ctx: dict = {}

    queue = lazily.ThreadSafeQueueCell[int](ctx)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(result is None for result in pool.map(queue.try_push, range(128)))
    assert queue.len() == 128
    assert sorted(queue.elements()) == list(range(128))

    topic = lazily.ThreadSafeTopicCell[int](ctx)
    topic.subscribe("reader")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(topic.publish, range(128)))
    assert sorted(topic.read_stream("reader")) == list(range(128))

    work = lazily.ThreadSafeWorkQueueCell[int](
        ctx, visibility_timeout=10, max_deliveries=3
    )
    for value in range(128):
        work.push(value)
    with ThreadPoolExecutor(max_workers=8) as pool:
        deliveries = list(
            pool.map(lambda worker: work.claim(str(worker), 0), range(128))
        )
    assert all(delivery is not None for delivery in deliveries)
    assert len({delivery.item_id for delivery in deliveries if delivery}) == 128
    assert work.pending_len() == 0
    assert work.in_flight_len() == 128
