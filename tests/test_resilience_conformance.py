"""Cross-language conformance for fault-tolerance primitives (``#lzresilience``).

Each test loads a canonical JSON fixture from
``lazily-spec/conformance/resilience`` and replays it through the lazily-py
reactive cells (:class:`CircuitBreakerCell`, :class:`RetryPolicyCell`,
:class:`BulkheadCell`, :class:`TimeoutCell`), asserting the spec's
language-agnostic expectations: each op's ``returns``, the resulting reader
value (``state`` / ``delay`` / ``in_use`` / ``is_timed_out``), and exactly which
reader invalidates.

The same fixtures are replayed by the Rust, Zig, Kotlin, Go, C++, and JS
bindings, so all implementations stay byte-compatible on the compute invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conformance_assert import instrument

from lazily import Slot
from lazily.resilience import (
    BreakerState,
    BulkheadCell,
    CircuitBreakerCell,
    RetryPolicyCell,
    TimeoutCell,
)


_SPEC = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "resilience"
)


def _load(rel: str) -> dict:
    return instrument(json.loads((_SPEC / rel).read_text()), name=f"resilience/{rel}")


def _spec_present() -> bool:
    return (_SPEC / "circuit_breaker.json").exists()


def _observer(ctx: dict, reader: Any) -> Slot:  # type: ignore[type-arg]
    """A materialized observer Slot over a reactive reader. ``is_in(ctx)`` reports
    whether the cached value survived the last op (cached => not invalidated)."""
    s: Slot = Slot(callable=lambda _ctx: reader(_ctx))
    s(ctx)  # materialize the cache
    return s


def _assert_inval(ctx: dict, obs: Slot, invalidates: dict, name: str) -> None:  # type: ignore[type-arg]
    """Assert the invalidation for one reader, then re-materialize for the next
    step. A reader absent from ``invalidates`` is not asserted."""
    if name in invalidates:
        cached = obs.is_in(ctx)
        if invalidates[name]:
            assert not cached, (
                f"reader `{name}` should have been invalidated but stayed cached"
            )
        else:
            assert cached, (
                f"reader `{name}` should have stayed cached but was invalidated"
            )
    obs(ctx)  # re-materialize


def _run_circuit_breaker() -> None:
    fx = _load("circuit_breaker.json")
    ctx: dict = {}
    cfg = fx["config"]
    cb = CircuitBreakerCell(
        ctx, cfg["window"], cfg["failure_threshold"], cfg["reset_timeout"]
    )
    obs = _observer(ctx, cb.state)

    for i, step in enumerate(fx["steps"]):
        op = step["op"]
        expected = step["expected"]
        invalidates = expected.get("invalidates", {})
        if op["type"] == "record":
            cb.record(op["success"], op["now"])
        elif op["type"] == "allow":
            got = cb.allow(op["now"])
            assert got == step["returns"], f"step {i}: allow returns {got!r}"
        else:
            raise AssertionError(f"unknown circuit_breaker op: {op['type']}")

        want_state = BreakerState(expected["state"])
        assert cb.state() == want_state, (
            f"step {i}: state {cb.state()} want {want_state}"
        )
        _assert_inval(ctx, obs, invalidates, "state")


def _run_retry() -> None:
    fx = _load("retry.json")
    ctx: dict = {}
    cfg = fx["config"]
    r = RetryPolicyCell(ctx, cfg["base"], cfg["cap"])
    obs = _observer(ctx, r.delay)

    for i, step in enumerate(fx["steps"]):
        op = step["op"]
        expected = step["expected"]
        invalidates = expected.get("invalidates", {})
        assert op["type"] == "next", f"unknown retry op: {op['type']}"
        got = r.next_delay()
        assert got == step["returns"], f"step {i}: next returns {got!r}"
        assert r.delay() == expected["delay"], f"step {i}: delay mismatch"
        _assert_inval(ctx, obs, invalidates, "delay")


def _run_bulkhead() -> None:
    fx = _load("bulkhead.json")
    ctx: dict = {}
    b = BulkheadCell(ctx, fx["config"]["capacity"])
    obs = _observer(ctx, b.permits_in_use)

    for i, step in enumerate(fx["steps"]):
        op = step["op"]
        expected = step["expected"]
        invalidates = expected.get("invalidates", {})
        if op["type"] == "acquire":
            got = b.acquire()
            assert got == step["returns"], f"step {i}: acquire returns {got!r}"
        elif op["type"] == "release":
            b.release()
        else:
            raise AssertionError(f"unknown bulkhead op: {op['type']}")

        assert b.permits_in_use() == expected["in_use"], f"step {i}: in_use mismatch"
        _assert_inval(ctx, obs, invalidates, "in_use")


def _run_timeout() -> None:
    fx = _load("timeout.json")
    ctx: dict = {}
    t = TimeoutCell(ctx)
    obs = _observer(ctx, t.is_timed_out)

    for i, step in enumerate(fx["steps"]):
        op = step["op"]
        expected = step["expected"]
        invalidates = expected.get("invalidates", {})
        now = op["now"]
        if op["type"] == "arm":
            t.arm(now, op["timeout"])
            got = False
        elif op["type"] == "tick":
            got = t.tick(now)
        else:
            raise AssertionError(f"unknown timeout op: {op['type']}")

        assert got == step["returns"], f"step {i}: edge {got!r}"
        assert t.is_timed_out() == expected["is_timed_out"], (
            f"step {i}: is_timed_out mismatch"
        )
        _assert_inval(ctx, obs, invalidates, "is_timed_out")


def test_resilience_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    _run_circuit_breaker()
    _run_retry()
    _run_bulkhead()
    _run_timeout()
