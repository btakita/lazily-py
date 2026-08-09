"""Cross-language conformance tests for distributed coordination (``#lzcoord``).

Each test loads a canonical JSON fixture from
``lazily-spec/conformance/coordination`` and replays it through the lazily-py
coordination cells, asserting the spec's language-agnostic expectations: the
op's ``returns`` value, the projected reader values, and — the core of the
spec — exactly which reader invalidates (observed via a :class:`~lazily.Slot`
reading the projected :class:`~lazily.cell.Cell`, mirroring the Rust
``ctx.computed`` observer + ``ctx.is_set``).

The same fixtures are replayed by the Rust, Zig, Kotlin, Go, C++, and JS
bindings, so all implementations stay byte-compatible on the compute
invariants.
"""

from __future__ import annotations

import json
from typing import Any

from conformance_assert import (
    assert_invalidates,
    assert_key,
    corpus_subdir,
    instrument,
)

from lazily import Slot
from lazily.coordination import (
    BarrierCell,
    LeaderCell,
    LeaseCell,
    LockCell,
    SemaphoreCell,
)


_SPEC = corpus_subdir("coordination")


def _load(rel: str) -> dict:
    return instrument(json.loads((_SPEC / rel).read_text()), name=f"coordination/{rel}")


def _spec_present() -> bool:
    return (_SPEC / "lease.json").exists()


def _observe(ctx: dict, cell: Any) -> Slot:
    """A materialized observer Slot reading ``cell.value``; ``is_in(ctx)`` reports
    whether its cache survived the last op (cached ⇒ reader not invalidated)."""
    s: Slot = Slot(callable=lambda _ctx: _ctx.read(cell))
    s(ctx)
    return s


def _assert_inval(ctx: dict, observer: Slot, step: dict, reader: str) -> None:
    """Assert the observed reader's invalidation for one step, then
    re-materialize the observer for the next step."""
    was_cached = observer.is_in(ctx)
    observer(ctx)  # re-materialize
    assert_invalidates(
        step["expected"], {reader: not was_cached}, where=f"op {step['op']['type']}"
    )


# ---------------------------------------------------------------------------
# One test per fixture.
# ---------------------------------------------------------------------------


def _run_lease() -> None:
    fx = _load("lease.json")
    ctx: dict = {}
    lease: LeaseCell[int] = LeaseCell(ctx)
    observed = _observe(ctx, lease.holder_cell())

    for step in fx["steps"]:
        op = step["op"]
        now = op["now"]
        op_type = op["type"]
        if op_type == "acquire":
            got: Any = lease.acquire(op["peer"], now, op["ttl"])
        elif op_type == "renew":
            got = lease.renew(op["peer"], now, op["ttl"])
        elif op_type == "tick":
            got = lease.tick(now)
        else:
            raise AssertionError(f"unknown lease op: {op_type}")

        if "returns" in step:
            assert got == step["returns"], f"lease returns: {got!r}"
        exp = step["expected"]
        assert_key(exp, "holder", lease.holder(now))
        assert_key(exp, "held", lease.is_held(now))
        assert_key(exp, "fence", lease.fence())

        _assert_inval(ctx, observed, step, "holder")


def _run_leader() -> None:
    fx = _load("leader.json")
    ctx: dict = {}
    me = fx["config"]["me"]
    leader: LeaderCell[int] = LeaderCell(ctx, me)
    observed = _observe(ctx, leader.current_leader_cell())

    for step in fx["steps"]:
        op = step["op"]
        now = op["now"]
        op_type = op["type"]
        if op_type == "campaign":
            role = leader.campaign(now, op["ttl"])
        elif op_type == "contend":
            role = leader.contend(op["peer"], now, op["ttl"])
        elif op_type == "tick":
            role = leader.tick(now)
        else:
            raise AssertionError(f"unknown leader op: {op_type}")

        exp = step["expected"]
        assert_key(exp, "role", role.value)
        assert_key(exp, "current_leader", leader.current_leader(now))

        _assert_inval(ctx, observed, step, "current_leader")


def _run_lock() -> None:
    fx = _load("lock.json")
    ctx: dict = {}
    lock: LockCell[int] = LockCell(ctx)
    observed = _observe(ctx, lock.is_locked_cell())

    for step in fx["steps"]:
        op = step["op"]
        op_type = op["type"]
        if op_type == "acquire":
            got: Any = lock.acquire(op["peer"], op["now"], op["ttl"])
        elif op_type == "validate":
            got = lock.validate(op["fence"])
        elif op_type == "tick":
            got = lock.tick(op["now"])
        else:
            raise AssertionError(f"unknown lock op: {op_type}")

        if "returns" in step:
            assert got == step["returns"], f"lock returns: {got!r}"
        exp = step["expected"]
        now = op["now"]
        assert_key(exp, "is_locked", lock.is_locked(now))
        assert_key(exp, "fence", lock.fence())

        _assert_inval(ctx, observed, step, "is_locked")


def _run_semaphore() -> None:
    fx = _load("semaphore.json")
    ctx: dict = {}
    cap = fx["config"]["capacity"]
    sem = SemaphoreCell(ctx, cap)
    observed = _observe(ctx, sem.permits_available_cell())

    for step in fx["steps"]:
        op_type = step["op"]["type"]
        if op_type == "acquire":
            got: Any = sem.acquire()
        elif op_type == "release":
            got = sem.release()
        else:
            raise AssertionError(f"unknown semaphore op: {op_type}")

        if "returns" in step:
            assert got == step["returns"], f"sem returns: {got!r}"
        exp = step["expected"]
        assert_key(exp, "permits_available", sem.permits_available())

        _assert_inval(ctx, observed, step, "permits_available")


def _run_quorum() -> None:
    fx = _load("quorum.json")
    ctx: dict = {}
    total = fx["config"]["total"]
    q: BarrierCell[int] = BarrierCell.quorum(ctx, total)
    observed = _observe(ctx, q.is_open_cell())

    for step in fx["steps"]:
        got = q.arrive(step["op"]["peer"])
        if "returns" in step:
            assert got == step["returns"], f"quorum returns: {got!r}"
        exp = step["expected"]
        assert_key(exp, "votes", q.count())
        assert_key(exp, "is_open", q.is_open())

        _assert_inval(ctx, observed, step, "is_open")


def test_coordination_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    _run_lease()
    _run_leader()
    _run_lock()
    _run_semaphore()
    _run_quorum()
