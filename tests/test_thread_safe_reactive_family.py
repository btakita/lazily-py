"""``ThreadSafeSourceMap`` / ``ThreadSafeComputedMap`` tests (``#reactivemap``,
thread-safe flavor).

Mirrors the ``lazily-rs`` ``thread_safe_reactive_family.rs`` unit tests.
Exercises the same materialization laws as the single-threaded map (eager
pre-mint / lazy mint-on-access, present-set monotonicity, transparency) plus
**materialization confluence** (proved in ``lazily-formal``'s ``Materialization``
module as ``materialize_present_comm`` / ``materialize_observe_comm``): whatever
order the lock admits concurrent materializations in, the present set and every
observed value are identical.
"""

from __future__ import annotations

import threading

import pytest

from lazily import (
    Cell,
    DisposedError,
    EntryKind,
    Slot,
    ThreadSafeComputedMap,
    ThreadSafeContext,
    ThreadSafeSourceMap,
)


def test_eager_computed_map_materializes_all_up_front() -> None:
    fam: ThreadSafeComputedMap[int, int] = ThreadSafeComputedMap({})
    fam.materialize_all([0, 1, 2, 5, 9], lambda _c, k: k * 3)
    assert fam.present_count() == 5
    assert all(fam.is_present(k) for k in (0, 1, 2, 5, 9))
    assert fam.entry_kind is EntryKind.COMPUTED


def test_lazy_computed_map_defers_until_read() -> None:
    fam: ThreadSafeComputedMap[int, int] = ThreadSafeComputedMap({})
    assert fam.present_count() == 0
    assert fam.get_or_insert_with(5, lambda _c, k: k * 3) == 15
    assert fam.is_present(5)
    assert fam.present_keys() == [5]


def test_eager_and_lazy_observe_identically() -> None:
    eager: ThreadSafeComputedMap[int, int] = ThreadSafeComputedMap({})
    eager.materialize_all([0, 1, 2, 5, 9], lambda _c, k: k * 3)
    lazy: ThreadSafeComputedMap[int, int] = ThreadSafeComputedMap({})
    for k in (0, 1, 2, 5, 9):
        assert eager.observe(k) == lazy.get_or_insert_with(k, lambda _c, k: k * 3)


def test_source_map_materializes_at_build() -> None:
    fam: ThreadSafeSourceMap[str, int] = ThreadSafeSourceMap({})
    for k in ("a", "b", "c"):
        fam.set(k, 0)
    assert fam.entry_kind is EntryKind.SOURCE
    assert fam.present_count() == 3


def test_source_map_writes_through_coalescing_context() -> None:
    fam: ThreadSafeSourceMap[int, int] = ThreadSafeSourceMap({})
    fam.set(7, 7)
    assert fam.observe(7) == 7
    fam.set(7, 100)  # routed through the ThreadSafeContext boundary
    assert fam.observe(7) == 100


def test_source_map_remove_disposes_held_handle() -> None:
    fam: ThreadSafeSourceMap[str, int] = ThreadSafeSourceMap({})
    fam.set("a", 7)
    handle = fam.handle("a")
    assert handle is not None

    assert fam.remove("a")
    assert handle.disposed
    with pytest.raises(DisposedError, match="disposed cell"):
        handle.get()


def test_computed_map_remove_disposes_held_handle() -> None:
    ctx: dict = {}
    fam: ThreadSafeComputedMap[str, int] = ThreadSafeComputedMap(ctx)
    handle = fam.get_or_insert_handle("a", lambda _c, _k: 7)
    assert fam.observe("a") == 7

    assert fam.remove("a")
    assert handle.disposed
    with pytest.raises(DisposedError, match="disposed slot"):
        handle(ctx)


def test_observe_is_reactive_when_factory_reads_a_cell() -> None:
    ctx: dict = {}
    src = Cell(ctx, 10)
    fam: ThreadSafeComputedMap[int, int] = ThreadSafeComputedMap(ctx)
    fam.materialize_all([1], lambda c, k: c.read(src) + k)
    seen: list[int] = []
    reader = Slot(lambda c: fam.observe(1, c))
    watcher = Slot(lambda c: seen.append(reader(c)))
    watcher(ctx)
    assert seen == [11]
    src.set(100)
    watcher(ctx)
    assert seen == [11, 101]


def test_stable_handle_across_repeated_get() -> None:
    fam: ThreadSafeComputedMap[int, int] = ThreadSafeComputedMap({})
    h1 = fam.get_or_insert_handle(3, lambda _c, k: k * 2)
    h2 = fam.get_or_insert_handle(3, lambda _c, k: k * 2)
    assert h1 is h2  # first-writer-wins keeps one stable handle


def test_present_set_grows_monotonically() -> None:
    fam: ThreadSafeComputedMap[int, int] = ThreadSafeComputedMap({})
    fam.get_or_insert_with(5, lambda _c, k: k)
    fam.get_or_insert_with(5, lambda _c, k: k)  # repeat: no growth
    fam.get_or_insert_with(9, lambda _c, k: k)
    assert fam.present_count() == 2
    assert fam.present_keys() == [5, 9]


def test_materialization_confluence_under_concurrent_reads() -> None:
    # Many threads materialize the same key space in different orders; the present
    # set (as a set) and every observed value MUST be identical regardless of the
    # interleaving the lock admits (materialize_present_comm / _observe_comm).
    keys = list(range(64))
    fam: ThreadSafeComputedMap[int, int] = ThreadSafeComputedMap(
        {}, ts=ThreadSafeContext()
    )
    handles: dict[int, list[object]] = {k: [] for k in keys}
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(order: list[int]) -> None:
        barrier.wait()
        for k in order:
            h = fam.get_or_insert_handle(k, lambda _c, k: k * 7)
            with lock:
                handles[k].append(h)

    threads = [
        threading.Thread(target=worker, args=(keys[::-1] if i % 2 else keys,))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(fam.present_keys()) == set(keys)
    assert fam.present_count() == len(keys)
    # First-writer-wins: every thread observed the SAME handle object per key.
    for k in keys:
        first = handles[k][0]
        assert all(h is first for h in handles[k]), f"key {k} handle not stable"
    for k in keys:
        assert fam.get_or_insert_with(k, lambda _c, k: k * 7) == k * 7
