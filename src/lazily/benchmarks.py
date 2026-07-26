"""Instrumentation / benchmarks — :mod:`lazily.benchmarks`.

Microbenchmarks for the reactive core, keyed reconciliation, the keyed cell
collections, and the CRDT plane — the in-process surfaces where proportional-to-
the-diff cost is the load-bearing invariant. Run as a script
(``python -m lazily.benchmarks``) or via :func:`run_benchmarks`; each entry
reports a sample size, total elapsed, and per-op time.
"""

from __future__ import annotations


__all__ = ["Benchmark", "BenchmarkResult", "run_benchmarks", "time_op"]


import time
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable

    from lazily.semtree import _IdLike


@dataclass(frozen=True)
class BenchmarkResult:
    """One benchmark measurement."""

    name: str
    samples: int
    total_seconds: float
    per_op_seconds: float

    def __str__(self) -> str:
        per_us = self.per_op_seconds * 1_000_000.0
        return (
            f"{self.name:<36} {self.samples:>10_d} ops  "
            f"{self.total_seconds * 1000.0:>8.3f} ms  {per_us:>9.3f} us/op"
        )


def time_op(name: str, op: Callable[[], None], samples: int) -> BenchmarkResult:
    """Run ``op`` ``samples`` times and report the wall-clock result."""
    start = time.perf_counter()
    for _ in range(samples):
        op()
    elapsed = time.perf_counter() - start
    return BenchmarkResult(
        name=name,
        samples=samples,
        total_seconds=elapsed,
        per_op_seconds=elapsed / samples if samples else 0.0,
    )


@dataclass
class Benchmark:
    """One named benchmark entry."""

    name: str
    run: Callable[[], BenchmarkResult]


def _bench_slot_read(n: int) -> BenchmarkResult:
    from lazily import CellSlot, Slot

    ctx: dict = {}
    name = CellSlot[dict, dict, str]()

    @Slot
    def derived(c: dict) -> str:
        return f"v-{name(c).value}"

    name(ctx).value = "seed"
    derived(ctx)  # warm the cache
    return time_op("slot.cached_read", lambda: derived(ctx), n)


def _bench_slot_invalidate(n: int) -> BenchmarkResult:
    from lazily import CellSlot, Slot

    ctx: dict = {}
    counter = CellSlot[dict, dict, int]()

    @Slot
    def doubled(c: dict) -> int:
        return counter(c).value * 2

    counter(ctx).value = 0
    i = 0

    def step() -> None:
        nonlocal i
        i += 1
        counter(ctx).value = i
        doubled(ctx)

    return time_op("slot.invalidate_recompute", step, n)


def _bench_reconcile(n: int) -> BenchmarkResult:
    from lazily import Level, reconcile_ops

    # The LIS kernel is now O(n log n) (patience sort, `#lzpylisnlogn`) — it
    # replaced an O(2^n) include-vs-skip recursion. This entry keeps the
    # historical 10-key level for continuity with prior BENCHMARKS.md rows.
    prior: Level[str, int] = Level(
        order=[f"k{i}" for i in range(10)],
        values={f"k{i}": i for i in range(10)},
    )
    target: Level[str, int] = Level(
        order=[f"k{i}" for i in range(1, 9)] + ["k0", "k9"],
        values={f"k{i}": i for i in range(10)},
    )
    return time_op(
        "reconcile.lis_move_minimized",
        lambda: reconcile_ops(prior, target),  # type: ignore[arg-type]
        n,
    )


def _bench_lis_by(keys: int, samples: int) -> BenchmarkResult:
    """The patience-sort LIS kernel (`#lzpylisnlogn`) at a given level size.

    Exercises the move-minimized reconcile over a level of ``keys`` entries
    (half rotated to the tail). The pre-`#lzpylisnlogn` recursion was O(2^n),
    so only N=10 was benchable; N=50/100 are new gates the fix unblocked."""
    from lazily import Level, reconcile_ops

    half = keys // 2
    prior: Level[str, int] = Level(
        order=[f"k{i}" for i in range(keys)],
        values={f"k{i}": i for i in range(keys)},
    )
    target: Level[str, int] = Level(
        order=[f"k{i}" for i in range(half, keys)] + [f"k{i}" for i in range(half)],
        values={f"k{i}": i for i in range(keys)},
    )
    return time_op(
        f"reconcile.lis_n{keys}",
        lambda: reconcile_ops(prior, target),  # type: ignore[arg-type]
        samples,
    )


def _bench_cellmap_insert(n: int) -> BenchmarkResult:
    from lazily import SourceMap

    def step() -> None:
        cmap = SourceMap({})
        for i in range(50):
            cmap.set(i, i)

    return time_op("cellmap.insert_50", step, n)


