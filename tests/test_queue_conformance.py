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
from typing import Any

from conformance_assert import (
    assert_invalidates,
    assert_key,
    corpus_subdir,
    instrument,
)

from lazily import (
    QueueCell,
    QueuePopError,
    QueuePushError,
    Slot,
    VecDequeStorage,
    batch,
)


_SPEC = corpus_subdir("collections")


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


def _assert_state(q: QueueCell[str], expected: dict, where: str) -> None:
    observations = {
        "elements": q.elements,
        "head": q.head,
        "len": q.len,
        "is_empty": q.is_empty,
        "is_full": q.is_full,
        "closed": q.is_closed,
    }
    for name, observe in observations.items():
        if name in expected:
            assert_key(expected, name, observe(), where=where)


def _assert_invalidation(
    ctx: dict, readers: _Readers, expected: dict, where: str
) -> None:
    """Assert the per-reader-kind invalidation matrix for one step, then
    re-materialize for the next step.

    Every reader kind the matrix names must be one this harness observes, or the
    fixture is expecting something of a reader nobody watched.
    """
    observed = {
        "head": not readers.head.is_in(ctx),
        "len": not readers.len.is_in(ctx),
        "is_empty": not readers.is_empty.is_in(ctx),
        "is_full": not readers.is_full.is_in(ctx),
        "closed": not readers.closed.is_in(ctx),
    }
    assert_invalidates(expected, observed, where=where)

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
        where = f"step {i}"

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

        _assert_state(q, expected, where)

        if "returns" in step:
            want = step["returns"]
            assert got_returns == want, (
                f"step {i}: returns {got_returns!r} want {want!r}"
            )

        _assert_invalidation(ctx, readers, expected, where)


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
