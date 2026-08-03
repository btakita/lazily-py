"""Cross-language conformance tests for the presence + ephemeral plane.

Each test loads a canonical JSON fixture from
``lazily-spec/conformance/presence`` and replays it through the lazily-py
``EphemeralCell`` / ``PresenceCell`` / ``AwarenessCell``, asserting the spec's
language-agnostic expectations. These are **compute** fixtures: the harness
builds the cell from ``config``/``initial``, replays each ``step``'s ``op``, and
asserts the resulting projected view (ephemeral value, or live ``peer -> value``
map) plus exactly which reader invalidates.

The same fixtures are replayed by the Rust binding (and the other bindings), so
all implementations stay byte-compatible on the compute invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conformance_assert import (
    assert_invalidates,
    assert_key,
    instrument,
)

from lazily import Slot
from lazily.presence import AwarenessCell, EphemeralCell, PresenceCell


_SPEC = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "presence"


def _load(rel: str) -> dict:
    return instrument(json.loads((_SPEC / rel).read_text()), name=f"presence/{rel}")


def _spec_present() -> bool:
    return (_SPEC / "presence.json").exists()


def _observer(ctx: dict, reader: Any) -> Slot:  # type: ignore[type-arg]
    """A cached slot over ``reader`` whose ``is_in(ctx)`` reports whether the
    cached value survived the last op (cached ⇒ not invalidated)."""
    s: Slot = Slot(callable=lambda _ctx: reader(_ctx))
    s(ctx)  # materialize the cache
    return s


def _assert_invalidation(
    ctx: dict, observer: Slot, expected: dict, reader_name: str, step: int
) -> None:
    cached = observer.is_in(ctx)
    assert_invalidates(expected, {reader_name: not cached}, where=f"step {step}")
    observer(ctx)  # re-materialize for the next step


def _present_map(cell: Any) -> dict[str, str]:
    """The cell's presence map keyed the way the corpus spells it.

    Normalising the OBSERVED side and comparing whole objects, rather than
    rebuilding the fixture side inside a predicate: whole-object equality is a
    key-set check the tracker can see, and a peer the corpus adds then fails
    here instead of being compared by nothing (#lzsubblockkeyset).
    """
    return {str(peer): value for peer, value in cell.present().items()}


def _run_presence(fixture: dict) -> None:
    ctx: dict = {}
    ttl = fixture["config"]["ttl"]
    cell: PresenceCell[int, str] = PresenceCell(ctx, ttl)
    observer = _observer(ctx, cell.present)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        now = op["now"]
        op_type = op["type"]
        if op_type == "heartbeat":
            cell.heartbeat(op["peer"], op["value"], now)
        elif op_type == "evict":
            cell.evict(op["peer"], now)
        elif op_type == "tick":
            cell.tick(now)
        else:
            raise AssertionError(f"unknown presence op: {op_type}")

        expected = step["expected"]
        assert_key(
            expected,
            "present",
            _present_map(cell),
            where=f"step {i} after {op}",
        )
        _assert_invalidation(ctx, observer, expected, "present", i)


def _run_awareness(fixture: dict) -> None:
    ctx: dict = {}
    ttl = fixture["config"]["ttl"]
    cell: AwarenessCell[int, str] = AwarenessCell(ctx, ttl)
    observer = _observer(ctx, cell.present)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        now = op["now"]
        op_type = op["type"]
        if op_type == "set":
            cell.set(op["peer"], op["value"], now)
        elif op_type == "tick":
            cell.tick(now)
        else:
            raise AssertionError(f"unknown awareness op: {op_type}")

        expected = step["expected"]
        assert_key(
            expected,
            "present",
            _present_map(cell),
            where=f"step {i} after {op}",
        )
        _assert_invalidation(ctx, observer, expected, "present", i)


def _run_ephemeral(fixture: dict) -> None:
    ctx: dict = {}
    cell: EphemeralCell[str] = EphemeralCell(ctx)
    observer = _observer(ctx, cell.value)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        now = op["now"]
        op_type = op["type"]
        if op_type == "set":
            cell.set(op["value"], now, op["ttl"])
        elif op_type == "tick":
            cell.tick(now)
        else:
            raise AssertionError(f"unknown ephemeral op: {op_type}")

        expected = step["expected"]
        assert_key(expected, "value", cell.value(), where=f"step {i} after {op}")
        _assert_invalidation(ctx, observer, expected, "value", i)


def test_presence_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")
    _run_presence(_load("presence.json"))
    _run_awareness(_load("awareness.json"))
    _run_ephemeral(_load("ephemeral.json"))
