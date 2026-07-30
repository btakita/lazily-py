from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from conformance_assert import instrument

from lazily.stdlib import (
    MAX_U64,
    RevisionBarrier,
    Timeout,
    TimeoutOperation,
    Timer,
    TimerError,
)


SPEC = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "stdlib"


def clean(value: object) -> dict[str, Any]:
    return {key: item for key, item in asdict(value).items() if item is not None}


def load(name: str) -> dict[str, Any]:
    return instrument(json.loads((SPEC / name).read_text()), name=f"stdlib/{name}")


def replay_timer(steps: list[dict[str, Any]]) -> None:
    timer: Timer | None = None
    last: dict[str, Any]
    for step in steps:
        if step["op"] == "start":
            try:
                timer = Timer(step["now"], step["duration"])
                last = {"outcome": "pending", "deadline": timer.deadline}
            except TimerError as error:
                last = {"outcome": "unavailable", "reason": error.reason}
        else:
            assert timer is not None
            last = clean(timer.observe(step["now"]))
        assert last == step["expect"]


def replay_timeout(steps: list[dict[str, Any]]) -> None:
    timeout: Timeout[str] | None = None
    for step in steps:
        if step["op"] == "start":
            try:
                timeout = Timeout(step["now"], step["duration"])
                actual = {"outcome": "pending", "deadline": timeout.deadline}
            except TimerError as error:
                actual = {"outcome": "unavailable", "reason": error.reason}
        else:
            assert timeout is not None
            operation_calls = 0
            cancellation_calls = 0
            operation_state = step["operation"]
            operation_value = step.get("value")
            cancellation_state = step["cancellation"]

            def operation(
                operation_state: str = operation_state,
                operation_value: object = operation_value,
            ) -> TimeoutOperation[str]:
                nonlocal operation_calls
                operation_calls += 1
                if operation_state == "completed":
                    assert isinstance(operation_value, str)
                    return TimeoutOperation.completed(operation_value)
                if operation_state == "unavailable":
                    return TimeoutOperation.unavailable()
                return TimeoutOperation.pending()

            def cancellation(cancellation_state: str = cancellation_state) -> str:
                nonlocal cancellation_calls
                cancellation_calls += 1
                return cancellation_state

            actual = clean(timeout.poll(step["now"], operation, cancellation))
            actual.update(
                operation_calls=operation_calls,
                cancellation_calls=cancellation_calls,
            )
        assert actual == step["expect"]


def replay_barrier(steps: list[dict[str, Any]]) -> None:
    barrier: RevisionBarrier | None = None
    for step in steps:
        calls = 0
        if step["op"] == "start":
            barrier = RevisionBarrier(
                step["revision"], step["required_revision"], step["deadline"]
            )
            value = barrier.receipt("")
        else:
            assert barrier is not None
            if step["op"] == "observe":
                cancellation_state = step["cancellation"]

                def cancellation(cancellation_state: str = cancellation_state) -> str:
                    nonlocal calls
                    calls += 1
                    return cancellation_state

                value = barrier.observe(step["now"], step["predicate"], cancellation)
            elif step["op"] == "register_recheck":
                value = barrier.register_recheck(
                    step["now"], step["observed_revision"], step["predicate"]
                )
            elif step["op"] == "advance":
                value = barrier.advance(step["revision"], step["predicate"])
            elif step["op"] == "dispose":
                value = barrier.dispose()
            else:
                value = barrier.receipt(step["key"])
        actual = clean(value)
        if step["op"] == "observe":
            actual["cancellation_calls"] = calls
        assert actual == step["expect"]


def test_stdlib_canonical_corpus() -> None:
    runners = {
        "stdlib_timer_v1": replay_timer,
        "stdlib_timeout_v1": replay_timeout,
        "stdlib_revision_barrier_v1": replay_barrier,
    }
    for name in ("timer.json", "timeout.json", "revision_barrier.json"):
        fixture = load(name)
        scenarios = {scenario["id"] for scenario in fixture["scenarios"]}
        for scenario in fixture["scenarios"]:
            runners[fixture["feature"]](scenario["steps"])
        for mutation in fixture["mutations"]:
            assert mutation["must_fail"]
            assert set(mutation["must_fail"]) <= scenarios


