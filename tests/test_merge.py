"""Phase 1 law-tests for the merge algebra (#relaycell).

Every policy MUST be associative; commutativity/idempotency are asserted per
flag. Replays the cross-language ``mergecell_algebra.json`` fixture — lazily-py
converges identically to lazily-rs / lazily-js.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance_assert import instrument

from lazily import (
    KeepLatest,
    Max,
    MergeCell,
    RawFifo,
    SetUnion,
    Sum,
    effect,
    merge_cell,
)


_SPEC_COLLECTIONS = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "collections"
)

_POLICIES = [KeepLatest, Sum, Max, SetUnion, RawFifo]


def _sample(policy):
    if policy.name == "SetUnion":
        return [{1, 2}, {2, 3}, {3, 4}]
    if policy.name == "RawFifo":
        return [[1], [2], [3]]
    return [5, -3, 8]


def test_every_policy_is_associative() -> None:
    for p in _POLICIES:
        a, b, c = _sample(p)
        assert p.merge(p.merge(a, b), c) == p.merge(a, p.merge(b, c)), p.name


def test_commutativity_matches_flag() -> None:
    for p in _POLICIES:
        a, b, c = _sample(p)
        if p.commutative:
            assert p.merge(p.merge(a, b), c) == p.merge(p.merge(a, c), b), p.name
    # Honesty: cleared flags exhibit a counterexample.
    assert KeepLatest.merge(KeepLatest.merge(0, 1), 2) != KeepLatest.merge(
        KeepLatest.merge(0, 2), 1
    )
    assert RawFifo.merge([1], [2]) != RawFifo.merge([2], [1])


def test_idempotency_matches_flag() -> None:
    for p in _POLICIES:
        a, b = _sample(p)[:2]
        if p.idempotent:
            assert p.merge(p.merge(a, b), b) == p.merge(a, b), p.name
    assert Sum.merge(Sum.merge(0, 5), 5) != Sum.merge(0, 5)
    assert RawFifo.merge(RawFifo.merge([], [1]), [1]) != RawFifo.merge([], [1])


def test_cell_is_merge_cell_keep_latest() -> None:
    from lazily import Cell

    ctx: dict = {}
    cell = Cell(ctx, 0)
    mc = merge_cell(ctx, 0, KeepLatest)
    for v in (3, 3, 7, 7, 1):
        cell.set(v)
        mc.merge(v)
        assert cell.get() == mc.get()
    assert mc.get() == 1


def test_sum_converges_regardless_of_order() -> None:
    ctx: dict = {}
    ops = [5, -3, 8, 2, -1]
    a = merge_cell(ctx, 0, Sum)
    for d in ops:
        a.merge(d)
    b = merge_cell(ctx, 0, Sum)
    for d in reversed(ops):
        b.merge(d)
    assert a.get() == b.get() == 11


def test_idempotent_merge_no_ops_via_guard() -> None:
    ctx: dict = {}
    mc = merge_cell(ctx, 10, Max)
    runs = [0]

    @effect
    def watch(ctx) -> None:
        mc.get(ctx)
        runs[0] += 1

    watch(ctx)
    assert runs[0] == 1
    mc.merge(5)
    mc.merge(10)
    mc.merge(0)
    assert runs[0] == 1  # merges at/below the max fire no cascade
    mc.merge(42)
    assert mc.get() == 42
    assert runs[0] == 2


def test_merge_cell_is_distinct_type() -> None:
    ctx: dict = {}
    assert isinstance(merge_cell(ctx, 0, Sum), MergeCell)


@pytest.mark.skipif(
    not (_SPEC_COLLECTIONS / "mergecell_algebra.json").exists(),
    reason="lazily-spec fixture not present as sibling",
)
def test_mergecell_algebra_fixture() -> None:
    fixture = instrument(
        json.loads((_SPEC_COLLECTIONS / "mergecell_algebra.json").read_text()),
        name="collections/mergecell_algebra.json",
    )
    by_name = {"KeepLatest": KeepLatest, "Sum": Sum, "Max": Max}
    seen = 0
    for scenario in fixture["scenarios"]:
        policy = by_name[scenario["policy"]]
        assert policy.commutative == scenario["flags"]["commutative"]
        assert policy.idempotent == scenario["flags"]["idempotent"]

        ctx: dict = {}
        mc = merge_cell(ctx, scenario["initial"], policy)
        runs = [0]

        @effect
        def watch(ctx, _mc=mc, _runs=runs) -> None:
            _mc.get(ctx)
            _runs[0] += 1

        watch(ctx)
        for step in scenario["steps"]:
            before = runs[0]
            mc.merge(step["merge"])
            fired = runs[0] > before
            assert mc.get() == step["expected"]["value"], policy.name
            assert fired == step["expected"]["invalidates"], policy.name
        seen += 1
    assert seen == 3
