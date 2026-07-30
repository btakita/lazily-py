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

from conformance_assert import assert_invalidates, assert_key, instrument

from lazily import Slot
from lazily.resilience import (
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


def _assert_inval(ctx: dict, obs: Slot, expected: dict, name: str, step: int) -> None:  # type: ignore[type-arg]
    """Assert the fixture's invalidation map for one reader, then re-materialize
    the observer for the next step."""
    cached = obs.is_in(ctx)
    assert_invalidates(expected, {name: not cached}, where=f"step {step}")
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
        if op["type"] == "record":
            cb.record(op["success"], op["now"])
        elif op["type"] == "allow":
            got = cb.allow(op["now"])
            assert got == step["returns"], f"step {i}: allow returns {got!r}"
        else:
            raise AssertionError(f"unknown circuit_breaker op: {op['type']}")

        assert_key(expected, "state", cb.state().value, where=f"step {i}")
        _assert_inval(ctx, obs, expected, "state", i)


def _run_retry() -> None:
    fx = _load("retry.json")
    ctx: dict = {}
    cfg = fx["config"]
    r = RetryPolicyCell(ctx, cfg["base"], cfg["cap"])
    obs = _observer(ctx, r.delay)

    for i, step in enumerate(fx["steps"]):
        op = step["op"]
        expected = step["expected"]
        assert op["type"] == "next", f"unknown retry op: {op['type']}"
        got = r.next_delay()
        assert got == step["returns"], f"step {i}: next returns {got!r}"
        assert_key(expected, "delay", r.delay(), where=f"step {i}")
        _assert_inval(ctx, obs, expected, "delay", i)


def _run_bulkhead() -> None:
    fx = _load("bulkhead.json")
    ctx: dict = {}
    b = BulkheadCell(ctx, fx["config"]["capacity"])
    obs = _observer(ctx, b.permits_in_use)

    for i, step in enumerate(fx["steps"]):
        op = step["op"]
        expected = step["expected"]
        if op["type"] == "acquire":
            got = b.acquire()
            assert got == step["returns"], f"step {i}: acquire returns {got!r}"
        elif op["type"] == "release":
            b.release()
        else:
            raise AssertionError(f"unknown bulkhead op: {op['type']}")

        assert_key(expected, "in_use", b.permits_in_use(), where=f"step {i}")
        _assert_inval(ctx, obs, expected, "in_use", i)


def _run_timeout() -> None:
    fx = _load("timeout.json")
    ctx: dict = {}
    t = TimeoutCell(ctx)
    obs = _observer(ctx, t.is_timed_out)

    for i, step in enumerate(fx["steps"]):
        op = step["op"]
        expected = step["expected"]
        now = op["now"]
        if op["type"] == "arm":
            t.arm(now, op["timeout"])
            got = False
        elif op["type"] == "tick":
            got = t.tick(now)
        else:
            raise AssertionError(f"unknown timeout op: {op['type']}")

        assert got == step["returns"], f"step {i}: edge {got!r}"
        assert_key(expected, "is_timed_out", t.is_timed_out(), where=f"step {i}")
        _assert_inval(ctx, obs, expected, "is_timed_out", i)


def test_resilience_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    _run_circuit_breaker()
    _run_retry()
    _run_bulkhead()
    _run_timeout()
