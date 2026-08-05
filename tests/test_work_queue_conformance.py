from __future__ import annotations

import json
from pathlib import Path

from lazily import Slot, WorkQueueCell, WorkQueueDeadLetterReason


_SPEC = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "collections"
)


def _work_queue_config(name: str) -> tuple[int, int]:
    fixture = json.loads((_SPEC / name).read_text())
    initial = fixture["initial"]
    return initial["visibility_timeout"], initial["max_deliveries"]


def _materialize(queue: WorkQueueCell[str]) -> None:
    queue.pending_len()
    queue.is_empty()
    queue.in_flight_len()
    queue.dead_letter_len()


def _assert_invalidated(
    ctx: dict,
    queue: WorkQueueCell[str],
    *,
    pending_len: bool = False,
    is_empty: bool = False,
    in_flight_len: bool = False,
    dead_letter_len: bool = False,
) -> None:
    handles = queue.reader_handles()
    expected = {
        handles.pending_len: pending_len,
        handles.is_empty: is_empty,
        handles.in_flight_len: in_flight_len,
        handles.dead_letter_len: dead_letter_len,
    }
    for reader, should_invalidate in expected.items():
        assert isinstance(reader, Slot)
        assert reader.is_in(ctx) is not should_invalidate
    _materialize(queue)


def test_competing_delivery_fixture() -> None:
    ctx: dict = {}
    visibility_timeout, max_deliveries = _work_queue_config(
        "workqueue_competing_delivery.json"
    )
    queue = WorkQueueCell[str](
        ctx,
        visibility_timeout=visibility_timeout,
        max_deliveries=max_deliveries,
    )
    _materialize(queue)

    assert queue.push("a") == 0
    _assert_invalidated(ctx, queue, pending_len=True, is_empty=True)
    assert queue.push("b") == 1
    _assert_invalidated(ctx, queue, pending_len=True)

    first = queue.claim("alpha", 100)
    assert first is not None
    assert (first.delivery_id, first.item_id, first.attempt, first.deadline) == (
        0,
        0,
        1,
        110,
    )
    _assert_invalidated(ctx, queue, pending_len=True, in_flight_len=True)

    second = queue.claim("beta", 100)
    assert second is not None
    assert (second.delivery_id, second.item_id, second.attempt, second.deadline) == (
        1,
        1,
        1,
        110,
    )
    _assert_invalidated(ctx, queue, pending_len=True, is_empty=True, in_flight_len=True)
    assert queue.claim("gamma", 100) is None
    _assert_invalidated(ctx, queue)

    assert not queue.ack("alpha", second.delivery_id)
    _assert_invalidated(ctx, queue)
    assert queue.ack("beta", second.delivery_id)
    _assert_invalidated(ctx, queue, in_flight_len=True)
    assert queue.nack("alpha", first.delivery_id)
    _assert_invalidated(ctx, queue, pending_len=True, is_empty=True, in_flight_len=True)

    retry = queue.claim("gamma", 105)
    assert retry is not None
    assert (retry.delivery_id, retry.item_id, retry.attempt, retry.deadline) == (
        2,
        0,
        2,
        115,
    )
    _assert_invalidated(ctx, queue, pending_len=True, is_empty=True, in_flight_len=True)
    assert queue.ack("gamma", retry.delivery_id)
    _assert_invalidated(ctx, queue, in_flight_len=True)
    assert queue.is_empty()


def test_visibility_timeout_and_dead_letter_fixture() -> None:
    ctx: dict = {}
    visibility_timeout, max_deliveries = _work_queue_config(
        "workqueue_lease_deadletter.json"
    )
    queue = WorkQueueCell[str](
        ctx,
        visibility_timeout=visibility_timeout,
        max_deliveries=max_deliveries,
    )
    _materialize(queue)
    queue.push("poison")
    _assert_invalidated(ctx, queue, pending_len=True, is_empty=True)

    first = queue.claim("worker-1", 0)
    assert first is not None and first.deadline == 10
    _assert_invalidated(ctx, queue, pending_len=True, is_empty=True, in_flight_len=True)
    assert queue.reap_expired(10) == 0
    _assert_invalidated(ctx, queue)
    assert queue.reap_expired(11) == 1
    _assert_invalidated(ctx, queue, pending_len=True, is_empty=True, in_flight_len=True)

    second = queue.claim("worker-2", 11)
    assert second is not None and second.attempt == 2 and second.deadline == 21
    _assert_invalidated(ctx, queue, pending_len=True, is_empty=True, in_flight_len=True)
    assert queue.reap_expired(21) == 0
    _assert_invalidated(ctx, queue)
    assert queue.reap_expired(22) == 1
    _assert_invalidated(ctx, queue, in_flight_len=True, dead_letter_len=True)

    dead = queue.dead_letter_items()
    assert len(dead) == 1
    assert dead[0].item_id == 0
    assert dead[0].attempts == 2
    assert dead[0].reason is WorkQueueDeadLetterReason.EXPIRED
    assert queue.claim("worker-3", 22) is None
    _assert_invalidated(ctx, queue)
