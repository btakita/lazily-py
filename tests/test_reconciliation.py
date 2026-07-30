"""Keyed reconciliation — LIS move-minimized ``{insert, remove, move, update}``.

The Python counterpart of the Lean ``LazilyFormal.Reconciliation`` formal model
in ``lazily-formal``. These tests mirror the named theorems and replay the
canonical ``lazily-spec/conformance/collections/keyed_reconciliation_lis.json``
fixture (the binding's conformance obligation for the keyed-collection layer).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from conformance_assert import instrument

from lazily import Level, ReconcileOp, reconcile_ops
from lazily.reconciliation import common_keys, idx_in, lis_by, moved_keys, stable_keys


_SPEC_FIXTURES = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "collections"
)


# =================================================================================
# lisBy_longest / reconcile_move_minimized / reconcile_stable_not_invalidated
# (Reconciliation.lean) — conformance clauses 1 & 2.
# =================================================================================


def test_lis_by_is_longest() -> None:
    # prior indices for [b, c, a] when prior order is [a, b, c, d]:
    # b -> 1, c -> 2, a -> 0. LIS over [1,2,0] is [1,2] (b,c).
    keys = ["b", "c", "a"]
    p = {"a": 0, "b": 1, "c": 2, "d": 3}
    lis = lis_by(p, keys)
    assert lis == ["b", "c"]


def test_lis_by_strictly_increasing_and_subset() -> None:
    p = {"a": 3, "b": 1, "c": 2, "d": 0, "e": 4}
    keys = ["a", "b", "c", "d", "e"]
    lis = lis_by(p, keys)
    # LIS of [3,1,2,0,4] is [1,2,4] -> [b,c,e]
    assert lis == ["b", "c", "e"]
    assert all(k in keys for k in lis)
    # strictly increasing by p
    assert all(p[lis[i]] < p[lis[i + 1]] for i in range(len(lis) - 1))


def test_reconcile_move_minimized() -> None:
    """reconcile_move_minimized: a stable (LIS) key is never moved."""
    prior = Level(order=["a", "b", "c", "d"], values={"a": 1, "b": 2, "c": 3, "d": 4})
    target = Level(order=["b", "c", "a"], values={"a": 1, "b": 2, "c": 3})
    ops = reconcile_ops(prior, target)
    stable = set(stable_keys(prior.order, target.order))
    assert stable == {"b", "c"}
    for op in ops:
        if op.kind == "move":
            assert op.key not in stable, f"stable key {op.key} must not move"


def test_reconcile_stable_not_invalidated() -> None:
    """reconcile_stable_not_invalidated: a stable entry (unchanged value, in the
    LIS) is neither moved nor updated."""
    prior = Level(order=["a", "b", "c", "d"], values={"a": 1, "b": 2, "c": 3, "d": 4})
    target = Level(order=["b", "c", "a"], values={"a": 1, "b": 2, "c": 3})
    ops = reconcile_ops(prior, target)
    for op in ops:
        if op.key in ("b", "c"):  # stable, unchanged value
            assert op.kind not in ("move", "update"), (
                f"stable {op.key} invalidated: {op}"
            )


def test_reconcile_emits_update_on_value_change() -> None:
    prior = Level(order=["a", "b"], values={"a": 1, "b": 2})
    target = Level(order=["a", "b"], values={"a": 1, "b": 99})
    ops = reconcile_ops(prior, target)
    assert ops == [ReconcileOp(kind="update", key="b", value=99)]


def test_reconcile_emits_insert_and_remove() -> None:
    prior = Level(order=["a", "b"], values={"a": 1, "b": 2})
    target = Level(order=["b", "c"], values={"b": 2, "c": 3})
    ops = reconcile_ops(prior, target)
    kinds = {(op.kind, op.key): op.value for op in ops}
    assert ("remove", "a") in kinds
    assert ("insert", "c") in kinds and kinds[("insert", "c")] == 3
    # b is stable + unchanged -> no op
    assert not any(op.key == "b" for op in ops)


def test_helpers() -> None:
    prior = ["a", "b", "c", "d"]
    target = ["b", "c", "a"]
    assert idx_in(prior, "a") == 0
    assert idx_in(prior, "z") == 4  # len(order) if absent
    assert common_keys(prior, target) == ["b", "c", "a"]
    assert stable_keys(prior, target) == ["b", "c"]
    assert moved_keys(prior, target) == ["a"]


# =================================================================================
# Conformance fixture replay: keyed_reconciliation_lis.json
# prior [a,b,c,d] -> target [b,c,a] emits only {remove d, move a after c}.
# =================================================================================


@pytest.mark.skipif(
    not _SPEC_FIXTURES.exists() and importlib.util.find_spec("jsonschema") is None,
    reason="no spec fixtures and no jsonschema",
)
def test_keyed_reconciliation_lis_fixture() -> None:
    fixture_path = _SPEC_FIXTURES / "keyed_reconciliation_lis.json"
    if not fixture_path.exists():
        pytest.skip("lazily-spec collection fixtures not co-located")
    raw = instrument(
        json.loads(fixture_path.read_text()),
        name="collections/keyed_reconciliation_lis.json",
    )
    recon = raw["reconcile"]
    prior = Level(order=recon["prior"]["order"], values=recon["prior"]["values"])
    target = Level(order=recon["target"]["order"], values=recon["target"]["values"])
    expected = raw["expected"]

    ops = reconcile_ops(prior, target)
    op_keys = {(op.kind, op.key) for op in ops}
    # Expected: remove d, move a (after c).
    assert ("remove", "d") in op_keys
    move_a = next((op for op in ops if op.kind == "move" and op.key == "a"), None)
    assert move_a is not None
    assert move_a.after == "c"
    # b and c are stable keys, must NOT be invalidated (no move/update).
    assert ("move", "b") not in op_keys and ("move", "c") not in op_keys
    assert ("update", "b") not in op_keys and ("update", "c") not in op_keys
    assert expected["stable_keys_not_invalidated"] == ["b", "c"]

    # `ops` is the MINIMAL op set, so the emitted ops must match it exactly —
    # spot-checking membership above would pass just as happily for a
    # reconciler that also moved b and c (#lzassertunknownkeys).
    emitted = [
        {"type": op.kind, "key": op.key}
        if op.after is None
        else {"type": op.kind, "key": op.key, "after": op.after}
        for op in ops
    ]
    assert emitted == expected["ops"]

    # And applying that op set to the prior order yields the target order.
    order = list(prior.order)
    for op in ops:
        if op.kind == "remove":
            order.remove(op.key)
        elif op.kind == "move":
            order.remove(op.key)
            order.insert(order.index(op.after) + 1 if op.after else 0, op.key)
    assert order == expected["result_order"]
