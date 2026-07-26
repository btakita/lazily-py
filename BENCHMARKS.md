# lazily-py Benchmarks

Wall-clock benchmarks for the lazily-py hot paths. Two suites:

- **Micro-benchmarks** — the in-library suite in
  [`src/lazily/benchmarks.py`](src/lazily/benchmarks.py) (`run_benchmarks()`),
  covering the reactive core, keyed reconciliation, `SourceMap`, `TextCrdt`, the
  CRDT plane, and the `SemTree` dirty-chain walk.
- **Scale** — a large spreadsheet-shaped graph
  ([`src/lazily/scale_bench.py`](src/lazily/scale_bench.py)) mirroring the
  lazily-rs [`scale`](https://github.com/lazily-hub/lazily-rs/blob/main/benches/scale.rs)
  and lazily-go `scale` groups.

All timings use `time.perf_counter()`. Lower is better.

> **The reactive core is mypyc-compiled.** `Slot` / `Cell` / `Signal` / `Effect`
> / `CellSlot` are compiled to native C extension classes (one compilation unit
> for `slot` / `cell` / `signal` / `effect` / `batch`), giving the speedups
> below. The public classes are decorated with
> `@mypyc_attr(allow_interpreted_subclasses=True)`, so interpreted subclasses
> (`class HttpClient(Slot[...])`) keep working — this is the
> `Py_TPFLAGS_BASETYPE`-equivalent that unblocked compilation. The same `.py`
> sources ship as a fallback used automatically when compilation is unavailable
> (no C toolchain); the "pure-Python" columns below are that fallback path. See
> the [Compilation (shipped)](#compilation-shipped) section for the full story.

## Reproduce

```bash
make bench          # micro-suite
make bench-scale    # scale suite at the default N = 1,000,000

# or directly:
uv run python -m lazily.benchmarks
uv run python -m lazily.scale_bench

# scale at a specific size / viewport:
LAZILY_SCALE_N=1000000 uv run python -m lazily.scale_bench
LAZILY_SCALE_N=5000000 uv run python -m lazily.scale_bench   # Google Sheets 10M-cell workbook
LAZILY_SCALE_VIEWPORT=1000 uv run python -m lazily.scale_bench
```

Treat the absolute numbers as indicative — the shapes (relative costs,
size-scaling behavior) are what transfer across runs. **CPython is far heavier
per node than Rust or Go**: every `Cell` and `Slot` is a Python object carrying
its own dependency sets, so both time and memory grow much faster with `N` than
in the compiled bindings. These numbers are reported honestly, not to claim
parity.

### Hardware / environment

| | |
|---|---|
| CPU | AMD Ryzen 9 9950X3D (16 cores / 32 threads) |
| RAM | 186 GiB |
| OS | Linux 7.1.1 (CachyOS), x86-64 |
| Python | 3.12.12 (CPython) |

## Micro-benchmark results

10,000 samples per entry (`run_benchmarks()` default). **compiled** = mypyc
native core; **pure-Python** = the shipped fallback (no `.so`).

| Benchmark | compiled µs/op | pure-Python µs/op | speedup | What it measures |
|-----------|---------------:|------------------:|--------:|------------------|
| `slot.cached_read` | 0.039 | 0.067 | ~1.7× | Cached (memoized) slot read — the steady-state pull with no recompute. |
| `slot.invalidate_recompute` | 0.352 | 0.540 | ~1.5× | Set a cell, then re-pull a dependent slot (edge re-tracking + recompute). |
| `reconcile.lis_move_minimized` | 4.6 | 4.6 | n/a¹ | LIS move-minimized keyed reconcile over a 10-key level (patience-sort O(n log n) kernel, `#lzpylisnlogn` — was ~159 µs under the O(2ⁿ) recursion it replaced). |
| `reconcile.lis_n10` | 6.1 | 6.1 | n/a¹ | The patience-sort LIS kernel over a 10-key level (half rotated). New `#lzpylisnlogn` gate. |
| `reconcile.lis_n50` | 39 | 39 | n/a¹ | Same kernel over 50 keys — unbenchable before `#lzpylisnlogn` (O(2ⁿ)). |
| `reconcile.lis_n100` | 107 | 107 | n/a¹ | Same kernel over 100 keys — unbenchable before `#lzpylisnlogn` (O(2ⁿ)). |
| `cellmap.insert_50` | 15.8 | 21.1 | ~1.3× | Build a `SourceMap` and insert 50 keyed entries (whole-collection construction, not per-insert). |
| `textcrdt.merge_disjoint` | 1.43 | 1.32 | n/a¹ | Merge two disjoint `TextCrdt` documents (Fugue/RGA order recomputed). |
| `crdt_plane.idempotent_apply` | 0.089 | 0.087 | n/a¹ | Re-apply an already-seen op to a `CrdtPlaneRuntime` (idempotent dedupe path). |
| `crdt_plane.apply_indexed_100` | 300 | 300 | n/a¹ | Apply 100 stamp-advancing updates to a 100-entry plane — each re-resolves an existing `(node, key)` via the `#lzpyfindindex` dict (was an O(n) scan per op). |
| `semtree.dirty_chain_100` | 50 | 50 | n/a¹ | Edit the deepest leaf of a 100-deep `SemTree` chain — the `#lzpysemtreeparents` index makes the ancestor dirty-walk O(depth) (was O(depth × N)). |

¹ `reconcile` / `textcrdt` / `crdt_plane` / `semtree` are not part of the mypyc
compilation unit (only the reactive core is), so their numbers move only with
run-to-run noise — they index the non-compiled paths.

### Notes

- The reactive steady state is cheap: a compiled cached slot read is ~39 ns, and
  an invalidate + recompute round trip is ~0.35 µs — roughly 1.5–1.7× faster than
  the pure-Python fallback under CPython's interpreter overhead.
- `reconcile.lis_*` measures the longest-increasing-subsequence kernel behind
  move-minimized keyed reconciliation. Through v0.32.0 it was a definitional
  include-vs-skip recursion (genuinely longest, not greedy) that was **O(2ⁿ)**,
  so the bench was capped at 10 keys. v0.33.0 (`#lzpylisnlogn`) replaces it with
  a **patience-sort O(n log n)** kernel that returns the same
  lexicographically-smallest LIS — ~35× faster at 10 keys and able to scale to
  N=50/100 (the new `reconcile.lis_n50` / `reconcile.lis_n100` gates).
- `cellmap.insert_50` measures whole-collection construction (50 inserts +
  allocation), not a single insert — divide by 50 for per-insert. Its speedup
  comes for free from the compiled `Cell` it builds on.

## Scale (≥1M cells) — spreadsheet-shaped graph

Replicates the lazily-rs `scale` group on a spreadsheet-shaped graph: `N` input
cells + `N` formula slots where `formula[i] = input[i] + input[i-1]` (local
fan-in, like a column of `=A_i + A_{i-1}`). With the default `N = 1,000,000`
that is **~2,000,000 reactive nodes**. Four scenarios cover the spreadsheet
lifecycle:

- `build` — construct all `2N` nodes (formulas lazy, not yet computed).
- `cold_full_recalc` — first read of every formula (forces every compute + edge-tracking).
- `viewport_recalc` — edit one input, read only a 1,000-cell viewport.
- `full_recalc_invalidate_all` — re-set every input, then read every formula.

> **A "cell count" here counts two cells per row** — the graph models a column of
> formulas `=A_i + A_{i-1}`, so each row is **one input cell `A_i` plus one
> formula cell**. `N` rows ⇒ `N` inputs + `N` formulas = `2N` cells.

### Compiled vs pure-Python (N = 300,000 rows, back-to-back A/B)

The mypyc-compiled core versus the pure-Python fallback on the same machine,
same run. The interactive-edit (`viewport_recalc`) and worst-case-edit
(`full_recalc_invalidate_all`) paths — the ones a spreadsheet UI actually hits —
see the largest gains.

| Benchmark | compiled | pure-Python | speedup |
|-----------|---------:|------------:|--------:|
| `build` | 949 ns/cell | 1120 ns/cell | ~1.18× |
| `cold_full_recalc` | 297 ns/cell | 403 ns/cell | ~1.36× |
| `viewport_recalc` | **34.4 µs/edit** | 66.3 µs/edit | **~1.93×** |
| `full_recalc_invalidate_all` | 528 ns/cell | 846 ns/cell | ~1.60× |

`build` is the smallest gain because it is allocation/GC-bound on one
interpreter object per node — mypyc speeds up the `Cell`/`Slot` constructors but
cannot remove the per-node allocation. The *recompute* paths run the compiled
attribute/cache machinery, which is where the speedup concentrates.

### 1,000,000 rows (~2M cells / nodes) — pure-Python fallback

The numbers below are the **pure-Python fallback** (no `.so`), matching what a
platform without a C toolchain ships. The compiled core scales proportionally
(see the N=300k A/B above for the per-path speedup). Peak RSS ~1.4 GiB.

| Benchmark | Time | Per cell | What it measures |
|-----------|-----:|---------:|------------------|
| `build` | 2.70 s | ~1.35 µs | Construct all 2N nodes (dependency edge sets are lazy — materialized on first read, not at construction — so build is allocation-bound on the `Cell`/`Slot` objects + formula closures, not on pre-allocated `set`s). |
| `cold_full_recalc` | 478 ms | ~478 ns | First read of every formula — forces every compute + edge-tracking (this is where the lazy edge sets materialize). |
| `viewport_recalc` | **66.1 µs** | — | Edit one input, read only a 1,000-cell viewport. ~7,200× cheaper than a full cold recalc. |
| `full_recalc_invalidate_all` | 1.03 s | ~1.03 µs | Re-set every input, then recompute the whole sheet (worst-case full-sheet edit). |

### 5,000,000 rows (10M cells — a full Google Sheets workbook)

Google Sheets caps a workbook at **10,000,000 cells**. Modeled as 5,000,000
input cells + 5,000,000 formula cells (`LAZILY_SCALE_N=5000000`). This is the
**largest size actually measured** — no extrapolation. Peak RSS ~6.5 GiB.

| Benchmark | Time | Per cell | What it measures |
|-----------|-----:|---------:|------------------|
| `build` | 12.4 s | ~1.24 µs | Build the full 10M-node workbook (allocation/GC-bound on the node objects + closures; edge sets are lazy). |
| `cold_full_recalc` | 2.79 s | ~558 ns | Compute all 5M formulas cold. |
| `viewport_recalc` | **72.7 µs** | — | Edit one input, read a 1,000-cell viewport. ~38,400× cheaper than a full cold recalc. |
| `full_recalc_invalidate_all` | 6.49 s | ~1.30 µs | Re-set every input, recompute the whole workbook. |

So lazily-py backs a **full-capacity Google Sheets workbook** on CPython:
building it is the expensive part (~12 s, allocation/GC-bound — one Python
object per node), but once built, a full cold recompute is ~2.8 s, and a
one-cell edit + bounded-viewport read stays in the **~73 µs range**. The lazy
pull-based model leaves off-viewport formulas dirty and never recomputes them —
only ~2 formulas actually recompute per edit (the two that read the edited
input), regardless of sheet size, which is exactly the property a
viewport-rendered spreadsheet needs.

### Spreadsheet cell-count context

| Spreadsheet | Documented limit | Cells |
|-------------|------------------|------:|
| Google Sheets | 10,000,000 cells per workbook (18,278 columns max) | 10,000,000 |
| Microsoft Excel | 1,048,576 rows × 16,384 columns per worksheet | 17,179,869,184 |

The `LAZILY_SCALE_N=5000000` run above covers a full Google Sheets workbook. A
grid-complete Excel worksheet (17 billion cells) is unrepresentative — real
sheets populate a tiny fraction of the grid, and lazily stores only the cells
you create, so the `scale` group measures the populated-cell path that matters.

## Notes

### CPython per-node overhead

Build dominates the lifecycle here (~1.24–1.35 µs/node) and is allocation- and
GC-bound: each reactive node is one interpreter object (`Cell` or `Slot`) plus
its formula closure. The dependency-edge `set`s (external subscribers +
auto-discovered parents) are **lazy** — an empty CPython `set()` is ~216 B, so
they are materialized only on first subscriber/edge, not at construction. A
quiescent 10M-node workbook therefore allocates ~10M node objects + closures
up front (the edge sets appear later, as formulas are read and edges form).
This is the inherent cost of one interpreter object per reactive node — the
compiled bindings (Rust slotmap arrays, Go structs) build the same graph far
cheaper. The *recompute* paths (`cold_full_recalc` ~0.46–0.56 µs/formula) are
much closer to the compiled bindings because they run the interpreter loop, not
the allocator.

### Iterative invalidation

The invalidation wave (a cell write cascading through its dependents) runs on
an explicit module-level work-stack rather than recursing one CPython frame per
graph level, mirroring lazily-rs `mark_frontier_locked`. This caps the call
stack at a constant depth regardless of cascade length, so an arbitrarily deep
chain of slots/signals (e.g. a 10,000-row formula cascade) invalidates without
risking CPython's recursion limit. A `batch` funnels every changed-cell root
through one coalesced drain, and effects are deduped by identity, so a
dependent reached through many changed cells in one batch fires at most once.

### Viewport scaling — flat, unlike lazily-go

lazily-py's viewport recalc is **effectively size-independent** (66.1 µs at 2M
nodes, 72.7 µs at 10M nodes). The value cache is a plain Python `dict` keyed by
node identity, so both the ~1,000 viewport cache-hit lookups and the ~2 actual
recomputes are O(1) hash operations that don't scale with total sheet size. This
matches lazily-rs's flat curve and avoids the mild per-lookup growth lazily-go
reports from Go-map cache/TLB pressure on a multi-GB map. At 10M cells a
one-cell edit + 1,000-cell viewport read is ~38,400× cheaper than a full cold
recalc and never touches off-viewport formulas.

### Threading

These are single-threaded benchmarks. The concurrency surfaces
(`ThreadSafeContext`, signaling, the CRDT plane) are correctness-tested rather
than benchmarked here.

### Compilation (shipped)

The reactive core (`slot` / `cell` / `signal` / `effect` / `batch`) is compiled
to native C extension classes with **mypyc 2.3** (`mypy 2.3.0`), invoked through
`setup.py`'s `mypycify(...)` as a single compilation unit so cross-file
native-class inheritance (`BaseSlot` → `CellSlot`, `Slot` → `Effect` /
`_SignalSlot`) gets mypyc's early binding. `make build` ships a platform wheel
(`cp312-cp312-linux_x86_64.whl`, etc.) carrying the compiled `.so` alongside the
`.py` sources.

**The subclassability blocker — solved.** The prior prototype (documented here
as "future work") found that mypyc compiles classes to C extension types that
CPython interpreted classes cannot subclass, so compiling `Slot` broke the
`class HttpClient(Slot[...])` extension pattern. mypyc 2.x solves this directly:
the public classes are decorated with
`@mypyc_attr(allow_interpreted_subclasses=True)` (the `Py_TPFLAGS_BASETYPE`
equivalent — `mypy_extensions.mypyc_attr`), so interpreted subclasses keep
working while the native classes themselves stay fast. All four public classes
(`BaseSlot`/`Slot`/`Cell`/`Signal`/`Effect`) opt in, so the public subclassable
contract is unchanged (`tests/test_slot.py::test_complex_dependency_graph`
passes against the compiled core).

**`callable` as an overridden method.** An interpreted subclass may override
`callable` as a *method* rather than assigning the instance attribute (the
`HttpClient` pattern). mypyc compiles `self.callable` to a native struct read,
which would miss the method (the slot is unset). `slot._callable_of` is
deliberately typed `Any` so mypyc emits a generic, MRO-aware attribute read that
finds the method. This is off the hot path — cached reads return before
`callable` is ever touched, and ordinary native slots keep their fast native
attribute *write* in `__init__`; only the (cache-miss) read is generic.

**Pure-Python fallback.** `setup.py` downgrades to a pure-Python build (no
extension modules) if mypyc is unavailable or C compilation fails, and the wheel
always carries the `.py` sources — so the package stays installable on platforms
without a C toolchain, losing the speedup but keeping full correctness and the
public API. A compiled install never imports `mypy_extensions` at runtime (the
decorator is baked into the C); it is a runtime dependency only for the fallback
sources.

**Dev workflow.** `make compile` rebuilds the in-place `.so` files after editing
the core; `make check` / `make bench` then run against the compiled code. With
no `.so` present (fresh checkout, no `make compile`), they run against the
pure-Python sources — both paths are green. `--follow-imports=silent` scopes
mypyc's mypy pass to the compilation unit: the package type-checks with `ty`
(not mypy), and this silences pre-existing mypy-only errors in modules reached
through `lazily/__init__` re-exports without affecting the compiled core.
