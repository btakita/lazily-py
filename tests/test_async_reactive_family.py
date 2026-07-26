"""``AsyncSourceMap`` / ``AsyncComputedMap`` tests (``#reactivemap``, async flavor).

Mirrors the ``lazily-rs`` ``async_reactive_family.rs`` unit tests. Exercises the
same materialization laws as the single-threaded map plus **eventual
transparency** (proved in ``lazily-formal``'s ``AsyncMaterialization`` module): a
non-blocking read of a derived slot is ``None`` while pending and the canonical
value once resolved (``observe_pending_is_none`` / ``eventual_transparency`` /
``async_resolved_matches_sync``).
"""

from __future__ import annotations

import asyncio

from lazily import AsyncComputedMap, AsyncSourceMap, Cell, EntryKind


def test_eager_computed_map_materializes_all_up_front() -> None:
    fam: AsyncComputedMap[int, int] = AsyncComputedMap({})
    fam.materialize_all([0, 1, 2, 5, 9], lambda _c, k: k * 3)
    assert fam.present_count() == 5
    assert all(fam.is_present(k) for k in (0, 1, 2, 5, 9))
    assert fam.entry_kind is EntryKind.SLOT


def test_lazy_computed_map_defers_until_read() -> None:
    fam: AsyncComputedMap[int, int] = AsyncComputedMap({})
    assert fam.present_count() == 0
    assert not fam.is_present(5)
    # Minting a slot materializes the entry but it is still pending.
    fam.get_or_insert_handle(5, lambda _c, k: k * 3)
    assert fam.observe(5) is None
    assert fam.is_present(5)
    assert fam.present_keys() == [5]


def test_observe_pending_is_none_then_resolves() -> None:
    async def scenario() -> None:
        fam: AsyncComputedMap[int, int] = AsyncComputedMap({})
        fam.materialize_all([1, 2, 3], lambda _c, k: k * 10)
        # Pending before drive: non-blocking observe is None (never a stale value).
        assert fam.observe(2) is None
        # Drive to resolution: canonical value.
        assert await fam.resolve(2, lambda _c, k: k * 10) == 20
        # Eventual transparency: observe now equals the canonical value.
        assert fam.observe(2) == 20

    asyncio.run(scenario())


def test_async_resolved_matches_sync() -> None:
    async def scenario() -> None:
        keys = [0, 1, 2, 5, 9]
        fam: AsyncComputedMap[int, int] = AsyncComputedMap({})
        for k in keys:
            assert await fam.resolve(k, lambda _c, k: k * 3) == k * 3

    asyncio.run(scenario())


def test_source_map_is_always_resolved() -> None:
    async def scenario() -> None:
        fam: AsyncSourceMap[str, int] = AsyncSourceMap({})
        fam.set("a", 7)
        fam.set("b", 7)
        assert fam.entry_kind is EntryKind.CELL
        # Input cells resolve at build — observe is immediate, not None.
        assert fam.observe("a") == 7
        assert await fam.resolve("b", lambda _c, _k: 7) == 7

    asyncio.run(scenario())


def test_source_map_entries_are_writable_inputs() -> None:
    fam: AsyncSourceMap[int, int] = AsyncSourceMap({})
    fam.set(7, 7)
    handle = fam.handle(7)
    assert isinstance(handle, Cell)
    fam.set(7, 100)
    assert fam.observe(7) == 100


def test_present_set_is_monotone_across_reads() -> None:
    fam: AsyncComputedMap[int, int] = AsyncComputedMap({})
    sizes = []
    for k in (2, 4, 2, 5):
        fam.get_or_insert_handle(k, lambda _c, k: k * 2)
        sizes.append(fam.present_count())
    assert sizes == [1, 2, 2, 3]
    assert fam.present_keys() == [2, 4, 5]


def test_stable_handle_across_repeated_get() -> None:
    fam: AsyncComputedMap[int, int] = AsyncComputedMap({})
    assert fam.get_or_insert_handle(3, lambda _c, k: k) is fam.get_or_insert_handle(
        3, lambda _c, k: k
    )
