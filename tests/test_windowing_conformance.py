"""Cross-language conformance tests for stream windowing (``#lzwindow``).

Each test loads a canonical JSON fixture from
``lazily-spec/conformance/windowing`` and replays it through the lazily-py
windowing cells, asserting the spec's language-agnostic expectations. Window
aggregation reuses the merge algebra (all fixtures use ``Sum`` over ``int``): the
aggregate of a window is the associative fold of its elements. These are
**compute** fixtures — the harness builds the window from ``config``, replays
each step's op, and asserts the emitted aggregate (``returns``), the projected
output (``expected.output``), and emit-only invalidation of the ``output`` reader
(``expected.invalidates.output``).

The same fixtures are replayed by the Rust, Zig, Kotlin, Go, C++, and JS
bindings, so all implementations stay byte-compatible on the compute invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conformance_assert import assert_invalidates, assert_key, instrument

from lazily import Slot, Sum
from lazily.windowing import (
    SessionCell,
    SlidingCell,
    TumblingCountCell,
    TumblingTimeCell,
)


_SPEC = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "windowing"
)


def _load(rel: str) -> dict:
    return instrument(json.loads((_SPEC / rel).read_text()), name=f"windowing/{rel}")


def _spec_present() -> bool:
    return (_SPEC / "tumbling_count.json").exists()


def _observer(ctx: dict, cell: Any) -> Slot:
    """A cached slot reading the window's output cell; ``is_in(ctx)`` reports
    whether the cached value survived the last op (cached ⇒ not invalidated)."""
    s: Slot = Slot(callable=lambda _ctx: _ctx.read(cell))
    s(ctx)  # materialize the cache
    return s


def _check(ctx: dict, observed: Slot, step: dict, out: Any) -> None:
    expected = step.get("expected", {})
    if "output" in expected:
        assert_key(expected, "output", out, where=f"{step['op']}")
    else:
        assert out is None, f"no expected output declared, got {out!r} for {step}"
    cached = observed.is_in(ctx)
    assert_invalidates(expected, {"output": not cached}, where=f"{step['op']}")
    observed(ctx)  # re-materialize for the next step


def _run_count(fx: dict) -> None:
    ctx: dict = {}
    n = fx["config"]["n"]
    w: TumblingCountCell[int] = TumblingCountCell(ctx, n, Sum)
    observed = _observer(ctx, w.output_cell())
    for step in fx["steps"]:
        emitted = w.push(step["op"]["value"])
        assert emitted == step["returns"], f"emit mismatch for {step}"
        _check(ctx, observed, step, w.output())


def _run_time(fx: dict) -> None:
    ctx: dict = {}
    period = fx["config"]["period"]
    w: TumblingTimeCell[int] = TumblingTimeCell(ctx, period, Sum)
    observed = _observer(ctx, w.output_cell())
    for step in fx["steps"]:
        op = step["op"]
        now = op["now"]
        if op["type"] == "push":
            w.push(now, op["value"])
            emitted = None
        else:
            emitted = w.tick(now)
        assert emitted == step["returns"], f"emit mismatch for {step}"
        _check(ctx, observed, step, w.output())


def _run_sliding(fx: dict) -> None:
    ctx: dict = {}
    size = fx["config"]["size"]
    slide = fx["config"]["slide"]
    w: SlidingCell[int] = SlidingCell(ctx, size, slide, Sum)
    observed = _observer(ctx, w.output_cell())
    for step in fx["steps"]:
        emitted = w.push(step["op"]["value"])
        assert emitted == step["returns"], f"emit mismatch for {step}"
        _check(ctx, observed, step, w.output())


def _run_session(fx: dict) -> None:
    ctx: dict = {}
    gap = fx["config"]["gap"]
    w: SessionCell[int] = SessionCell(ctx, gap, Sum)
    observed = _observer(ctx, w.output_cell())
    for step in fx["steps"]:
        op = step["op"]
        now = op["now"]
        if op["type"] == "push":
            emitted = w.push(now, op["value"])
        else:
            emitted = w.flush(now)
        assert emitted == step["returns"], f"emit mismatch for {step}"
        _check(ctx, observed, step, w.output())


def test_windowing_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    _run_count(_load("tumbling_count.json"))
    _run_time(_load("tumbling_time.json"))
    _run_sliding(_load("sliding_count.json"))
    _run_session(_load("session.json"))
