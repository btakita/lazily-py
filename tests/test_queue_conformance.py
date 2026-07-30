"""Cross-language conformance tests for the reactive queue (``QueueCell``).

Each test loads a canonical JSON fixture from
``lazily-spec/conformance/collections`` and replays it through the lazily-py
``QueueCell``, asserting the spec's language-agnostic expectations. These are
**compute** fixtures: the harness loads the ``initial`` state, replays each
``step``'s ``op``, and asserts the ``expected`` observable effects (resulting
``elements`` / ``head`` / ``len`` / ``is_empty`` / ``is_full`` / ``closed``, and
— the core of the spec — exactly which reader classes (``head`` / ``len`` /
``is_empty`` / ``is_full`` / ``closed``) invalidate).

The same fixtures are replayed by the Rust, Zig, Kotlin, Go, C++, and JS
bindings, so all implementations stay byte-compatible on the compute
invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conformance_assert import instrument

from lazily import (
    QueueCell,
    QueuePopError,
    QueuePushError,
    Slot,
    VecDequeStorage,
    batch,
)


_SPEC = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "collections"
)


def _load(rel: str) -> dict:
    return instrument(json.loads((_SPEC / rel).read_text()), name=f"collections/{rel}")


def _spec_present() -> bool:
    return (_SPEC / "queuecell_spsc_push_pop.json").exists()


# ---------------------------------------------------------------------------
# Reader-kind slots whose invalidation we observe via ``Slot.is_in``.
# ---------------------------------------------------------------------------


class _Readers:
    """One cached slot per reader kind; ``is_in(ctx)`` reports whether the cached
    value survived the last op (cached ⇒ not invalidated)."""

    def __init__(self, ctx: dict, q: QueueCell[Any]) -> None:
        self.head = self._slot(ctx, q.head)
        self.len = self._slot(ctx, q.len)
        self.is_empty = self._slot(ctx, q.is_empty)
        self.is_full = self._slot(ctx, q.is_full)
        self.closed = self._slot(ctx, q.is_closed)

    @staticmethod
    def _slot(ctx: dict, fn: Any) -> Slot:  # type: ignore[type-arg]
        s: Slot = Slot(callable=lambda _ctx: fn(_ctx))
        s(ctx)  # materialize the cache
        return s

    def materialize_all(self, ctx: dict) -> None:
        self.head(ctx)
        self.len(ctx)
        self.is_empty(ctx)
        self.is_full(ctx)
        self.closed(ctx)


def _build_initial(ctx: dict, initial: dict) -> QueueCell[str]:
    cap = initial.get("capacity")
    if cap is not None:
        q: QueueCell[str] = QueueCell(ctx, storage=VecDequeStorage.with_capacity(cap))
    else:
        q = QueueCell(ctx)
    for e in initial.get("elements", []):
        result = q.try_push(e)
        assert result is None, f"seeding push failed: {result}"
    if initial.get("closed"):
        q.close()
    return q


def _assert_state(q: QueueCell[str], expected: dict) -> None:
    if "elements" in expected:
        assert q.elements() == expected["elements"], "elements mismatch"
    if "head" in expected:
        want = expected["head"]
        assert q.head() == want, f"head mismatch: {q.head()} want {want}"
    if "len" in expected:
        assert q.len() == expected["len"], "len mismatch"
    if "is_empty" in expected:
        assert q.is_empty() == expected["is_empty"], "is_empty mismatch"
    if "is_full" in expected:
        assert q.is_full() == expected["is_full"], "is_full mismatch"
    if "closed" in expected:
        assert q.is_closed() == expected["closed"], "closed mismatch"


def _assert_invalidation(ctx: dict, readers: _Readers, invalidates: dict) -> None:
    """Assert the per-reader-kind invalidation matrix for one step, then
    re-materialize for the next step.

    A reader kind explicitly present in ``invalidates`` is asserted
    (``True`` ⇒ must invalidate, ``False`` ⇒ must stay cached). A reader kind
    **absent** from ``invalidates`` is not asserted.
    """

    def check(name: str, reader: Slot) -> None:
        if name not in invalidates:
            return
        expected_inv = invalidates[name]
        cached = reader.is_in(ctx)
        if expected_inv:
            assert not cached, (
                f"reader `{name}` should have been invalidated but stayed cached"
            )
        else:
            assert cached, (
                f"reader `{name}` should have stayed cached but was invalidated"
            )

    check("head", readers.head)
    check("len", readers.len)
    check("is_empty", readers.is_empty)
    check("is_full", readers.is_full)
    check("closed", readers.closed)

    readers.materialize_all(ctx)


def _returns_label(result: Any) -> str | None:
    if isinstance(result, QueuePopError):
        return result.label
    if isinstance(result, QueuePushError):
        return result.label
    return None


def _run_fixture(fixture: dict) -> None:
    ctx: dict = {}
    q = _build_initial(ctx, fixture["initial"])
    readers = _Readers(ctx, q)
    readers.materialize_all(ctx)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        op_type = op["type"]
        expected = step.get("expected", {})
        invalidates = expected.get("invalidates", {})

        got_returns: Any = None
        if op_type == "push":
            result = q.try_push(op["value"])
            assert result is None, f"step {i}: push should succeed, got {result}"
        elif op_type == "try_push":
            got_returns = _returns_label(q.try_push(op["value"]))
        elif op_type in ("pop", "try_pop"):
            got_returns = q.try_pop()
            if isinstance(got_returns, QueuePopError):
                got_returns = got_returns.label
        elif op_type == "close":
            q.close()
        elif op_type == "batch":
            inner_ops = op["ops"]

            def do_batch(_ops: list = inner_ops) -> None:
                for inner in _ops:
                    assert inner["type"] == "push", "batch currently only wraps pushes"
                    result = q.try_push(inner["value"])
                    assert result is None, f"batch push failed: {result}"

            batch(do_batch)
        else:
            raise AssertionError(f"unknown queue op type: {op_type}")

        _assert_state(q, expected)

        if "returns" in step:
            want = step["returns"]
            assert got_returns == want, (
                f"step {i}: returns {got_returns!r} want {want!r}"
            )

        _assert_invalidation(ctx, readers, invalidates)


# ---------------------------------------------------------------------------
# One test per fixture (parametrized over the five canonical fixtures).
# ---------------------------------------------------------------------------


_FIXTURES = [
    "queuecell_spsc_push_pop.json",
    "queuecell_popped_head_observation.json",
    "queuecell_mpsc_multi_writer.json",
    "queuecell_bounded_backpressure.json",
    "queuecell_closure_lifecycle.json",
]


def test_queue_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    for name in _FIXTURES:
        fixture = _load(name)
        _run_fixture(fixture)
