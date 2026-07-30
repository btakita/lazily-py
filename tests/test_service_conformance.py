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

from conformance_assert import instrument

from lazily import Slot
from lazily.service import (
    DiscoveryCell,
    Health,
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


def _assert_inval(ctx: dict, observer: Slot, invalidates: dict, reader: str) -> None:
    if reader not in invalidates:
        return
    cached = observer.is_in(ctx)
    if invalidates[reader]:
        assert not cached, (
            f"reader `{reader}` should have been invalidated but stayed cached"
        )
    else:
        assert cached, (
            f"reader `{reader}` should have stayed cached but was invalidated"
        )


def _run_health(fixture: dict) -> None:
    ctx: dict = {}
    cell = HealthCell(ctx)
    observer = _observer(ctx, cell.health)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        cell.set(op["name"], op["up"], op["critical"])

        expected = step["expected"]
        want = Health(expected["health"])
        assert cell.health() is want, f"step {i}: health {cell.health()} want {want}"

        _assert_inval(ctx, observer, expected.get("invalidates", {}), "health")
        observer(ctx)  # re-materialize


def _run_readiness(fixture: dict) -> None:
    ctx: dict = {}
    cell = ReadinessCell(ctx)
    observer = _observer(ctx, cell.ready)

    for i, step in enumerate(fixture["steps"]):
        op = step["op"]
        cell.set(op["name"], op["ready"])

        expected = step["expected"]
        assert cell.ready() == expected["ready"], f"step {i}: ready mismatch"

        _assert_inval(ctx, observer, expected.get("invalidates", {}), "ready")
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
        assert cell.discovery() == expected["discovery"], f"step {i}: map mismatch"

        _assert_inval(ctx, observer, expected.get("invalidates", {}), "discovery")
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
        assert reg.projection() == expected["projection"], (
            f"step {i}: projection mismatch"
        )

        _assert_inval(ctx, observer, expected.get("invalidates", {}), "projection")
        observer(ctx)


def test_service_conformance() -> None:
    if not _spec_present():
        import pytest

        pytest.skip("lazily-spec conformance fixtures not found")

    _run_health(_load("health.json"))
    _run_readiness(_load("readiness.json"))
    _run_discovery(_load("discovery.json"))
    _run_service_registry(_load("service_registry.json"))
