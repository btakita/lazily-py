"""Cross-language conformance tests for the embedded-service plane
(``#lzservice``).

Each test loads a canonical JSON fixture from
``lazily-spec/conformance/service`` and replays it through the lazily-py
:mod:`lazily.service` cells, asserting the spec's language-agnostic
expectations. These are **compute** fixtures: the harness replays each ``step``'s
``op`` and asserts the ``expected`` projected value (health status enum / ready
bool / discovery map / registry projection) and — the core of the spec —
exactly which reader invalidates.

The same fixtures are replayed by the Rust binding (see
``lazily-rs/tests/service_conformance.rs``), so both implementations stay
compatible on the compute invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conformance_assert import assert_invalidates, assert_key, instrument

from lazily import Slot
from lazily.service import (
    DiscoveryCell,
    HealthCell,
    ReadinessCell,
    ServiceRegistry,
)


_SPEC = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "service"


def _load(rel: str) -> dict:
    return instrument(json.loads((_SPEC / rel).read_text()), name=f"service/{rel}")


def _spec_present() -> bool:
    return (_SPEC / "health.json").exists()


def _observer(ctx: dict, read: Any) -> Slot:
    """A cached observer Slot over a reactive reader. ``is_in(ctx)`` reports
    whether the cache survived the last op (cached ⇒ not invalidated)."""
    s: Slot = Slot(callable=lambda _ctx: read(_ctx))
    s(ctx)  # materialize the cache
    return s


def _assert_inval(
    ctx: dict, observer: Slot, expected: dict, reader: str, step: int
) -> None:
    cached = observer.is_in(ctx)
    assert_invalidates(expected, {reader: not cached}, where=f"step {step}")


def _run_health(fixture: dict) -> None:
    ctx: dict = {}
    cell = HealthCell(ctx)
    observer = _observer(ctx, cell.health)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        cell.set(op["name"], op["up"], op["critical"])

        expected = step["expected"]
        assert_key(expected, "health", cell.health().value, where=f"step {i}")

        _assert_inval(ctx, observer, expected, "health", i)
        observer(ctx)  # re-materialize


def _run_readiness(fixture: dict) -> None:
    ctx: dict = {}
    cell = ReadinessCell(ctx)
    observer = _observer(ctx, cell.ready)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        cell.set(op["name"], op["ready"])

        expected = step["expected"]
        assert_key(expected, "ready", cell.ready(), where=f"step {i}")

        _assert_inval(ctx, observer, expected, "ready", i)
        observer(ctx)


def _run_discovery(fixture: dict) -> None:
    ctx: dict = {}
    cell = DiscoveryCell(ctx)
    observer = _observer(ctx, cell.discovery)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        op_type = op["type"]
        if op_type == "register":
            cell.register(op["service"], op["endpoint"], op["peer"])
        elif op_type == "deregister":
            cell.deregister(op["service"])
        elif op_type == "evict":
            cell.evict(op["peer"])
        elif op_type == "resolve":
            got = cell.resolve(op["service"])
            assert got == step.get("returns"), (
                f"step {i}: resolve {got!r} want {step.get('returns')!r}"
            )
        else:
            raise AssertionError(f"unknown discovery op type: {op_type}")

        expected = step["expected"]
        assert_key(expected, "discovery", cell.discovery(), where=f"step {i}")

        _assert_inval(ctx, observer, expected, "discovery", i)
        observer(ctx)


def _run_service_registry(fixture: dict) -> None:
    ctx: dict = {}
    reg = ServiceRegistry(ctx)
    observer = _observer(ctx, reg.projection)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        op_type = op["type"]
        if op_type == "register":
            reg.register(op["service"], op["endpoint"])
        elif op_type == "deregister":
            reg.deregister(op["service"])
        elif op_type == "replay":
            reg.replay()
        else:
            raise AssertionError(f"unknown registry op type: {op_type}")

        expected = step["expected"]
        assert_key(expected, "projection", reg.projection(), where=f"step {i}")

        _assert_inval(ctx, observer, expected, "projection", i)
        observer(ctx)


def test_service_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")

    _run_health(_load("health.json"))
    _run_readiness(_load("readiness.json"))
    _run_discovery(_load("discovery.json"))
    _run_service_registry(_load("service_registry.json"))
