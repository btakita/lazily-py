from __future__ import annotations

import pytest

from lazily import (
    TopicCell,
    TopicDurability,
    TopicSnapshot,
    TopicSubscribeOutcome,
    TopicSubscriptionSnapshot,
)


def test_broadcast_cursor_isolation() -> None:
    topic: TopicCell[str] = TopicCell({})
    assert topic.subscribe("alice") is TopicSubscribeOutcome.Subscribed
    assert topic.subscribe("bob") is TopicSubscribeOutcome.Subscribed
    assert topic.publish("a") == 0
    assert topic.publish("b") == 1
    assert topic.read_stream("alice") == ["a", "b"]
    assert topic.read_stream("bob") == ["a", "b"]
    assert topic.advance("alice") == "a"
    assert topic.read_stream("alice") == ["b"]
    assert topic.read_stream("bob") == ["a", "b"]


def test_durable_replay_and_safe_gc() -> None:
    topic: TopicCell[str] = TopicCell({})
    topic.subscribe("fast")
    topic.subscribe("slow")
    topic.publish("a")
    topic.publish("b")
    assert topic.advance("fast", 2) == "a"
    assert topic.advance("slow") == "a"
    topic.disconnect("slow")
    topic.publish("c")
    assert topic.gc() == 1
    assert topic.base_offset == 1
    assert topic.elements() == ["b", "c"]
    topic.reconnect("slow")
    assert topic.read_stream("slow") == ["b", "c"]
    restored = TopicCell({}, topic.snapshot())
    assert restored.snapshot() == topic.snapshot()


def test_ephemeral_disconnect_does_not_hold_gc() -> None:
    topic: TopicCell[str] = TopicCell({})
    topic.subscribe("durable", TopicDurability.Durable)
    topic.subscribe("viewer", TopicDurability.Ephemeral)
    topic.publish("a")
    assert topic.advance("durable") == "a"
    topic.disconnect("viewer")
    assert topic.subscription("viewer") is None
    assert topic.gc() == 1
    assert (
        topic.subscribe("viewer", TopicDurability.Ephemeral)
        is TopicSubscribeOutcome.Subscribed
    )
    viewer = topic.subscription("viewer")
    assert viewer is not None and viewer.cursor == topic.tail_offset


def test_tail_and_offline_advance_are_noops() -> None:
    topic: TopicCell[str] = TopicCell({})
    topic.subscribe("worker")
    topic.publish("a")
    assert topic.advance("worker") == "a"
    assert topic.advance("worker") is None

    topic.disconnect("worker")
    topic.publish("b")
    assert topic.read_stream("worker") == []
    assert topic.advance("worker") is None
    worker = topic.subscription("worker")
    assert worker is not None and worker.cursor == 1

    topic.reconnect("worker")
    assert topic.read_stream("worker") == ["b"]
    assert topic.gc() == 1
    assert topic.base_offset == 1
    worker = topic.subscription("worker")
    assert worker is not None and worker.cursor == 1


def test_snapshot_rejects_disconnected_ephemeral_subscription() -> None:
    invalid_snapshot = TopicSnapshot(
        0,
        (),
        (TopicSubscriptionSnapshot("viewer", 0, TopicDurability.Ephemeral, False),),
    )
    with pytest.raises(ValueError, match="disconnected ephemeral"):
        TopicCell({}, invalid_snapshot)
