"""Keyed reactive collections — ``SourceMap`` / ``ComputedMap`` independence laws
(``#reactivemap``).

The Python counterpart of the Lean ``LazilyFormal.Collection`` formal model in
``lazily-formal``. Each test mirrors a named theorem (the three independent
reactive signals + atomic-move identity preservation + per-key mint identity).
"""

from __future__ import annotations

import warnings

import pytest

from lazily import ComputedMap, EntryKind, Slot, SourceMap


# =================================================================================
# setEntryValue_preserves_{membership,order,siblings}
# Updating one entry's value touches neither the membership nor the order
# signal, nor any sibling entry's value cell.
# =================================================================================


def test_set_entry_value_preserves_membership_and_order() -> None:
    ctx: dict = {}
    cm = SourceMap[str, int](ctx)
    cm.entry("a", 1)
    cm.entry("b", 2)
    m_before = cm.membership_signal.value
    o_before = cm.order_signal.value

    cm.set("a", 99)

    assert cm.membership_signal.value == m_before  # membership unchanged
    assert cm.order_signal.value == o_before  # order unchanged
    assert cm.get("a") == 99


def test_set_entry_value_preserves_siblings() -> None:
    ctx: dict = {}
    cm = SourceMap[str, int](ctx)
    cm.entry("a", 1)
    cm.entry("b", 2)
    cm.set("a", 99)
    assert cm.get("b") == 2  # sibling untouched


# =================================================================================
# moveKey_preserves_{membership,values} / moveKey_advances_order
# A pure reorder leaves membership and every value cell untouched, bumping only
# the order signal — "a pure reorder MUST NOT invalidate set-membership readers".
# =================================================================================


def test_move_to_preserves_membership_and_values() -> None:
    ctx: dict = {}
    cm = SourceMap[str, int](ctx)
    cm.entry("a", 1)
    cm.entry("b", 2)
    cm.entry("c", 3)
    m_before = cm.membership_signal.value

    cm.move_to("a", 2)  # [a,b,c] -> [b,c,a]

    assert cm.membership_signal.value == m_before  # membership unchanged
    assert cm.get("a") == 1  # value cell identity preserved (not re-minted)
    assert cm.get("b") == 2
    assert cm.get("c") == 3
    assert cm.keys() == ["b", "c", "a"]


def test_move_to_advances_order_signal_only() -> None:
    ctx: dict = {}
    cm = SourceMap[str, int](ctx)
    cm.entry("a", 1)
    cm.entry("b", 2)
    m_before = cm.membership_signal.value
    o_before = cm.order_signal.value

    cm.move_to("a", 1)

    assert cm.membership_signal.value == m_before  # unchanged
    assert cm.order_signal.value == o_before + 1  # advanced exactly once


def test_move_before_and_move_after() -> None:
    ctx: dict = {}
    cm = SourceMap[str, int](ctx)
    for k, v in [("a", 1), ("b", 2), ("c", 3), ("d", 4)]:
        cm.entry(k, v)
    cm.move_before("d", "b")  # [a,b,c,d] -> [a,d,b,c]
    assert cm.keys() == ["a", "d", "b", "c"]
    cm.move_after("a", "c")  # [a,d,b,c] -> [d,b,c,a]
    assert cm.keys() == ["d", "b", "c", "a"]


# =================================================================================
# addKey_advances_membership_and_order / removeKey
# =================================================================================


def test_add_key_advances_membership_and_order() -> None:
    ctx: dict = {}
    cm = SourceMap[str, int](ctx)
    cm.entry("a", 1)
    m0, o0 = cm.membership_signal.value, cm.order_signal.value
    cm.entry("b", 2)
    assert cm.membership_signal.value == m0 + 1
    assert cm.order_signal.value == o0 + 1
    # Idempotent: re-`entry`-ing a member is a no-op (default ignored).
    cm.entry("a", 99)
    assert cm.get("a") == 1  # unchanged — entry of existing member is a no-op


