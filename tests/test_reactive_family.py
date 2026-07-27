"""``ComputedMap`` / ``SourceMap`` materialization tests (``#reactivemap``).

Mirrors the ``lazily-rs`` ``cell_family.rs`` unit tests and the
``materialization_conformance.rs`` harness, driven by the canonical fixtures in
``lazily-spec/conformance/materialization/`` (now ``"model": "ComputedMap"``).
Exercises the laws proved in ``lazily-formal``'s ``Materialization`` module
against the Python :class:`~lazily.ComputedMap` specialization of
:class:`~lazily.ReactiveMap`: observational transparency (eager pre-mint vs lazy
mint-on-access), deferral-not-deallocation present-set monotonicity, and
entry-kind orthogonal to strategy (input cells always materialized / derived
slots deferred under lazy). There is no eager/lazy mode flag — eager is a
pre-mint loop (``materialize_all``), lazy is mint-on-access
(``get_or_insert_with``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lazily import ComputedMap, DisposedError, EntryKind, SourceMap


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance" / "materialization"
_SPEC_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "lazily-spec"
    / "conformance"
    / "materialization"
)

#: Accepted spellings of the derived-map ``kind`` / ``model`` wire fields.
#: ``SlotMap`` is the DEPRECATED spelling of ``ComputedMap`` — the field is wire
#: data shared by nine independent bindings, so a runner MUST keep accepting the
#: old spelling while the other bindings migrate. Only the new spelling is
#: emitted; the vendored fixtures carry ``ComputedMap``.
_COMPUTED_MAP_MODELS = frozenset({"ComputedMap", "SlotMap"})

#: Accepted spellings of a fixture **entry**'s ``kind`` field -> the
#: :class:`EntryKind` it denotes. ``"cell"`` / ``"slot"`` are the current
#: canonical fixture spelling and the enum's (unchanged) member VALUES;
#: ``"source"`` / ``"computed"`` are the v2 spelling the canonical corpus will
#: flip to. The field is wire data nine independent bindings read, so a runner
#: MUST accept both while the corpus and the other bindings migrate. Anything
#: else is a hard error — never a silent default or skip.
_ENTRY_KIND_SPELLINGS = {
    "cell": EntryKind.SOURCE,
    "source": EntryKind.SOURCE,
    "slot": EntryKind.COMPUTED,
    "computed": EntryKind.COMPUTED,
}


def entry_kind_of(entry: dict) -> EntryKind:
    """The :class:`EntryKind` a fixture entry declares, accepting both the old
    (``cell`` / ``slot``) and new (``source`` / ``computed``) spellings.

    An unrecognized spelling fails the run: a fixture the binding cannot read is
    a conformance failure, not something to default or skip past.
    """
    spelling = entry["kind"]
    kind = _ENTRY_KIND_SPELLINGS.get(spelling)
    if kind is None:
        raise AssertionError(
            f"unknown fixture entry kind {spelling!r}; expected one of "
            f"{sorted(_ENTRY_KIND_SPELLINGS)}"
        )
    return kind


def load_fixture(name: str) -> dict:
    spec_path = _SPEC_FIXTURES / name
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / name
    return json.loads(path.read_text())


def _ctx_factory(fn):
    """Adapt a legacy 1-arg ``factory(k)`` to the ctx-param ``factory(view, k)``
    contract (``#lzcellkernel``); the compute view is unused by a factory that
    reads no reactive node."""
    return lambda _c, k: fn(k)


# ---------------------------------------------------------------------------
# Unit tests (mirror cell_family.rs)
# ---------------------------------------------------------------------------


def test_eager_computed_map_materializes_all_up_front() -> None:
    fam: ComputedMap[int, int] = ComputedMap({})
    fam.materialize_all([0, 1, 2, 5, 9], lambda _c, k: k * 3)
    assert fam.present_count() == 5
    assert all(fam.is_present(k) for k in (0, 1, 2, 5, 9))
    assert fam.entry_kind is EntryKind.COMPUTED


def test_lazy_computed_map_defers_until_read() -> None:
    fam: ComputedMap[int, int] = ComputedMap({})
    assert fam.present_count() == 0
    assert not fam.is_present(5)
    # First read mints just that key ("materialize on pull").
    assert fam.get_or_insert_with(5, lambda _c, k: k * 3) == 15
    assert fam.is_present(5)
    assert fam.present_keys() == [5]


def test_eager_and_lazy_observe_identically() -> None:
    eager: ComputedMap[int, int] = ComputedMap({})
    eager.materialize_all([0, 1, 2, 5, 9], lambda _c, k: k * 3)
    lazy: ComputedMap[int, int] = ComputedMap({})
    for k in (0, 1, 2, 5, 9):
        assert eager.get(k) == lazy.get_or_insert_with(k, lambda _c, k: k * 3)


def test_present_set_is_monotone_across_reads() -> None:
    fam: ComputedMap[int, int] = ComputedMap({})
    sizes = []
    for k in (2, 4, 2, 5):
        fam.get_or_insert_with(k, lambda _c, k: k * 2)
        sizes.append(fam.present_count())
    # Re-reading 2 does not re-materialize; sizes are non-decreasing.
    assert sizes == [1, 2, 2, 3]
    assert fam.present_keys() == [2, 4, 5]


def test_get_or_insert_with_mints_once_then_returns_existing() -> None:
    fam: ComputedMap[str, int] = ComputedMap({})
    calls = [0]

    def factory(_c: object, _k: str) -> int:
        calls[0] += 1
        return 7

    assert fam.get_or_insert_with("a", factory) == 7
    assert fam.present_count() == 1
    # Second access returns the existing value; factory is NOT called again.
    assert fam.get_or_insert_with("a", lambda _c, _k: 999) == 7
    assert calls[0] == 1


def test_source_map_entries_are_writable_inputs() -> None:
    fam: SourceMap[int, int] = SourceMap({})
    handle = fam.entry(7, 7)
    assert handle.get() == 7
    assert fam.entry_kind is EntryKind.SOURCE
    handle.set(100)
    assert fam.get(7) == 100


def test_source_map_set_seeds_and_updates() -> None:
    fam: SourceMap[str, int] = SourceMap({})
    fam.set("a", 1)
    assert fam.present_count() == 1
    fam.set("a", 42)  # existing entry: no membership change
    assert fam.present_count() == 1
    assert fam.get("a") == 42


def test_source_map_remove_disposes_held_handle() -> None:
    fam: SourceMap[str, int] = SourceMap({})
    handle = fam.entry("a", 7)

    assert fam.remove("a")
    assert handle.disposed
    with pytest.raises(DisposedError, match="disposed cell"):
        handle.get()


def test_computed_map_remove_disposes_held_handle() -> None:
    ctx: dict = {}
    fam: ComputedMap[str, int] = ComputedMap(ctx)
    assert fam.get_or_insert_with("a", lambda _c, _k: 7) == 7
    handle = fam.handle("a")
    assert handle is not None

    assert fam.remove("a")
    assert handle.disposed
    with pytest.raises(DisposedError, match="disposed slot"):
        handle(ctx)


def test_observe_is_reactive_when_factory_reads_a_cell() -> None:
    # A derived slot entry whose factory reads a Cell re-derives when the cell
    # changes — reactivity is orthogonal to materialization.
    from lazily import Cell, Slot

    ctx: dict = {}
    src = Cell(ctx, 10)
    fam: ComputedMap[int, int] = ComputedMap(ctx)
    fam.materialize_all([1], lambda c, k: c.read(src) + k)
    seen = []

    reader = Slot(lambda c: fam.get(1, c))
    watcher = Slot(lambda c: seen.append(reader(c)))
    watcher(ctx)
    assert seen == [11]
    src.set(100)
    watcher(ctx)
    assert seen == [11, 101]


# ---------------------------------------------------------------------------
# Conformance fixtures (mirror materialization_conformance.rs)
# ---------------------------------------------------------------------------


def _val_lookup(spec_val: dict) -> dict[str, int]:
    return {k: int(v) for k, v in spec_val.items()}


def _check_val_fixture(name: str) -> dict:
    fixture = load_fixture(name)
    assert fixture["kind"] in _COMPUTED_MAP_MODELS
    expected = fixture["expected"]
    # default_mode_eager: eager is the default materialization strategy.
    assert expected["default_mode"] == "eager"

    vals = _val_lookup(fixture["spec"]["val"])
    keys = list(vals.keys())
    lookup = _ctx_factory(vals.__getitem__)

    # eager: pre-mint the whole keyset; lazy: empty, mint-on-access.
    eager: ComputedMap[str, int] = ComputedMap({})
    eager.materialize_all(keys, lookup)
    lazy: ComputedMap[str, int] = ComputedMap({})

    # eager_materializes_all
    assert eager.present_count() == len(keys)
    assert set(eager.present_keys()) == set(expected["eager_present"])
    # lazy defers every derived slot: nothing present at build.
    assert lazy.present_count() == 0

    # observe_canonical / eager_lazy_observationally_equivalent
    for k, want in expected["observe"].items():
        assert eager.get(k) == want
        assert lazy.get_or_insert_with(k, lookup) == want

    return fixture


def test_conformance_observational_transparency() -> None:
    fixture = _check_val_fixture("observational_transparency.json")
    expected = fixture["expected"]

    vals = _val_lookup(fixture["spec"]["val"])
    lazy: ComputedMap[str, int] = ComputedMap({})
    for k in fixture["reads"]:
        lazy.get_or_insert_with(k, _ctx_factory(vals.__getitem__))
    assert set(lazy.present_keys()) == set(expected["lazy_present_after_reads"])


def test_conformance_deferral_not_deallocation() -> None:
    fixture = _check_val_fixture("deferral_not_deallocation.json")
    expected = fixture["expected"]

    vals = _val_lookup(fixture["spec"]["val"])
    lazy: ComputedMap[str, int] = ComputedMap({})

    got_sizes = []
    for k in fixture["reads"]:
        lazy.get_or_insert_with(k, _ctx_factory(vals.__getitem__))
        got_sizes.append(lazy.present_count())
    assert got_sizes == expected["present_after_each_read"]

    lazy_present = set(lazy.present_keys())
    assert lazy_present == set(expected["lazy_present_after_reads"])
    assert lazy_present.issubset(set(expected["eager_present"]))


def test_conformance_entry_kind_orthogonal_to_mode() -> None:
    fixture = load_fixture("entry_kind_orthogonal_to_mode.json")
    expected = fixture["expected"]
    assert expected["default_mode"] == "eager"

    entries = fixture["spec"]["entries"]
    cell_keys = [k for k, e in entries.items() if entry_kind_of(e) is EntryKind.SOURCE]
    slot_keys = [
        k for k, e in entries.items() if entry_kind_of(e) is EntryKind.COMPUTED
    ]
    vals = {k: int(e["val"]) for k, e in entries.items()}
    # ``lookup`` is used both directly as a value producer (``lookup(k)`` to seed
    # a SourceMap entry) and as a family factory; keep it 1-arg and wrap it with
    # :func:`_ctx_factory` only at the family call sites (the ctx-param contract).
    lookup = vals.__getitem__

    # A single ReactiveMap fixes one handle kind, so a mixed-kind fixture is
    # modelled by a SourceMap over the cell entries and a ComputedMap over the slot
    # entries — sharing one logical key space.
    eager_cells: SourceMap[str, int] = SourceMap({})
    for k in cell_keys:
        eager_cells.entry(k, lookup(k))
    eager_slots: ComputedMap[str, int] = ComputedMap({})
    eager_slots.materialize_all(slot_keys, _ctx_factory(lookup))
    assert eager_cells.entry_kind is EntryKind.SOURCE
    assert eager_slots.entry_kind is EntryKind.COMPUTED
    eager_present = set(eager_cells.present_keys()) | set(eager_slots.present_keys())
    assert eager_present == set(expected["eager_present"])

    # Lazy build: cells present at build (always materialized), slots deferred.
    lazy_cells: SourceMap[str, int] = SourceMap({})
    for k in cell_keys:
        lazy_cells.entry(k, lookup(k))
    lazy_slots: ComputedMap[str, int] = ComputedMap({})
    assert set(lazy_cells.present_keys()) == set(expected["lazy_present_at_build"])
    assert lazy_slots.present_keys() == []

    for k in fixture["reads"]:
        if k in slot_keys:
            lazy_slots.get_or_insert_with(k, _ctx_factory(lookup))
        else:
            lazy_cells.get_or_insert_with(k, _ctx_factory(lookup))
    lazy_after = set(lazy_cells.present_keys()) | set(lazy_slots.present_keys())
    assert lazy_after == set(expected["lazy_present_after_reads"])

    # Observational transparency across kinds.
    for k, want in expected["observe"].items():
        if k in cell_keys:
            assert eager_cells.get(k) == want
            assert lazy_cells.get(k) == want
        else:
            assert eager_slots.get(k) == want
            assert lazy_slots.get_or_insert_with(k, _ctx_factory(lookup)) == want


@pytest.mark.parametrize(
    "name",
    [
        "observational_transparency.json",
        "deferral_not_deallocation.json",
        "entry_kind_orthogonal_to_mode.json",
    ],
)
def test_fixture_loads_and_is_computed_map(name: str) -> None:
    fixture = load_fixture(name)
    assert fixture["kind"] in _COMPUTED_MAP_MODELS
    assert fixture["model"] in _COMPUTED_MAP_MODELS
    assert fixture["expected"]["default_mode"] == "eager"


# ---------------------------------------------------------------------------
# Fixture wire-format forward compatibility (``kind``: cell/slot -> source/computed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("cell", EntryKind.SOURCE),
        ("source", EntryKind.SOURCE),
        ("slot", EntryKind.COMPUTED),
        ("computed", EntryKind.COMPUTED),
    ],
)
def test_entry_kind_accepts_old_and_new_fixture_spellings(
    spelling: str, expected: EntryKind
) -> None:
    assert entry_kind_of({"kind": spelling, "val": 1}) is expected


@pytest.mark.parametrize("spelling", ["", "Cell", "memo", "signal", "cells"])
def test_unknown_entry_kind_spelling_is_a_hard_error(spelling: str) -> None:
    # No silent default, no skip: an unreadable fixture fails the run.
    with pytest.raises(AssertionError, match="unknown fixture entry kind"):
        entry_kind_of({"kind": spelling, "val": 1})


def test_entry_kind_orthogonal_fixture_partitions_under_the_new_spelling() -> None:
    # The canonical fixture will later flip `kind` to source/computed; the
    # partition the runner builds must be identical either way. The fixture
    # itself lives in lazily-spec and is NOT edited here — this re-spells a
    # loaded copy in memory.
    fixture = load_fixture("entry_kind_orthogonal_to_mode.json")
    entries = fixture["spec"]["entries"]
    flipped = {
        "cell": "source",
        "slot": "computed",
        "source": "source",
        "computed": "computed",
    }
    respelled = {k: {**e, "kind": flipped[e["kind"]]} for k, e in entries.items()}

    def partition(src: dict) -> tuple[list[str], list[str]]:
        return (
            [k for k, e in src.items() if entry_kind_of(e) is EntryKind.SOURCE],
            [k for k, e in src.items() if entry_kind_of(e) is EntryKind.COMPUTED],
        )

    assert partition(respelled) == partition(entries)
    # Guard against a vacuous pass: the fixture really does carry both kinds.
    sources, computeds = partition(entries)
    assert sources and computeds
