"""The keyed-collection ordering contract replayed against **all three**
execution flavors.

``test_collection_conformance.py`` already replays the ordering fixtures, but
only against the single-threaded :class:`SourceMap`. That is the blind spot this
file closes: :class:`ThreadSafeReactiveMap` and :class:`AsyncReactiveMap` shipped
``present_keys`` / ``present_count`` and nothing else — no ordering surface and no
reactive membership. The coverage matrix read OK because *a* flavor passed.

Invalidation is measured by **recompute count** inside the reader's own compute
body, not by a cache flag. A counter the library has to move is the one probe
that cannot be satisfied by runner bookkeeping.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from conformance_assert import assert_key_into, assert_key_with, instrument, sub_entries

import lazily


if TYPE_CHECKING:
    from collections.abc import Callable


_LOCAL = Path(__file__).resolve().parent / "conformance"
_SPEC = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"

_FIXTURES = ("cellmap_atomic_move.json", "cellmap_independence.json")


def _load(name: str) -> dict:
    path = _SPEC / "collections" / name
    if not path.exists():
        path = _LOCAL / "collections" / name
    if not path.exists():
        pytest.skip(f"canonical collections fixture not found: {name}")
    return instrument(json.loads(path.read_text()), name=f"collections/{name}")


def _order_digest(keys: list[str]) -> int:
    """Order-sensitive, so an order reader's *value* changes on a reorder and
    not merely its cache state."""
    acc = 17
    for key in keys:
        for ch in key:
            acc = acc * 31 + ord(ch)
        acc = acc * 31 + 7
    return acc


class _Flavor:
    """One execution flavor, driving the same fixture ops."""

    name: str

    def __init__(self) -> None:
        self.ctx: dict = {}
        self.map: Any

    # mutations
    def set_value(self, key: str, value: int) -> None:
        self.map.set(key, value)

    def insert(self, key: str, value: int) -> None:
        self.map.set(key, value)

    def remove(self, key: str) -> None:
        self.map.remove(key)

    def move_to(self, key: str, index: int) -> None:
        self.map.move_to(key, index)

    def move_before(self, key: str, anchor: str) -> None:
        self.map.move_before(key, anchor)

    def move_after(self, key: str, anchor: str) -> None:
        self.map.move_after(key, anchor)

    # untracked observations
    def keys_untracked(self) -> list[str]:
        return self.map.present_keys()

    def value_untracked(self, key: str) -> tuple[int, bool]:
        handle = self.map.handle(key)
        if handle is None:
            return 0, False
        return handle.value, True

    def entry_identity(self, key: str) -> Any:
        """The entry's node identity: stable across a reorder, different after a
        re-mint. This is what separates a move from a remove + insert."""
        return self.map.handle(key)

    # readers, each reporting how many times it recomputed
    def _reader(self, body: Callable[[Any], int]) -> Callable[[], int]:
        count = [0]

        def compute(view: Any) -> int:
            count[0] += 1
            return body(view)

        node = lazily.computed(self.ctx, compute)

        def drive() -> int:
            node.get()
            return count[0]

        return drive

    def value_reader(self, key: str) -> Callable[[], int]:
        handle = self.map.handle(key)

        def body(view: Any) -> int:
            return view.read(handle) if handle is not None else -1

        return self._reader(body)

    def membership_reader(self) -> Callable[[], int]:
        return self._reader(lambda view: self.map.len(view))

    def order_reader(self) -> Callable[[], int]:
        return self._reader(lambda view: _order_digest(self.map.keys(view)))


class SyncFlavor(_Flavor):
    name = "sync"

    def __init__(self) -> None:
        super().__init__()
        self.map = lazily.SourceMap(self.ctx)


class ThreadSafeFlavor(_Flavor):
    name = "thread-safe"

    def __init__(self) -> None:
        super().__init__()
        self.map = lazily.ThreadSafeSourceMap(self.ctx)


class AsyncFlavor(_Flavor):
    name = "async"

    def __init__(self) -> None:
        super().__init__()
        self.map = lazily.AsyncSourceMap(self.ctx)


_FLAVORS = (SyncFlavor, ThreadSafeFlavor, AsyncFlavor)


def _replay(flavor: _Flavor, fixture_name: str) -> None:
    fixture = _load(fixture_name)
    where = lambda i: f"{flavor.name} {fixture_name} step {i}"  # noqa: E731

    initial = fixture.get("initial")
    assert initial, f"{flavor.name}: fixture {fixture_name} has no initial state"
    seed = list(initial.get("order") or [])
    assert seed, f"{flavor.name}: fixture {fixture_name} seeds no keys"
    values = initial.get("values") or {}
    for key in seed:
        assert key in values, f"{flavor.name}: no initial value for key {key}"
        flavor.insert(key, int(values[key]))

    steps = fixture.get("steps") or []
    # A zero-step replay asserts nothing and still reports green.
    assert steps, (
        f"{flavor.name}: fixture {fixture_name} has no steps - "
        "a vacuous replay would report green"
    )

    matrices = 0

    for i, step in enumerate(steps):
        op = step.get("op")
        expected = step.get("expected")
        assert op is not None and expected is not None, f"{where(i)}: malformed step"

        # Rebuild + settle readers from the CURRENT key set so each step's
        # invalidation is measured against a fully settled graph.
        before_keys = flavor.keys_untracked()
        value_readers = {key: flavor.value_reader(key) for key in before_keys}
        baseline = {key: drive() for key, drive in value_readers.items()}
        membership = flavor.membership_reader()
        order = flavor.order_reader()
        membership_base = membership()
        order_base = order()

        ids_before = {key: flavor.entry_identity(key) for key in before_keys}

        kind = op["type"]
        if kind == "set_value":
            flavor.set_value(op["key"], int(op["value"]))
        elif kind == "insert":
            flavor.insert(op["key"], int(op["value"]))
            at = op.get("at")
            # `at` says where the new key lands; minting appends, so "end" is
            # already right. An unrecognised form must fail, not silently append.
            if isinstance(at, int):
                flavor.move_to(op["key"], at)
            elif at is not None:
                assert at == "end", f"{where(i)}: unsupported insert placement {at!r}"
        elif kind == "remove":
            flavor.remove(op["key"])
        elif kind == "move_to":
            flavor.move_to(op["key"], int(op["index"]))
        elif kind == "move_before":
            flavor.move_before(op["key"], op["before"])
        elif kind == "move_after":
            flavor.move_after(op["key"], op["after"])
        else:
            raise AssertionError(
                f"{where(i)}: unsupported op {kind!r} - "
                "an unknown op must fail, never silently skip"
            )

        got_order = flavor.keys_untracked()
        assert_key_with(
            expected,
            "order",
            lambda want, got_order=got_order: got_order == list(want),
            where=f"{where(i)}: order is {got_order}",
        )

        if "membership" in expected:
            assert_key_with(
                expected,
                "membership",
                lambda want, got_order=got_order: set(want) == set(got_order),
                where=f"{where(i)}: membership set",
            )

        # DESCEND into the per-entry maps (#lzsubblockkeyset): each is an object
        # with a key set of its own, and a predicate walking it from the outside
        # leaves an entry the corpus adds compared by nothing.
        if "values" in expected:
            for key, value in sub_entries(expected, "values", where=where(i)):
                got, present = flavor.value_untracked(key)
                assert present, f"{where(i)}: value for {key} is absent"
                assert got == int(value), (
                    f"{where(i)}: value for {key} is {got}, expected {value}"
                )

        # The invalidation matrix, read from expected.invalidates - where the
        # fixtures actually nest it. lazily-rs read it off the step instead, so
        # its assertion never ran once.
        assert "invalidates" in expected, (
            f"{where(i)}: expected.invalidates is missing - the matrix is the contract"
        )
        # Every branch below compares against this matrix, so reading it here
        # books the assertion (#lzconsumednotasserted). The DESCENT is what holds
        # the matrix's own key set (#lzsubblockkeyset) — a projection the corpus
        # adds beside `value`/`membership`/`order` fails as unconsumed.
        invalidates = expected.sub("invalidates")
        matrices += 1

        dirty = set(
            assert_key_into(invalidates, "value", lambda fixture_value: fixture_value)
            or []
            if "value" in invalidates
            else []
        )
        survivors = set(got_order)
        for key, drive in value_readers.items():
            if key not in survivors:
                continue  # removed by this op: no entry left to read
            recomputed = drive() != baseline[key]
            if key in dirty:
                assert recomputed, (
                    f"{where(i)}: value reader for {key} should have been invalidated"
                )
            else:
                assert not recomputed, (
                    f"{where(i)}: value reader for {key} should have stayed cached - "
                    "per-entry independence is the whole point"
                )

        want_membership_dirty = bool(
            assert_key_into(
                invalidates, "membership", lambda fixture_value: fixture_value
            )
            if "membership" in invalidates
            else False
        )
        assert (membership() != membership_base) is want_membership_dirty, (
            f"{where(i)}: membership reader invalidation mismatch - "
            "a pure reorder must NOT invalidate set-identity readers"
        )

        want_order_dirty = bool(
            assert_key_into(invalidates, "order", lambda fixture_value: fixture_value)
            if "order" in invalidates
            else False
        )
        assert (order() != order_base) is want_order_dirty, (
            f"{where(i)}: order reader invalidation mismatch"
        )

        # Handle stability: the law separating an atomic move from a remove +
        # re-mint. A reorder keeps the entry's node, so dependents and lineage
        # survive.
        if "handle_stable" in expected:
            for key, want_stable in sub_entries(
                expected, "handle_stable", where=where(i)
            ):
                after = flavor.entry_identity(key)
                before = ids_before.get(key)
                if want_stable:
                    assert before is not None and after is before, (
                        f"{where(i)}: handle for {key} must survive the move - "
                        "a reorder that re-mints is a remove + insert, not a move"
                    )
                else:
                    assert before is None or after is None or after is not before, (
                        f"{where(i)}: handle for {key} should have changed"
                    )

    assert matrices > 0, (
        f"{flavor.name}: fixture {fixture_name} asserted no invalidation matrix"
    )


@pytest.mark.parametrize("flavor_cls", _FLAVORS, ids=lambda c: c.name)
@pytest.mark.parametrize("fixture_name", _FIXTURES)
def test_ordering_contract_all_flavors(flavor_cls, fixture_name: str) -> None:
    _replay(flavor_cls(), fixture_name)


@pytest.mark.parametrize("flavor_cls", _FLAVORS, ids=lambda c: c.name)
def test_directional_moves_all_flavors(flavor_cls) -> None:
    """Cover a direction the canonical corpus does not.

    ``cellmap_atomic_move.json``'s only ``move_before`` step moves a key that
    already *follows* its anchor (from=2, anchor=0), so it exercises only the
    branch where the insertion point is the anchor index itself. The branch where
    the key *precedes* its anchor — target ``anchor - 1`` — is never replayed.
    That is exactly the direction lazily-zig's ``moveBefore`` was wrong in:
    ``move_before("a","d")`` on ``[a,b,c,d]`` produced ``[b,c,d,a]``. The
    canonical corpus would have scored that binding green.

    The negative-index case covers a defect this work found in this binding:
    ``move_to`` clamped only the upper end, so a negative index reached Python's
    insert-from-the-right semantics and landed the key near the end instead of at
    the front — the same missing lower clamp lazily-js has.
    """
    seed = ["a", "b", "c", "d"]

    def build():
        flavor = flavor_cls()
        for i, key in enumerate(seed):
            flavor.insert(key, i + 1)
        return flavor

    cases = [
        (
            "move_before, key precedes anchor",
            lambda f: f.move_before("a", "d"),
            ["b", "c", "a", "d"],
        ),
        (
            "move_before, key follows anchor",
            lambda f: f.move_before("d", "b"),
            ["a", "d", "b", "c"],
        ),
        (
            "move_after, key precedes anchor",
            lambda f: f.move_after("a", "c"),
            ["b", "c", "a", "d"],
        ),
        (
            "move_after, key follows anchor",
            lambda f: f.move_after("d", "a"),
            ["a", "d", "b", "c"],
        ),
        (
            "move_to past the end clamps",
            lambda f: f.move_to("a", 99),
            ["b", "c", "d", "a"],
        ),
        # -1 is the discriminating index, not -5: Python clamps a very negative
        # list.insert to 0 all by itself, so -5 lands correctly even WITHOUT a
        # lower clamp. -1 is where the missing clamp actually shows, inserting
        # before the last element instead of at the front.
        (
            "move_to to -1 clamps to the front",
            lambda f: f.move_to("d", -1),
            ["d", "a", "b", "c"],
        ),
        (
            "move_to far below zero clamps",
            lambda f: f.move_to("d", -5),
            ["d", "a", "b", "c"],
        ),
        (
            "move on an absent key is a no-op",
            lambda f: (f.move_before("zz", "a"), f.move_to("zz", 0)),
            seed,
        ),
    ]

    for what, run, want in cases:
        flavor = build()
        identity_before = flavor.entry_identity("a")
        run(flavor)
        got = flavor.keys_untracked()
        assert got == want, (
            f"{flavor.name}: {what} gave {got}, expected {want} - "
            "the target must be computed on the pre-removal list"
        )
        assert flavor.entry_identity("a") is identity_before, (
            f"{flavor.name}: {what} re-minted entry a - a reorder must keep the node"
        )