def test_remove_key_advances_signals() -> None:
    ctx: dict = {}
    cm = SourceMap[str, int](ctx)
    cm.entry("a", 1)
    cm.entry("b", 2)
    m0, o0 = cm.membership_signal.value, cm.order_signal.value
    assert cm.remove("a")
    assert "a" not in cm
    assert cm.membership_signal.value == m0 + 1
    assert cm.order_signal.value == o0 + 1
    assert cm.remove("a") is False  # no-op on absent key


# =================================================================================
# Reactive independence — a Slot reading `len` is NOT invalidated by a move.
# =================================================================================


def test_len_reader_not_invalidated_by_move() -> None:
    ctx: dict = {}
    cm = SourceMap[str, int](ctx)
    cm.entry("a", 1)
    cm.entry("b", 2)

    @Slot
    def length(_ctx: dict) -> int:
        return cm.len(_ctx)

    runs = [0]

    @Slot
    def watch(_ctx: dict) -> int:
        v = length(_ctx)
        runs[0] += 1
        return v

    assert watch(ctx) == 2
    assert runs[0] == 1
    cm.move_to("a", 1)  # pure reorder — membership unchanged
    assert watch(ctx) == 2
    assert runs[0] == 1  # len reader NOT invalidated by the move
    cm.entry("c", 3)  # membership change
    assert watch(ctx) == 3
    assert runs[0] == 2  # invalidated by the add


# =================================================================================
# get_or_insert_with / entry — per-key mint identity stability.
# =================================================================================


def test_source_map_entry_idempotent_after_first() -> None:
    ctx: dict = {}
    cm = SourceMap[str, int](ctx)
    c1 = cm.entry("x", 1)
    c2 = cm.entry("x", 1)  # second request -> same cell
    assert c1 is c2
    assert cm.is_present("x")
    c3 = cm.entry("y", 2)
    assert c3 is not c1


def test_computed_map_get_or_insert_with_mints_once() -> None:
    ctx: dict = {}
    sm = ComputedMap[str, int](ctx)
    calls = [0]

    def factory(_c: object, k: str) -> int:
        calls[0] += 1
        return len(k)

    assert sm.get_or_insert_with("abc", factory) == 3
    assert sm.get_or_insert_with("abc", factory) == 3  # cached: factory not re-run
    assert calls[0] == 1
    assert sm.handle("abc") is sm.handle("abc")  # identity-stable handle


# =================================================================================
# EntryKind — v2 ``Source`` / ``Computed`` member rename.
# The member NAMES moved; the member VALUES are wire data nine binding runners
# read out of the shared conformance fixtures, so they MUST NOT move.
# =================================================================================


def test_entry_kind_values_are_unchanged_wire_data() -> None:
    # The rename is name-only: these value strings are the fixture wire format.
    assert EntryKind.SOURCE.value == "cell"
    assert EntryKind.COMPUTED.value == "slot"
    # Value lookup — how a runner turns a fixture's `kind` field into a member.
    assert EntryKind("cell") is EntryKind.SOURCE
    assert EntryKind("slot") is EntryKind.COMPUTED
    assert {k.value for k in EntryKind} == {"cell", "slot"}


def test_deprecated_entry_kind_names_still_resolve() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert EntryKind.CELL is EntryKind.SOURCE
        assert EntryKind.SLOT is EntryKind.COMPUTED
    assert len(caught) == 2
    assert all(issubclass(w.category, DeprecationWarning) for w in caught)
    messages = [str(w.message) for w in caught]
    assert any("EntryKind.CELL is deprecated" in m for m in messages)
    assert any("EntryKind.SLOT is deprecated" in m for m in messages)
    assert any("use EntryKind.SOURCE instead" in m for m in messages)
    assert any("use EntryKind.COMPUTED instead" in m for m in messages)


def test_deprecated_names_stay_out_of_the_canonical_member_set() -> None:
    # Aliases are served on demand: iteration and ``__members__`` show the
    # current spelling only, so a binding cannot round-trip the old name.
    assert [k.name for k in EntryKind] == ["SOURCE", "COMPUTED"]
    assert list(EntryKind.__members__) == ["SOURCE", "COMPUTED"]


def test_unknown_entry_kind_member_still_raises() -> None:
    with pytest.raises(AttributeError):
        EntryKind.NOPE  # type: ignore[attr-defined]  # noqa: B018
