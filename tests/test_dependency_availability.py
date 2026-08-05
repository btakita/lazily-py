"""Exact-key dependency availability conformance (``#lzdependencyavailability``)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from conformance_assert import assert_key, instrument

from lazily import (
    AsyncDependencyMap,
    DependencyAvailability,
    DependencyMap,
    Slot,
    ThreadSafeDependencyMap,
)


def test_exact_key_availability_is_a_value_transition() -> None:
    fixture = instrument(
        json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "lazily-spec/conformance/collections"
                / "dependency_reactive_availability.json"
            ).read_text()
        ),
        name="collections/dependency_reactive_availability.json",
    )
    ctx: dict = {}
    dependencies = DependencyMap[str, int](ctx)
    runs = [0]

    @Slot
    def wanted(compute: dict) -> DependencyAvailability[int]:
        runs[0] += 1
        return dependencies.observe_dependency(fixture["key"], compute)

    identity = None
    for index, step in enumerate(fixture["steps"]):
        op = step["op"]
        match op["type"]:
            case "observe_dependency":
                wanted(ctx)
            case "publish":
                dependencies.publish(op["key"], op["value"])
            case "unpublish":
                dependencies.unpublish(op["key"])
            case other:
                raise AssertionError(f"step {index}: unsupported operation {other}")

        state = wanted(ctx)
        projected = {"Available": state.value} if state.available else "Unavailable"
        expected = step["expected"]
        assert_key(expected, "state", projected)
        assert_key(expected, "recomputes", runs[0])
        assert_key(expected, "present_count", dependencies.present_count())
        handle = dependencies.handle(fixture["key"])
        if identity is None:
            identity = handle
        assert handle is identity
        assert_key(expected, "identity", "wanted-1")


def test_thread_safe_first_observers_share_one_slot() -> None:
    dependencies = ThreadSafeDependencyMap[str, int]({})
    barrier = threading.Barrier(16)
    handles: list[object] = []
    lock = threading.Lock()

    def observe() -> None:
        barrier.wait()
        dependencies.observe_dependency("wanted")
        handle = dependencies.handle("wanted")
        with lock:
            handles.append(handle)

    threads = [threading.Thread(target=observe) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert dependencies.present_count() == 1
    assert handles[0] is not None
    assert all(handle is handles[0] for handle in handles)


def test_async_flavor_preserves_availability_identity() -> None:
    dependencies = AsyncDependencyMap[str, int]({})

    assert dependencies.observe_dependency("wanted") == (
        DependencyAvailability.unavailable()
    )
    handle = dependencies.handle("wanted")
    dependencies.publish("wanted", 7)
    assert dependencies.observe_dependency("wanted") == (
        DependencyAvailability.available_value(7)
    )
    dependencies.unpublish("wanted")
    assert dependencies.observe_dependency("wanted") == (
        DependencyAvailability.unavailable()
    )
    assert dependencies.handle("wanted") is handle
