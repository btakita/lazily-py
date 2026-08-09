"""Cross-language conformance tests for the temporal sources (``#lztime``).

Each test loads a canonical JSON fixture from
``lazily-spec/conformance/temporal`` and replays it through the lazily-py
temporal cells, asserting the spec's language-agnostic expectations. These are
**compute** fixtures: the harness loads the ``initial`` state, replays each
``step``'s ``tick(now)`` op, and asserts the fire edge (``returns``), the
projected reader values, and — the core of the spec — that the primary reader
invalidates exactly on the fire edge.

Invalidation is observed by wrapping the reader in an observer :class:`Slot` and
checking whether its cached value survives the tick (``is_in`` ⇒ still cached).

The same fixtures are replayed by the Rust, Zig, Kotlin, Go, C++, and JS
bindings, so all implementations stay byte-compatible on the compute invariants.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from conformance_assert import (
    assert_invalidates,
    assert_key,
    corpus_subdir,
    instrument,
)

from lazily import Slot
from lazily.temporal import CronCell, DeadlineCell, IntervalCell, TimerCell


if TYPE_CHECKING:
    from collections.abc import Callable


_SPEC = corpus_subdir("temporal")


def _load(rel: str) -> dict:
    return instrument(json.loads((_SPEC / rel).read_text()), name=f"temporal/{rel}")


def _spec_present() -> bool:
    return (_SPEC / "timer_single_shot.json").exists()


def _observer(ctx: dict, reader: Callable[[], Any]) -> Slot:  # type: ignore[type-arg]
    """A primed observer Slot over ``reader``; ``is_in(ctx)`` reports whether the
    cached value survived the last op (cached ⇒ not invalidated)."""
    s: Slot = Slot(callable=lambda _ctx: reader(_ctx))
    s(ctx)  # materialize the cache
    return s


def _assert_inval(ctx: dict, observer: Slot, exp: dict, reader: str, step: int) -> None:  # type: ignore[type-arg]
    cached = observer.is_in(ctx)
    assert_invalidates(exp, {reader: not cached}, where=f"step {step}")
    observer(ctx)  # re-materialize for the next step


def _unit(value: Any) -> Any:
    """The corpus spells the timer's unit payload as the string ``"()"``.

    Mapping the *real* value into the fixture's spelling keeps the fixture value
    on the asserted side of the comparison. Reading the fixture to pick which
    literal to expect (``() if exp["value"] == "()" else None``) would let any
    other spelling silently expect ``None`` (#lzconsumednotasserted).
    """
    return "()" if value == () else value


def _run_timer(fx: dict) -> None:
    ctx: dict = {}
    timer = TimerCell(ctx, fx["initial"]["fire_at"])
    observed = _observer(ctx, timer.has_fired)

    for i, step in enumerate(fx["steps"]):
        edge = timer.tick(step["op"]["now"])
        assert edge == step["returns"], f"timer step {i}: fire edge"

        exp = step["expected"]
        assert_key(exp, "fired", timer.has_fired(), where=f"timer step {i}")
        assert_key(exp, "value", _unit(timer.value()), where=f"timer step {i}")
        assert_key(exp, "next_fire", timer.next_fire(), where=f"timer step {i}")

        _assert_inval(ctx, observed, exp, "fired", i)


def _run_interval(fx: dict) -> None:
    ctx: dict = {}
    iv = IntervalCell(ctx, fx["initial"]["period"])
    observed = _observer(ctx, iv.count)

    for i, step in enumerate(fx["steps"]):
        edge = iv.tick(step["op"]["now"])
        assert edge == step["returns"], f"interval step {i}: fire edge"

        exp = step["expected"]
        assert_key(exp, "count", iv.count(), where=f"interval step {i}")
        assert_key(exp, "next_fire", iv.next_fire(), where=f"interval step {i}")

        _assert_inval(ctx, observed, exp, "count", i)


def _run_cron(fx: dict) -> None:
    ctx: dict = {}
    cron = CronCell(ctx, fx["initial"]["cycle"], fx["initial"]["offsets"])
    observed = _observer(ctx, cron.count)

    for i, step in enumerate(fx["steps"]):
        edge = cron.tick(step["op"]["now"])
        assert edge == step["returns"], f"cron step {i}: fire edge"

        exp = step["expected"]
        assert_key(exp, "count", cron.count(), where=f"cron step {i}")
        assert_key(exp, "next_fire", cron.next_fire(), where=f"cron step {i}")

        _assert_inval(ctx, observed, exp, "count", i)


def _run_deadline(fx: dict) -> None:
    ctx: dict = {}
    value = fx["initial"]["value"]
    d: DeadlineCell[str] = DeadlineCell(ctx, value, fx["initial"]["deadline"])
    observed = _observer(ctx, d.state)

    for i, step in enumerate(fx["steps"]):
        edge = d.tick(step["op"]["now"])
        assert edge == step["returns"], f"deadline step {i}: expiry edge"

        exp = step["expected"]
        state = d.state()
        want_state = assert_key(
            exp, "state", state.state.value, where=f"deadline step {i}"
        )
        assert_key(exp, "value", state.value, where=f"deadline step {i}")
        assert state.is_expired() == (want_state == "Expired")

        _assert_inval(ctx, observed, exp, "state", i)


def test_temporal_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")

    _run_timer(_load("timer_single_shot.json"))
    _run_interval(_load("interval_periodic.json"))
    _run_cron(_load("cron_pattern.json"))
    _run_deadline(_load("deadline_expiry.json"))
