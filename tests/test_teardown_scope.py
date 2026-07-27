"""Teardown scopes, node disposal, and degree introspection (``#lzspecedgeindex``).

The cross-language corpus in ``tests/test_reactive_graph_conformance.py`` is the
acceptance test for the *semantics*; this module covers the parts of the Python
surface the corpus cannot reach, because the fixtures are written against an
abstract op vocabulary rather than any binding's spelling:

* the ``with`` / ``async with`` spelling, which is the whole reason a scope is a
  context manager here and not a value with a destructor,
* :meth:`~lazily.teardown.TeardownScope.adopt`,
* the mypyc-compiled/interpreted split, where a stale in-place ``.so`` silently
  shadows an edited ``.py`` and turns a mutation into a false green.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from lazily import Cell, Effect, Slot, effect
from lazily.async_context import AsyncContext
from lazily.slot import DisposedError
from lazily.teardown import TeardownScope, teardown_scope


# --------------------------------------------------------------------------- #
# The compiled/interpreted split
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module", ["cell", "slot", "effect"])  # type: ignore[misc]
def test_disposal_api_is_live_on_whichever_module_is_loaded(module: str) -> None:
    """The disposal surface must exist on the module that actually imported.

    ``make compile`` writes ``src/lazily/<mod>.cpython-*.so`` next to the source,
    and an extension module wins over its ``.py`` sibling on ``sys.path``. A
    stale ``.so`` therefore keeps serving the *previous* build while the edited
    source sits unused — every test still passes, against code nobody changed.

    Asserting the attributes on ``sys.modules`` rather than on a fresh import
    pins the object the rest of the suite is really exercising, whichever of the
    two paths that is.
    """
    loaded = sys.modules[f"lazily.{module}"]
    origin = getattr(loaded, "__file__", "")
    assert origin, f"lazily.{module} has no __file__ to attribute behaviour to"
    owner = {"cell": "Cell", "slot": "Slot", "effect": "Effect"}[module]
    node = getattr(loaded, owner)
    for name in ("dispose", "disposed", "dependent_count", "dependency_count"):
        assert hasattr(node, name), (
            f"{owner} loaded from {origin} has no `{name}` — if that path ends "
            f"in .so, the compiled extension is stale; run `make compile`"
        )


# --------------------------------------------------------------------------- #
# Degree introspection
# --------------------------------------------------------------------------- #


def test_degrees_count_both_directions() -> None:
    ctx: dict = {}
    src = Cell(ctx, 1)
    mid = Slot(lambda c: c.read(src) + 1)
    sink = Slot(lambda c: mid(c) + 10)

    # Degrees are zero until something actually reads: an edge is discovered by
    # a read, never declared.
    assert src.dependent_count() == 0
    assert sink.dependency_count() == 0

    assert sink(ctx) == 12
    assert src.dependent_count() == 1
    assert mid.dependent_count() == 1
    assert mid.dependency_count() == 1
    assert sink.dependency_count() == 1
    # A cell is a pure source and reads nothing.
    assert src.dependency_count() == 0


def test_repeated_reads_do_not_grow_the_degree() -> None:
    ctx: dict = {}
    src = Cell(ctx, 1)
    reader = Slot(lambda c: c.read(src) + c.read(src) + c.read(src))
    assert reader(ctx) == 3
    assert src.dependent_count() == 1
    assert reader.dependency_count() == 1


# --------------------------------------------------------------------------- #
# Disposal: the three semantics
# --------------------------------------------------------------------------- #


def test_disposal_detaches_both_directions() -> None:
    ctx: dict = {}
    src = Cell(ctx, 1)
    mid = Slot(lambda c: c.read(src) + 1)
    assert mid(ctx) == 2
    assert src.dependent_count() == 1

    mid.dispose(ctx)
    assert mid.disposed
    assert src.dependent_count() == 0
    assert mid.dependency_count() == 0


def test_disposal_dirties_a_surviving_reader() -> None:
    """Semantic 1 — the regression ``lazily-rs`` 5db90d2 / ``lazily-js`` 4d20670.

    Detaching edges without dirtying leaves the reader serving a cached value
    computed from a node that no longer exists, forever: the edge that would
    have invalidated it is the one disposal just removed.
    """
    ctx: dict = {}
    src = Cell(ctx, 4)
    derived = Slot(lambda c: c.read(src))
    reader = Slot(lambda c: derived(c) + 1)
    assert reader(ctx) == 5

    derived.dispose(ctx)

    # Not the stale 5: the reader is dirty, recomputes, and reaches the error.
    with pytest.raises(DisposedError):
        reader(ctx)


def test_disposal_does_not_run_a_reached_effect() -> None:
    """Semantic 2 — disposal is not a publish.

    An effect reached by the dirtying walk must be marked, never scheduled:
    running it during teardown re-enters a body that reads the node being torn
    down, so teardown would stop being idempotent and the error would surface
    inside ``dispose`` instead of on the next recompute.
    """
    ctx: dict = {}
    runs: list[str] = []
    src = Cell(ctx, 1)
    derived = Slot(lambda c: c.read(src))

    def body(_c: dict) -> None:
        runs.append("run")
        derived(_c)

    watcher = effect(body)
    watcher(ctx)
    assert runs == ["run"]

    derived.dispose(ctx)
    assert runs == ["run"], "disposal scheduled an effect"

    # The contract is "errors on next recompute", so the effect is still live
    # and still subscribed — it just has not run.
    assert not watcher.disposed


def test_disposal_is_idempotent_and_terminal() -> None:
    ctx: dict = {}
    src = Cell(ctx, 1)
    derived = Slot(lambda c: c.read(src))
    assert derived(ctx) == 1

    derived.dispose(ctx)
    derived.dispose(ctx)  # no-op, no raise
    with pytest.raises(DisposedError):
        derived(ctx)

    src.dispose()
    src.dispose()
    with pytest.raises(DisposedError):
        _ = src.value
    # A write to a torn-down source is inert rather than an error.
    src.set(99)


def test_churn_returns_the_dependent_set_to_baseline() -> None:
    """The leak that motivates disposal at all.

    A subscribe/unsubscribe cycle that disposes what it creates leaves the
    source's dependent set at exactly its starting size, no matter how many
    cycles run. The assertion is on the edge-set *size*, not on any allocator
    statistic: reusing storage is not what conformance means here.
    """
    ctx: dict = {}
    topic = Cell(ctx, 0)

    def subscribe() -> Effect:
        def body(_c: dict) -> None:
            _ = _c.read(topic)

        handle = Effect(body)
        handle(ctx)
        return handle

    live = [subscribe() for _ in range(8)]
    assert topic.dependent_count() == 8

    for cycle in range(500):
        index = cycle % 8
        live[index].dispose()
        live[index] = subscribe()
        assert topic.dependent_count() == 8

    for handle in live:
        handle.dispose()
    assert topic.dependent_count() == 0
    # The source itself is untouched by its subscribers coming and going.
    assert topic.value == 0


# --------------------------------------------------------------------------- #
# Scopes
# --------------------------------------------------------------------------- #


def test_with_block_disposes_members_on_exit() -> None:
    ctx: dict = {}
    topic = Cell(ctx, 2)

    with teardown_scope(ctx) as scope:
        doubled = scope.computed(lambda c: c.read(topic) * 2)
        assert doubled(ctx) == 4
        assert len(scope) == 1
        assert topic.dependent_count() == 1

    assert doubled.disposed
    assert topic.dependent_count() == 0
    # Grouping bounds teardown, not the source it read.
    assert topic.value == 2


def test_scope_bounds_teardown_not_visibility() -> None:
    """A scoped node reads parent-owned nodes freely, and outlives nothing."""
    ctx: dict = {}
    parent_owned = Cell(ctx, 3)

    with teardown_scope(ctx) as scope:
        inner = scope.computed(lambda c: c.read(parent_owned) + 1)
        assert inner(ctx) == 4

    assert not parent_owned.disposed
    assert parent_owned.value == 3


def test_scope_tears_down_in_reverse_creation_order() -> None:
    """Semantic 3 — dependents before what they read.

    Graph state is order-independent, so this is not about the edges: effect
    *cleanups* are side effects, and a dependent's cleanup must not observe a
    graph where what it read is already gone.
    """
    ctx: dict = {}
    order: list[str] = []
    topic = Cell(ctx, 1)

    scope = TeardownScope(ctx)
    first = scope.computed(lambda c: c.read(topic))

    def earlier(_c: dict) -> Any:
        first(_c)
        return lambda: order.append("cleanup_earlier")

    def later(_c: dict) -> Any:
        first(_c)
        return lambda: order.append("cleanup_later")

    scope.effect(earlier)
    scope.effect(later)

    scope.close()
    # Reverse creation order: the later effect's cleanup runs first.
    assert order == ["cleanup_later", "cleanup_earlier"]
    assert first.disposed


def test_scope_teardown_equals_the_fold_of_individual_disposals() -> None:
    """``disposeScope_eq_disposeAll`` — the two routes are observationally equal."""

    def build(use_scope: bool) -> tuple[dict, Cell[int], Any, list[str]]:
        ctx: dict = {}
        cleanups: list[str] = []
        topic = Cell(ctx, 1)
        outside = Slot(lambda c: c.read(topic) + 100)
        scope = TeardownScope(ctx)

        a = scope.computed(lambda c: c.read(topic) + 1)
        b = scope.computed(lambda c: a(c) + 2)

        def watch(_c: dict) -> Any:
            b(_c)
            return lambda: cleanups.append("watch_b")

        watch_b = scope.effect(watch)

        assert outside(ctx) == 101
        assert b(ctx) == 4

        if use_scope:
            scope.close()
        else:
            scope.disarm()
            watch_b.dispose()
            b.dispose(ctx)
            a.dispose(ctx)
        return ctx, topic, outside, cleanups

    scoped_ctx, scoped_topic, scoped_outside, scoped_cleanups = build(True)
    folded_ctx, folded_topic, folded_outside, folded_cleanups = build(False)

    assert scoped_cleanups == folded_cleanups == ["watch_b"]
    assert (
        scoped_topic.dependent_count() == folded_topic.dependent_count() == 1
    )  # only `outside` survives
    assert scoped_outside(scoped_ctx) == folded_outside(folded_ctx) == 101


def test_disarm_disposes_nothing() -> None:
    ctx: dict = {}
    topic = Cell(ctx, 1)
    scope = TeardownScope(ctx)
    escaped = scope.computed(lambda c: c.read(topic))
    assert escaped(ctx) == 1
    assert len(scope) == 1

    scope.disarm()
    assert not scope.armed
    assert len(scope) == 0

    scope.close()
    assert not escaped.disposed
    assert escaped(ctx) == 1
    assert topic.dependent_count() == 1


def test_adopt_takes_ownership_of_an_existing_node() -> None:
    ctx: dict = {}
    orphan = Slot(lambda c: 7)
    assert orphan(ctx) == 7

    with teardown_scope(ctx) as scope:
        assert scope.adopt(orphan) is orphan
        assert len(scope) == 1

    assert orphan.disposed


def test_closing_a_scope_twice_is_a_no_op() -> None:
    ctx: dict = {}
    scope = TeardownScope(ctx)
    node = scope.computed(lambda c: 1)
    scope.close()
    scope.close()
    assert node.disposed


# --------------------------------------------------------------------------- #
# Async surface
# --------------------------------------------------------------------------- #


def test_async_scope_disposes_members_on_exit() -> None:
    async def main() -> None:
        ctx = AsyncContext()
        topic = ctx.source(2)

        async def double(cc: Any) -> int:
            return cc.get(topic) * 2

        async with ctx.scope() as scope:
            doubled = scope.computed(double)
            assert await ctx.get_async(doubled) == 4
            assert ctx.dependent_count(topic) == 1
            assert ctx.dependency_count(doubled) == 1

        assert doubled.disposed
        assert ctx.dependent_count(topic) == 0
        with pytest.raises(DisposedError):
            await ctx.get_async(doubled)

    asyncio.run(main())


def test_async_disposal_dirties_a_surviving_reader() -> None:
    """Semantic 1 on the path where it is hardest to notice.

    Async staleness is a revision counter, not a pull chain: a reader left
    ``Resolved`` after its dependency is torn down keeps handing out that
    resolved value with no edge left to disturb it.
    """

    async def main() -> None:
        ctx = AsyncContext()
        src = ctx.source(4)

        async def derive(cc: Any) -> int:
            return cc.get(src)

        derived = ctx.computed(derive)

        async def read_derived(cc: Any) -> int:
            return await cc.get_async(derived) + 1

        reader = ctx.computed(read_derived)
        assert await ctx.get_async(reader) == 5

        ctx.dispose_computed(derived)

        with pytest.raises(DisposedError):
            await ctx.get_async(reader)

    asyncio.run(main())


def test_async_disposal_does_not_schedule_a_reached_effect() -> None:
    """Semantic 2 on the async path."""

    async def main() -> None:
        ctx = AsyncContext()
        runs: list[str] = []
        src = ctx.source(1)

        async def derive(cc: Any) -> int:
            return cc.get(src)

        derived = ctx.computed(derive)

        async def body(cc: Any) -> None:
            runs.append("run")
            await cc.get_async(derived)

        watcher = ctx.effect_async(body)
        await watcher.settle()
        assert runs == ["run"]

        ctx.dispose_computed(derived)
        await asyncio.sleep(0)
        assert runs == ["run"], "disposal scheduled an async effect"
        assert not watcher.disposed

    asyncio.run(main())


def test_async_scope_tears_down_in_reverse_creation_order() -> None:
    """Semantic 3 on the async path.

    ``aclose`` has its own loop over ``owned``, so the synchronous test does not
    cover it — a forward-iteration regression in one is invisible to the other.

    Three effects, not two, and the assertion is on the whole ordered list: with
    a single member every order is the same order, which is exactly why
    ``scope_teardown_equals_fold_of_disposals`` could not discriminate here.
    """

    async def main() -> None:
        ctx = AsyncContext()
        order: list[str] = []

        def member(name: str) -> Any:
            async def body(_cc: Any) -> Any:
                async def cleanup() -> None:
                    order.append(name)

                return cleanup

            return body

        scope = ctx.scope()
        for name in ("first", "second", "third"):
            handle = scope.effect_async(member(name))
            await handle.settle()
        assert order == [], "cleanup ran before any rerun or disposal"

        await scope.aclose()
        assert order == ["third", "second", "first"]

    asyncio.run(main())


def test_async_disarm_disposes_nothing() -> None:
    async def main() -> None:
        ctx = AsyncContext()
        topic = ctx.source(1)

        async def compute(cc: Any) -> int:
            return cc.get(topic)

        scope = ctx.scope()
        escaped = scope.computed(compute)
        assert await ctx.get_async(escaped) == 1

        scope.disarm()
        assert len(scope) == 0
        await scope.aclose()

        assert not escaped.disposed
        assert await ctx.get_async(escaped) == 1

    asyncio.run(main())
