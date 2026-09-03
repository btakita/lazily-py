"""Replay the canonical latest-durable projection trace across all Python shells."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conformance_assert import corpus_subdir, instrument, scenarios

import lazily


_NAME = "egress/latest_durable_projection.json"
_FIXTURE = corpus_subdir("egress") / "latest_durable_projection.json"
FLAVORS = (
    lazily.LatestDurableProjection,
    lazily.ThreadSafeLatestDurableProjection,
    lazily.AsyncLatestDurableProjection,
)


def _load() -> dict[str, Any]:
    if not _FIXTURE.exists():
        pytest.skip(f"canonical latest-durable fixture not found at {_FIXTURE}")
    return instrument(json.loads(_FIXTURE.read_text()), name=_NAME)


def _revision(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"epoch": value.epoch, "value": value.value}


def _envelope(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "generation": value.generation,
        "key": value.key,
        "epoch": value.epoch,
        "value": value.value,
    }


def _snapshot(value: Any) -> dict[str, Any]:
    return {
        "generation": value.generation,
        "entries": [
            {
                "key": state.key,
                "desired": _revision(state.desired),
                "inflight": _envelope(state.inflight),
                "durable_through": state.durable_through,
            }
            for state in value.entries
        ],
    }


def _outcome(value: Any) -> dict[str, Any]:
    if isinstance(value, lazily.LatestDurableUpsert):
        result = {"upsert": str(value.kind)}
        if value.durable_through is not None:
            result["durable_through"] = value.durable_through
        if value.current is not None:
            result["current"] = value.current
        return result
    if isinstance(value, lazily.LatestDurableClaim):
        result = {"claim": str(value.kind)}
        if value.envelope is not None:
            result["envelope"] = _envelope(value.envelope)
        if value.current is not None:
            result["current"] = value.current
        return result
    if isinstance(value, lazily.LatestDurableAck):
        result = {"ack": str(value.kind)}
        if value.durable_through is not None:
            result["durable_through"] = value.durable_through
        if value.current is not None:
            result["current"] = value.current
        return result
    if isinstance(value, lazily.LatestDurableFailure):
        result = {"failure": str(value.kind)}
        if value.current is not None:
            result["current"] = value.current
        return result
    if isinstance(value, lazily.LatestDurableReconnect):
        result = {"reconnect": str(value.kind)}
        for name in ("generation", "requeued", "superseded", "current"):
            field = getattr(value, name)
            if field is not None:
                result[name] = field
        return result
    raise AssertionError(f"unexpected latest-durable outcome {value!r}")


def _apply(projection: Any, op: dict[str, Any]) -> Any:
    kind = op["type"]
    if kind == "upsert_desired":
        return projection.upsert_desired(op["key"], op["epoch"], op["value"])
    if kind == "claim":
        return projection.claim(op["key"], op["generation"])
    if kind == "ack_applied":
        return projection.ack_applied(op["key"], op["generation"], op["epoch"])
    if kind == "fail_retryable":
        return projection.fail_retryable(op["key"], op["generation"], op["epoch"])
    if kind == "reconnect":
        return projection.reconnect(op["generation"])
    raise AssertionError(f"unknown latest-durable operation {kind!r}")


class _Counter:
    def __init__(self, ctx: dict, projection: Any) -> None:
        self.count = 0

        def compute(view: Any) -> Any:
            self.count += 1
            return projection.snapshot(view)

        self._node = lazily.computed(ctx, compute)

    def drive(self) -> tuple[int, Any]:
        value = self._node.get()
        return self.count, value


@pytest.mark.parametrize("projection_cls", FLAVORS)
def test_canonical_latest_durable_projection_fixture(projection_cls: type[Any]) -> None:
    fixture = _load()
    assert fixture["kind"] == "LatestDurableProjection"
    assert fixture["model"] == "LatestDurableProjectionCore"
    count = 0

    for scenario in scenarios(fixture):
        ctx: dict = {}
        projection = projection_cls(ctx, scenario["generation"])
        counter = _Counter(ctx, projection)
        _, previous = counter.drive()
        steps = scenario["steps"]
        assert steps

        for index, step in enumerate(steps):
            before_runs, _ = counter.drive()
            returned = _outcome(_apply(projection, step["op"]))
            after_runs, current = counter.drive()
            where = f"{projection_cls.__name__} step {index}"
            assert step["returns"] == returned, f"{where}: outcome"
            actual = _snapshot(current)
            assert step["expected"] == actual, f"{where}: state"
            assert (after_runs > before_runs) == (current != previous), (
                f"{where}: invalidation must match a real state transition"
            )
            previous = current
            count += 1

    assert count >= 20


def test_core_preserves_newer_desire_across_failure_and_stale_tokens() -> None:
    core = lazily.LatestDurableProjectionCore[str, str](4)
    assert core.upsert_desired("doc", 1, "one")[1].kind == "accepted"
    assert core.claim("doc", 4)[1].kind == "claimed"
    assert core.upsert_desired("doc", 2, "two")[1].kind == "accepted"

    _, failure = core.fail_retryable("doc", 4, 1)
    assert failure.kind == lazily.LatestDurableFailureKind.SUPERSEDED
    state = core.state("doc")
    assert state is not None
    assert state.inflight is None
    assert state.desired == lazily.LatestDurableRevision(2, "two")

    before = core.snapshot()
    assert core.ack_applied("doc", 3, 1)[1].kind == "stale_generation"
    assert core.claim("doc", 5)[1].kind == "stale_generation"
    assert core.snapshot() == before


def test_core_enforces_epoch_identity_and_monotone_durability() -> None:
    core = lazily.LatestDurableProjectionCore[str, str](1)
    core.upsert_desired("doc", 2, "two")
    assert core.upsert_desired("doc", 1, "old")[1] == lazily.LatestDurableUpsert(
        lazily.LatestDurableUpsertKind.STALE_EPOCH,
        current=2,
    )
    assert core.upsert_desired("doc", 2, "other")[1].kind == "epoch_conflict"
    core.claim("doc", 1)
    assert core.ack_applied("doc", 1, 3)[1].kind == "unknown_epoch"
    assert core.ack_applied("doc", 1, 2)[1].kind == "advanced"
    assert core.ack_applied("doc", 1, 2)[1] == lazily.LatestDurableAck(
        lazily.LatestDurableAckKind.UNCHANGED,
        durable_through=2,
    )
    assert core.upsert_desired("doc", 1, "old")[1] == lazily.LatestDurableUpsert(
        lazily.LatestDurableUpsertKind.ALREADY_DURABLE,
        durable_through=2,
    )