def _bench_textcrdt_merge(n: int) -> BenchmarkResult:
    from lazily import TextCrdt

    a = TextCrdt.seed(1, "the quick brown fox")
    b = TextCrdt.seed(2, "jumps over the lazy dog")
    return time_op("textcrdt.merge_disjoint", lambda: a.merge(b), n)


def _bench_crdt_plane_apply(n: int) -> BenchmarkResult:
    from lazily import CrdtOp, CrdtPlaneRuntime, WireStamp
    from lazily.ipc import IpcValue_Inline

    plane = CrdtPlaneRuntime(self_peer=0)
    op = CrdtOp.new(1, WireStamp(10, 0, 1), IpcValue_Inline(b"x"))
    return time_op("crdt_plane.idempotent_apply", lambda: plane.apply(op), n)


def _bench_crdt_plane_apply_indexed(n: int) -> BenchmarkResult:
    """Apply updates to a populated plane (`#lzpyfindindex`).

    Each update re-resolves an existing ``(node, key)`` — the path the
    secondary ``_by_node_key`` index turns from an O(n) scan into an O(1) dict
    lookup. Stamps strictly advance each sample so every op clears dedup and
    reaches :meth:`_find`."""
    from lazily import CrdtOp, CrdtPlaneRuntime, WireStamp
    from lazily.ipc import IpcValue_Inline, NodeKey

    size = 100
    plane = CrdtPlaneRuntime(self_peer=0)
    for i in range(size):
        plane.apply(
            CrdtOp.keyed(
                i, NodeKey.new(f"p{i}"), WireStamp(1, i, 1), IpcValue_Inline(b"x")
            )
        )
    tick = [1]

    def step() -> None:
        wall = tick[0] + 1
        tick[0] = wall
        for i in range(size):
            plane.apply(
                CrdtOp.keyed(
                    i,
                    NodeKey.new(f"p{i}"),
                    WireStamp(wall, i, 1),
                    IpcValue_Inline(b"y"),
                )
            )

    return time_op("crdt_plane.apply_indexed_100", step, n)


def _bench_semtree_dirty_chain(n: int) -> BenchmarkResult:
    """Edit the deepest leaf of a 100-deep chain (`#lzpysemtreeparents`).

    ``set_node_value`` walks the ancestor chain via the ``_parents`` index —
    O(depth) instead of the former O(depth x N) full-table scan."""
    from typing import cast

    from lazily import SemTree

    depth = 100
    tree = SemTree[str, int](fold="sum")
    tree.add(cast("_IdLike", "n0"), 0)
    for i in range(1, depth):
        tree.insert_child(cast("_IdLike", f"n{i - 1}"), cast("_IdLike", f"n{i}"), 0)
    leaf = cast("_IdLike", f"n{depth - 1}")
    root = cast("_IdLike", "n0")
    tree.derived(leaf)  # materialize the memoized chain

    def step() -> None:
        tree.set_node_value(leaf, 1)
        tree.derived(root)
        tree.set_node_value(leaf, 0)
        tree.derived(root)

    return time_op("semtree.dirty_chain_100", step, n)


def run_benchmarks(samples: int = 10_000) -> list[BenchmarkResult]:
    """Run the full benchmark suite and return each result.

    The suite exercises the reactive core (cached read + invalidate/recompute),
    keyed reconciliation (LIS move-minimized diff at 10 keys + the patience-sort
    kernel at 10/50/100 keys), :class:`SourceMap` insertion, :class:`TextCrdt`
    merge, the :class:`CrdtPlaneRuntime` apply paths (idempotent + indexed
    lookup), and the :class:`SemTree` dirty-chain walk.
    """
    entries: list[Callable[[], BenchmarkResult]] = [
        lambda: _bench_slot_read(samples),
        lambda: _bench_slot_invalidate(samples),
        lambda: _bench_reconcile(samples),
        lambda: _bench_lis_by(10, samples),
        lambda: _bench_lis_by(50, samples),
        lambda: _bench_lis_by(100, samples),
        lambda: _bench_cellmap_insert(samples),
        lambda: _bench_textcrdt_merge(samples),
        lambda: _bench_crdt_plane_apply(samples),
        lambda: _bench_crdt_plane_apply_indexed(samples),
        lambda: _bench_semtree_dirty_chain(samples),
    ]
    return [entry() for entry in entries]


def main() -> None:
    print("lazily-py benchmarks")
    for result in run_benchmarks():
        print(result)


if __name__ == "__main__":
    main()