def test_async_adapters_are_caller_driven() -> None:
    async def run() -> None:
        timeout: Timeout[str] = Timeout(0, 10)
        calls: list[str] = []

        async def operation() -> TimeoutOperation[str]:
            calls.append("operation")
            return TimeoutOperation.completed("async")

        async def cancellation() -> str:
            calls.append("cancellation")
            return "pending"

        assert clean(await timeout.poll_async(2, operation, cancellation)) == {
            "outcome": "completed",
            "value": "async",
        }
        assert calls == ["operation", "cancellation"]

        barrier = RevisionBarrier(0, 1)

        async def cancelled() -> str:
            return "cancelled"

        assert (await barrier.observe_async(0, False, cancelled)).outcome == "cancelled"

    asyncio.run(run())


def test_public_logical_inputs_are_strict_uint64() -> None:
    with pytest.raises(TypeError):
        Timer(0.5, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Timer(True, 1)
    with pytest.raises(ValueError):
        Timer(-1, 1)

    timer = Timer(0, 1)
    with pytest.raises(ValueError):
        timer.observe(MAX_U64 + 1)

    timeout: Timeout[str] = Timeout(0, 10)
    with pytest.raises(TypeError):
        timeout.poll(  # type: ignore[arg-type]
            1.5,
            TimeoutOperation.pending,
            lambda: "pending",
        )

    with pytest.raises(ValueError):
        RevisionBarrier(MAX_U64 + 1, 1)
    with pytest.raises(TypeError):
        RevisionBarrier(0, 1, False)

    barrier = RevisionBarrier(0, 1)
    with pytest.raises(ValueError):
        barrier.observe(MAX_U64 + 1, False, lambda: "pending")
    with pytest.raises(TypeError):
        barrier.register_recheck(0, 0.5, False)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        barrier.advance(MAX_U64 + 1, False)


def test_barrier_clock_regression_skips_cancellation_and_latches() -> None:
    barrier = RevisionBarrier(0, 2)
    cancellation_calls = 0

    def pending() -> str:
        nonlocal cancellation_calls
        cancellation_calls += 1
        return "pending"

    assert barrier.observe(5, False, pending).outcome == "pending"
    regressed = barrier.observe(4, False, pending)
    assert clean(regressed) == {
        "outcome": "unavailable",
        "revision": 0,
        "generation": 0,
        "reason": "clock_regression",
    }
    assert cancellation_calls == 1

    registration = barrier.register_recheck(4, 2, True)
    assert registration.outcome == "unavailable"
    assert registration.revision == 0
    assert registration.generation == 0

    latched = barrier.advance(2, True)
    assert latched.outcome == "unavailable"
    assert latched.reason == "clock_regression"
    assert latched.revision == 0
    assert latched.generation == 0


def test_sync_adapters_preserve_reentrant_first_terminal_result() -> None:
    timeout: Timeout[str] = Timeout(0, 10)

    def operation() -> TimeoutOperation[str]:
        inner = timeout.poll(
            2,
            lambda: TimeoutOperation.completed("first"),
            lambda: "pending",
        )
        assert inner.outcome == "completed"
        return TimeoutOperation.pending()

    outer = timeout.poll(1, operation, lambda: "cancelled")
    assert outer.outcome == "completed"
    assert outer.value == "first"

    barrier = RevisionBarrier(0, 1)

    def dispose_then_cancel() -> str:
        assert barrier.dispose().outcome == "disposed"
        return "cancelled"

    assert barrier.observe(0, False, dispose_then_cancel).outcome == "disposed"
    assert barrier.receipt("").outcome == "disposed"


def test_async_adapters_start_together_and_preserve_reentrant_terminal() -> None:
    async def run() -> None:
        timeout: Timeout[str] = Timeout(0, 10)
        never = asyncio.Event()
        calls: list[str] = []

        async def operation() -> TimeoutOperation[str]:
            calls.append("operation")
            await never.wait()
            return TimeoutOperation.pending()

        async def cancellation() -> str:
            calls.append("cancellation")
            return "cancelled"

        task = asyncio.create_task(timeout.poll_async(1, operation, cancellation))
        for _ in range(3):
            await asyncio.sleep(0)
            if len(calls) == 2:
                break
        assert calls == ["operation", "cancellation"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        reentrant: Timeout[str] = Timeout(0, 10)

        async def reentrant_operation() -> TimeoutOperation[str]:
            reentrant.poll(
                2,
                lambda: TimeoutOperation.completed("first"),
                lambda: "pending",
            )
            return TimeoutOperation.pending()

        result = await reentrant.poll_async(
            1,
            reentrant_operation,
            cancellation,
        )
        assert result.outcome == "completed"
        assert result.value == "first"

        barrier = RevisionBarrier(0, 1)

        async def dispose_then_cancel() -> str:
            barrier.dispose()
            return "cancelled"

        assert (
            await barrier.observe_async(0, False, dispose_then_cancel)
        ).outcome == "disposed"

    asyncio.run(run())
