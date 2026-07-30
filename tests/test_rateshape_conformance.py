"""Cross-language conformance tests for the rate-shaping source operators
(``#lzrateshape``).

Each test loads a canonical JSON fixture from
``lazily-spec/conformance/rateshape`` and replays it through the lazily-py
operators, asserting the spec's language-agnostic expectations. These are
**compute** fixtures: the harness loads the ``initial`` state, replays each
``step``'s ``op`` (``input`` / ``tick``), and asserts the emitted value
(``returns``), the projected ``output``, and that the ``output`` reader
invalidates exactly on an emit (observed via a wrapping :class:`~lazily.Slot`).

The same fixtures are replayed by the Rust, Zig, Kotlin, Go, C++, and JS
bindings, so all implementations stay byte-compatible on the compute
invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conformance_assert import assert_invalidates, assert_key, instrument


if TYPE_CHECKING:
    from collections.abc import Callable

from lazily import Slot
from lazily.rateshape import (
    DebounceCell,
    Lcg,
    ProbabilisticSampleCell,
    SampleCell,
    SampleMode,
    ThrottleCell,
    ThrottleEdge,
)


_SPEC = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "rateshape"
)


def _load(rel: str) -> dict:
    return instrument(json.loads((_SPEC / rel).read_text()), name=f"rateshape/{rel}")


def _spec_present() -> bool:
    return (_SPEC / "debounce.json").exists()


def _observer(ctx: dict, output: Callable[[], Any]) -> Slot:
    """A cached Slot wrapping the operator's ``output`` reader; ``is_in(ctx)``
    reports whether the cache survived the last op (cached ⇒ not invalidated)."""
    s: Slot = Slot(callable=lambda _ctx: output(_ctx))
    s(ctx)  # materialize the cache (subscribes to the output cell)
    return s


def _run(
    ctx: dict,
    fixture: dict,
    output: Callable[[], Any],
    drive: Callable[[dict], Any],
) -> None:
    """Replay ``fixture``'s steps. ``drive(step)`` performs the op and returns
    the emitted value; ``output()`` reads the current projected output. ``ctx``
    is the shared context the operator cell was built in."""
    observer = _observer(ctx, output)

    for i, step in enumerate(fixture["steps"]):
        emitted = drive(step)

        want_ret = step.get("returns")
        assert emitted == want_ret, f"step {i}: emit {emitted!r} want {want_ret!r}"

        expected = step.get("expected", {})
        if "output" in expected:
            assert_key(expected, "output", output(), where=f"step {i}")
        else:
            assert output() is None, f"step {i}: undeclared output {output()!r}"

        cached = observer.is_in(ctx)
        assert_invalidates(expected, {"output": not cached}, where=f"step {i}")

        observer(ctx)  # re-materialize for the next step


def test_debounce() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    fx = _load("debounce.json")
    ctx: dict = {}
    cell: DebounceCell[str] = DebounceCell(ctx, fx["initial"]["quiet"])

    def drive(step: dict) -> Any:
        op = step["op"]
        if op["type"] == "input":
            cell.input(op["now"], op["value"])
            return None
        return cell.tick(op["now"])

    _run(ctx, fx, cell.output, drive)


def _run_throttle(name: str, edge: ThrottleEdge) -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    fx = _load(name)
    ctx: dict = {}
    cell: ThrottleCell[str] = ThrottleCell(ctx, edge, fx["initial"]["window"])

    def drive(step: dict) -> Any:
        op = step["op"]
        if op["type"] == "input":
            return cell.input(op["now"], op["value"])
        return cell.tick(op["now"])

    _run(ctx, fx, cell.output, drive)


def test_throttle_leading() -> None:
    _run_throttle("throttle_leading.json", ThrottleEdge.LEADING)


def test_throttle_trailing() -> None:
    _run_throttle("throttle_trailing.json", ThrottleEdge.TRAILING)


def test_sample_count() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    fx = _load("sample_count.json")
    ctx: dict = {}
    cell: SampleCell[str] = SampleCell(ctx, SampleMode.Count(fx["initial"]["n"]))

    def drive(step: dict) -> Any:
        return cell.input(step["op"]["value"])

    _run(ctx, fx, cell.output, drive)


def test_sample_time() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    fx = _load("sample_time.json")
    ctx: dict = {}
    cell: SampleCell[str] = SampleCell(ctx, SampleMode.Time(fx["initial"]["period"]))

    def drive(step: dict) -> Any:
        op = step["op"]
        if op["type"] == "input":
            cell.input(op["value"])
            return None
        return cell.tick(op["now"])

    _run(ctx, fx, cell.output, drive)


def test_probabilistic_sample() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    fx = _load("probabilistic_sample.json")
    ctx: dict = {}
    # Draws are injected per step via ``input_with_draw``; the owned RNG is
    # unused here, a deterministic ``Lcg`` satisfies the type.
    cell: ProbabilisticSampleCell[str] = ProbabilisticSampleCell(
        ctx, fx["initial"]["rate"], Lcg(0)
    )

    def drive(step: dict) -> Any:
        op = step["op"]
        return cell.input_with_draw(op["value"], op["draw"])

    _run(ctx, fx, cell.output, drive)


def test_lcg_matches_rust_distribution() -> None:
    """The SplitMix64 ``Lcg`` must reproduce the Rust empirical sampling rate
    (within statistical bounds) — guards the byte-identical constants."""
    ctx: dict = {}
    cell: ProbabilisticSampleCell[int] = ProbabilisticSampleCell(ctx, 0.3, Lcg(42))
    n = 20_000
    passed = sum(1 for i in range(n) if cell.input(i) is not None)
    frac = passed / n
    assert abs(frac - 0.3) < 0.02, f"empirical rate {frac} off target"
