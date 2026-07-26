## 0.37.0

### Added

- **The `ReactiveMap` Core surface now binds every flavor.** Ordering and atomic
  move bound only the single-threaded map; the thread-safe and async maps
  exposed the present set and nothing else. All flavors now carry `keys`, `len`,
  `is_empty`, `contains_key`, `position`, `move_to` / `move_before` /
  `move_after`, and `remove`, each with membership and order signals minted on
  its own graph. A move touches no entry handle and awaits nothing, so it is
  neither thread- nor async-coloured.
- A shared, graph-agnostic `KeyedOrder` core holding the present set, the key
  order, and the move algebra. It deliberately holds no reactivity: membership
  and order invalidation is a graph write, so each flavor owns its own cells.
- The canonical ordering fixtures now replay against all three flavors, with
  invalidation measured by recompute count rather than a cache flag, plus
  directional move coverage the canonical corpus does not provide (its only
  `move_before` step moves a key that already follows its anchor, so the
  `anchor - 1` branch was never exercised).

### Fixed

- `move_to` clamped only the upper end, so a negative index reached Python's
  insert-from-the-right semantics and `move_to(key, -1)` landed the key
  second-to-last instead of at the front. Both ends are clamped now.

## 0.36.0

### Changed

- **Renamed the keyed maps to `SourceMap` / `ComputedMap`**, finishing the v2 kernel
  migration: the node kinds became `Source` and `Computed`, and the map names now say which
  kind of entry they hold instead of the pre-v2 `Cell` / `Slot` vocabulary.
  `CellMap` -> `SourceMap`, `SlotMap` -> `ComputedMap`, and the `ThreadSafe*` / `Async*`
  variants alongside them.

### Deprecated

- The old names are kept as deprecated aliases of the new ones, so existing callers still
  compile. Conformance runners accept both the old and new `model` spellings in a fixture;
  the corpus emits only the new. Fixture FILE names are unchanged.


### Changed

- **v2 Cell kernel (`#lzcellkernel`) — removed the v1 value vocabulary.**
  `Signal` / `signal` / `signal_def`, `formula` / `formula_def` / `FormulaCell`,
  `SourceCell` / `SourceCellSlot`, and the `Computed.drive()` / `.undrive()` /
  `is_driven` / `is_active` methods are **deleted** (they shipped only in 0.x).
  Use the v2 spelling: an eager `Computed` is `computed(ctx, f).eager()`; the
  lifecycle is `.eager()` / `.lazy()` / `.is_eager()`.
- **`source` / `computed` are the canonical constructors.** `cell` / `cell_def`
  are **deprecated** aliases of `source` / `source_def`, and the derived-value
  `slot` decorator is **deprecated** in favour of the guarded `computed`
  (`Slot` remains as the storage-sense primitive; `slot_def` remains the
  storage-sense resolver factory). Every derived cell is guarded — there is no
  unguarded derived mode, and the former `memo` role folds into `computed`.
- **Unified `get` / `set` on the async and thread-safe contexts.** `get` reads
  both source cells and computed handles; `set` writes source cells only
  (computed handles are not writable — write protection). `get_cell` / `set_cell`
  are **deprecated** forwarders. `AsyncContext.cell` is deprecated toward
  `AsyncContext.source`.

### Fixed

- **`Signal` re-materialized once per write inside a `batch`, not once per
  flush.** The eager puller was welded into the backing slot's `_invalidate`,
  so it recomputed *during* invalidation — earlier than the effect flush — and
  the compute count scaled with the number of changed sources in the batch. The
  values were always correct, which is why it shipped unnoticed. The puller is
  now an ordinary `Effect` over the backing memo, the composition
  `lazily-spec/docs/reactive-graph.md` § "Signal eagerness" recommends, so it
  obeys "effects are scheduled, not inline" and N writes in one batch produce
  exactly one compute (normative clause 3, `#lzsignaleager`).
- **`ThreadSafeContext.batch` did not coalesce effects.** Its flush touched each
  changed cell outside the single-threaded `batch` boundary, leaving
  `in_batch()` false, so every scheduled reader — an `Effect`, and therefore a
  `Signal`'s puller — reran once per changed cell instead of once per batch. The
  flush now runs inside that boundary. Same defect, same clause, second write
  path.

### Added

- Reactive-graph conformance: the three `signal_*` / `dispose_signal_*` fixtures
  replay against `Context` and `ThreadSafeContext`, including the `signal`,
  `dispose_signal` and `batch` ops and the `computes_of` observable. They skip
  on `AsyncContext`, which ships no signal constructor — recorded in the
  runner's `EXPECTED_SKIPS` ledger as a gap in the public surface.

## 0.34.0

### Removed

- **BREAKING** `Cell.subscribe`, `Slot.subscribe`, and `Signal.subscribe`, with their
  registration tables and disposers. `lazily-spec` v0.35.0 makes observer APIs a
  `MUST NOT` on every reactive: observation is a declared dependency edge, and a
  callback list on a reactive bypasses batching, glitch-freedom, coalescing, and
  scope teardown. `StateMachine.on_transition` is now an `Effect`; use a `Topic`
  when you need every transition rather than the settled value.

### Added

- `AsyncContext` — py was the last of eight bindings without one.
- Teardown scopes (`teardown_scope`, `TeardownScope`, `AsyncTeardownScope`),
  handle-side `Slot.dispose` / `Cell.dispose`, and `dependent_count` /
  `dependency_count` on every node kind.
- Reactive-graph conformance runner: 11 canonical fixtures replayed against all
  three contexts.

### Fixed

- Async effect cleanup ran at the end of the flush that ran the body rather than
  on rerun or dispose. The canonical effect acquires in the body and releases in
  the cleanup, so eager cleanup released while the effect was still live — an
  effect that subscribed and returned an unsubscribe would unsubscribe itself
  immediately. `lazily-spec` `b6eb030` makes the trigger normative.
- `AsyncSlot` handed every waiter the same `asyncio.Future`, so one caller
  cancelling its `get_async` killed the shared computation for all remaining
  waiters.
- Disposal did not stop an in-flight re-resolve, so a waiter mid-loop could
  respawn and publish into a disposed graph.
- Disposal did not dirty the surviving dependent cone, leaving a live reader
  frozen on its cached value.

### Added

- **Teardown scopes, per-node disposal, and degree introspection
  (`#lzspecedgeindex`).**
  Until now a `Cell` or `Slot` lived as long as the context mapping that cached
  it. Handles are plain references, but the *reverse* edge set on every source
  is a strong reference to each reader, so dropping every handle to a node
  reclaimed nothing and a source's dependent set grew by one per subscribe and
  never shrank. A workload with a constant live subscriber count degraded
  without bound in both memory and propagation cost, since every publish walks
  that list.

  - `Slot.dispose(ctx)`, `Cell.dispose()`, and the existing `Effect.dispose()`
    now detach **both** edge directions and go terminal. Reading a disposed node
    raises the new `lazily.DisposedError`; disposing twice is a no-op; writing
    to a disposed cell is inert.
  - `TeardownScope` / `teardown_scope(ctx)` group nodes created through them and
    dispose the group in reverse creation order. It is a context manager —
    Python has no `Drop`, and `with` is the construct that already means "this
    block owns these, release them on the way out". `disarm()` cancels the
    teardown without touching the nodes. `ThreadSafeContext.scope(ctx)` returns
    the same object; `AsyncContext.scope()` returns the `async with` twin,
    `AsyncTeardownScope`, because disposing an async effect awaits its cleanup.
  - `AsyncContext.dispose_slot` / `dispose_cell`, and
    `dependent_count` / `dependency_count` on every node kind — counts, never
    the collections, so introspection cannot become a path into the graph.

  Disposal **dirties the surviving dependent cone**: detaching edges alone would
  leave a live reader serving a cached value computed from a node that no longer
  exists, because the edge that would have invalidated it is the one disposal
  just removed. Effects reached by that walk are marked and deliberately *not*
  scheduled — disposal is not a publish, and rerunning an effect during teardown
  re-enters a body that reads the node being torn down. The contract is "errors
  on the next recompute".

  Slots now track forward edges (`dependency_count`) as well as reverse ones.
  Disposal cannot remove a node from its dependencies' reverse sets without
  them, and the fixture that measures the leak asserts on edge-set *size*, so an
  implementation that filtered disposed entries at query time would report
  conformant while still leaking.

### Changed — tests

- The reactive-graph conformance runner replays **all nine** canonical
  `lazily-spec/conformance/reactive-graph` fixtures against all three shipped
  contexts (`Context`, `ThreadSafeContext`, `AsyncContext`), up from one; both
  fixture shapes now execute, including `scenarios` and its
  `observationally_equal` relation. The corpus is 11 fixtures as of
  `lazily-spec` b6eb030 — 126 ops and 153 assertions per context.

  All three contexts replay every fixture with **no divergences**: the runner's
  `KNOWN_DIVERGENCES` ledger is empty and is asserted exactly in both
  directions, so a regression fails the build.

- **The three disposal semantics are mutation-verified.** A test never observed
  failing is not yet evidence, so each semantic was individually broken and the
  suite confirmed red: dropping the dirty-cone cascade, scheduling effects
  during the teardown walk, and iterating scope members forward instead of
  reversed. Each is caught by a direct test and — since `lazily-spec` added
  `disposal_does_not_run_surviving_effects` and
  `teardown_runs_members_in_reverse_creation_order` — by the corpus, on all
  three contexts. The scope-ordering tests use three effects rather than two and
  assert the whole ordered list, because with a single member every order is the
  same order.

### Fixed

- **`AsyncEffect` no longer runs a body's cleanup at the end of its own flush.**
  The cleanup a body returns is now *retained* and runs on the next rerun
  (before the next body) or on disposal, matching sync `Effect`, `lazily-go`,
  and `lazily-dart`. The eager spelling released the resource an effect had just
  acquired while the effect was still live and would rerun later — an effect
  that subscribed and returned an unsubscribe closure unsubscribed itself
  instantly. `lazily-spec/docs/async.md` § Conformance item 5 now makes the
  *trigger* normative ("rerun or dispose and at no other time"), not merely the
  ordering, which eager cleanup satisfied vacuously. This is a behavioural
  change to a published async contract.

### Removed

- **The reactive observer APIs are gone (`Cell.subscribe`, `Signal.subscribe`,
  `Slot.subscribe`).**
  No reactive in `lazily` exposes an observer API, in any binding. Observation
  in a reactive graph is a declared dependency edge, not a registered callback:
  a callback registry bolted onto a reactive bypasses the graph, ignores
  batching, and breaks glitch-freedom. The decisive objection is cost — the
  registry (and, on `Cell`, its registration counter) was paid by *every*
  instance whether or not anyone subscribed. Four of the eight bindings (`rs`,
  `cpp`, `js`, `kt`) never had `Cell` observers; they were right.

  Where a caller genuinely needs a stream of every transition rather than the
  settled value, that is a `Topic`, which every binding already has.

  This reverses the registration-keying work earlier in this same unreleased
  cycle (`b2bc6bd`, `#lzdartobservercow`): the code is deleted rather than
  fixed. `Cell.__slots__` drops from five entries to three (`_parents`,
  `_value`, `ctx`); `Cell.touch()` now does nothing but push the auto-discovered
  reactive parents into the coalesced invalidation wave.

  `Signal` carried the identical registry — `signal.py` documented it as
  "an external (non-reactive) change callback", with `_parents` tracked
  separately as the actual graph edges — so it goes for the same reasons, plus
  one of its own: its dedup was by equality via a `set`, which the spec now
  states as a MUST NOT. That is the defect where two components sharing a bound
  method silently cancel each other's subscription. `Signal.__slots__` drops
  `_subscribers`; `_parents` is kept on both types, being the genuine graph
  edges. Nothing in `src/` called `Signal.subscribe` — after
  `StateMachine.on_transition` moved to an effect, the library contains zero
  `.subscribe(` calls on any reactive.

  `Slot` was the third and last one, and the most thoroughly mislabelled: its
  own `touch()` comment read "External subscribers persist across touches (they
  are not reactive edges)" directly above the loop that invoked them. The edge
  set it was mistaken for is `_parents`, which sits two lines below doing the
  rebind-then-clear the comment describes. `Slot.__slots__` drops to
  `("_parents",)`, `_invalidate` and `touch` lose their notify loops, and
  `Effect` (which subclasses `Slot`) stops clearing an attribute that no longer
  exists.

  All three registries were labelled or assumed to be dependency-graph edges and
  none of them were. In each case the real edge set sat beside the registry
  under a different name — `_parents` on all three types. The tell was uniform
  and available in the source the whole time: graph edges are rebound to `None`
  and re-established on recompute; observer registries persist across
  notifications. Anything that survives a `touch` is not an edge.

  Removed with them: the `CellSubscriber` and `SlotSubscriber` protocols,
  `tests/test_cell_observer.py`, `tests/test_signal_observer.py`, and
  `tests/test_reactive_graph_conformance.py`. The two `Slot.subscribe` tests in
  `tests/test_recursion_regression.py` were **ported to Effects, not deleted**
  — see Changed. The conformance runner went
  because its entire supported op vocabulary was the observer API — with the
  spec's `observer_*` fixtures withdrawn, every remaining fixture in
  `lazily-spec/conformance/reactive-graph` uses the disposal/teardown-scope
  vocabulary this binding does not model, so the runner would have skipped 100%
  of its fixtures and still reported green. A conformance suite that executes
  nothing is worse than no suite. When `lazily-py` grows teardown scopes, a
  runner for *that* vocabulary gets written then. The matching CI fixture-presence
  guard in `.github/workflows/precommit.yml` was dropped for the same reason.

### Changed

- **The two `Slot.subscribe` recursion-regression tests were ported to Effects.**
  Both pinned commit `30907ea`; neither was dropped, but they no longer pin the
  same things and the file says so.

  Fix 1 (`Slot.__call__` must not cascade while computing) still discriminates
  exactly, and its symptom got *worse*, not weaker: with no callback registry, a
  cascade during computation no longer fires a spurious notification — it
  destroys the dependency edge, because `touch` rebinds `_parents` to `None` and
  wipes the very dependent that was mid-computation registering itself. The
  dependent then never updates again. The ported test asserts the second run
  happens, which the original `notified == []` form could not.

  Fix 2 (reset re-entrancy) lost its mechanism. Notify-before-clear was a
  property of a callback registry, and there is no registry left to re-enter;
  propagation is an append to an iterative work-stack drained by one loop, so
  reordering the clear in `Slot._invalidate` is now unobservable — verified by
  reverting it, both orderings terminate identically. The guarantee relocated to
  `Effect._running`, which is the only remaining way user code runs inside an
  invalidation wave, and the ported test pins it there: deleting that guard
  makes it diverge without bound. Relocated coverage, honestly labelled, rather
  than a test that quietly stopped discriminating.

- **`StateMachine.on_transition` is now an `Effect`.** It was the only non-test
  consumer of `Cell.subscribe`. It is reimplemented the way the Rust reference
  (`lazily-rs`, `state_machine.rs`) always did it: an effect over the backing
  cell with a `prev` captured in the closure, firing `handler(old, new)` only on
  a real change and returning a disposer. The initial run seeds `prev` and fires
  nothing, as before.

  **Behaviour change under `batch`.** An effect-based `on_transition` is a graph
  participant, so it sees only the *settled* value of a batch: `A -> B -> C`
  inside one `batch` now reports a single `(A, C)` transition rather than two.
  This is intended — a batch asserts atomicity, so intermediate states are not
  observable. Unbatched transitions are unaffected and still report every step.

## 0.33.0

### Changed — performance (Phase 2 quick wins of `tasks/agent-doc/plans/lazily-perf-memory-audit.md`)

- **Patience-sort LIS (`#lzpylisnlogn`).** `reconciliation.lis_by` replaced its
  O(2ⁿ) include-vs-skip recursion (genuinely longest, not greedy) with an
  O(n log n) patience-sort kernel. It returns the same lexicographically-smallest
  longest increasing subsequence the recursion chose, so the stable set a
  reconcile selects is unchanged. The 10-key micro-bench drops from ~159 µs to
  ~4.6 µs (~35×), and N=50 / N=100 — unbenchable before — are now first-class
  gates (`reconcile.lis_n50` / `reconcile.lis_n100`). Verified against
  `lazily-spec/conformance/collections/keyed_reconciliation_lis.json`.
- **CRDT plane secondary index (`#lzpyfindindex`).** `CrdtPlaneRuntime` keeps a
  `_by_node_key: dict[(NodeId, path|None), int]` so `_find` is an O(1) dict
  lookup instead of an O(n) entry scan. `apply()` / `apply_frame()` /
  `delta_sync()` drop O(N·M) → O(N+M). New `crdt_plane.apply_indexed_100` gate.
- **`SemTree` parent index (`#lzpysemtreeparents`).** A `_parents:
  dict[Id, list[Node]]` index makes `_mark_dirty_chain` an O(depth) walk up the
  ancestor chain instead of an O(depth × N) full-table scan per ancestor. New
  `semtree.dirty_chain_100` gate.
- **Reconcile LIS computed once (`#lzpyreconcileidx`).** `stable_keys` builds the
  prior-index map once via `enumerate` (O(n)) instead of an `idx_in` linear scan
  per common key (O(n·m)), and `reconcile_ops` derives the moved set from the
  single LIS rather than recomputing it.
- **`@dataclass(slots=True)` sweep (`#lzpySlotsSweep`).** ~47 leaf dataclasses
  across `ipc.py`, `textcrdt.py`, `command.py`, `temporal.py`, `service.py`,
  `rateshape.py`, and the `lossless_tree_crdt.py` internal record/body classes
  now declare `slots=True` (frozen leaves get `frozen=True, slots=True`). The
  non-slots tagged-union bases (`NodeState` / `IpcValue` / `DeltaOp`) and
  `PermissionDenied(Exception)` were intentionally skipped.
- **`match` dispatch (`#lzpymatchdispatch`).** The 6-way `isinstance` chains in
  `lossless_tree_crdt._kind_to_dict` / `_apply_op` / `_dependencies_ready` are
  now `match` statements (class patterns). `_apply_op` also consolidates the
  per-node dict lookup on `CreateNode`.

## 0.32.0

### Changed — performance (CRDT plane, Phase 1 of `tasks/agent-doc/plans/lazily-perf-memory-audit.md`)

- **`TextCrdt._visible_ordered` cache (`#lztextordcache`).** The DFS pre-order
  is now memoized and invalidated on every mutation that changes the element
  set (`seed`/`insert`/`insert_str`/`merge`/`apply_delta`/`gc`). Repeated
  `text()` / `__len__()` between mutations drops from O(N log N) per call to
  O(N) total (one rebuild + N filter passes) instead of N × O(N log N).
  Tombstone flips (`delete`) do **not** invalidate the cache — the DFS includes
  tombstones and callers filter at read.
- **`LosslessTreeCrdt` parent→children index (`#lzlivelchildidx`).** A new
  `_children_by_parent` dict replaces the O(N) full-scan in `_live_children`.
  `render()` drops from O(N²) to O(N) over the tree. Tombstones remain in the
  bucket (logical delete); sort is lazy on read. Maintained at every
  `CreateNode` / `SplitLeaf` site (all structural mutations flow through
  `_apply_op` / `_apply_split`); `fork` rebuilds from the copied node map.
- `insert_str` origin chaining (`#lztextinsertchain`) was already in place.

## 0.31.2

### Changed — packaging

- **CI auto-publish to PyPI.** The `wheels.yml` publish step now uploads the
  built matrix with the PyPI API token from the repo secret
  `PYPI_PUBLISH_TOKEN` (sourced from passage `btak/PYPI_PUBLISH_TOKEN` — the
  same key Claude/Codex use for `twine upload`), replacing OIDC trusted
  publishing (which needed PyPI-side config). A `git tag v*` now builds and
  publishes the full wheel matrix hands-off.
- **macOS x86_64 via Apple-Silicon cross-compile.** Dropped the deprecated
  Intel `macos-13` runner (which stalled ~40 min queueing); `macos-14` now
  builds both arm64 and x86_64 (`CIBW_ARCHS_MACOS`).
- Fixed drifted `lazily.__version__` (was stuck at `0.29.0`).

## 0.31.1

### Changed — packaging

- **Precompiled wheels for every supported platform.** A cibuildwheel
  `.github/workflows/wheels.yml` now builds the mypyc-compiled wheel matrix on
  tag release: Linux (manylinux + musllinux, x86_64 + aarch64), macOS (x86_64 +
  arm64), and Windows (x86_64), for CPython 3.12 / 3.13 / 3.14. Users no longer
  fall back to pure Python on non-(Linux cp312 x86_64) platforms; the compiled
  extension installs directly. (0.31.0 shipped only a Linux cp312 x86_64 wheel +
  sdist; this release fills out the matrix.)

## 0.31.0

### Changed — reactive-core performance

- **mypyc-compiled reactive core.** The five core modules (`slot` / `cell` /
  `signal` / `effect` / `batch`) are now compiled as a single mypyc compilation
  unit. Public classes use `@mypyc_attr(allow_interpreited_subclasses=True)`
  (mypyc's `Py_TPFLAGS_BASETYPE` equivalent) so interpreted subclasses such as
  `class HttpClient(Slot[...])` keep working. A prebuilt CPython 3.12 Linux
  wheel ships the compiled extensions; on other platforms / interpreters the
  sdist's build step falls back to pure Python automatically (the package stays
  installable everywhere, losing only the speedup). Measured (CPython 3.12):
  `slot.cached_read` ~1.7×, `viewport_recalc` ~1.9×, `full_recalc_invalidate_all`
  ~1.6×.
- **Iterative DFS invalidation + batch root coalescing.** The recursive
  `Cell.touch` / `Slot.reset` cascade is replaced by an explicit work-stack, and
  `batch()` now collects all changed-cell roots into one DFS pass — mirroring
  lazily-rs `mark_frontier_locked` / `flush_batched_invalidations`. Removes the
  CPython recursion limit on deep dependency chains.
- **Hot-path micro-optimizations.** The `resolve_identity` no-op resolver is
  short-circuited with an `is` check on every cached read; the single-threaded
  `batch()` state moved from `threading.local` + `getattr`/`hasattr` to plain
  module globals; `touch()` snapshots switched from `tuple(...)` allocation to
  rebind-then-clear.

## 0.30.0

### Added

- Realtime + distributed primitive families, at full parity with lazily-rs:
  temporal sources (`#lztime` — `TimerCell`/`IntervalCell`/`CronCell`/`DeadlineCell`),
  rate-shaping operators (`#lzrateshape` — `DebounceCell`/`ThrottleCell`/`SampleCell`/`ProbabilisticSampleCell`),
  membership + failure detection (`#lzmemb` — `MembershipCell` SWIM + Phi-accrual),
  coordination (`#lzcoord` — `LeaseCell`/`LeaderCell`/`LockCell`/`SemaphoreCell`/`BarrierCell`),
  presence + ephemeral plane (`#lzpresence` — `PresenceCell`/`AwarenessCell`/`EphemeralCell`),
  stream windowing (`#lzwindow` — tumbling/sliding/session),
  fault tolerance (`#lzresilience` — `CircuitBreakerCell`/`RetryPolicyCell`/`BulkheadCell`/`TimeoutCell`),
  and the embedded-service plane (`#lzservice` — `HealthCell`/`ReadinessCell`/`DiscoveryCell`/`ServiceRegistry`).

## 0.29.0

### Added

- **`WorkQueueCell` competing-consumer delivery (`#lzworkqueue`).** Exclusive
  FIFO claims use stable item ids and fresh delivery ids, worker-scoped
  ack/nack settlement, strict visibility expiry, tail redelivery, bounded
  dead-letter handling, and independent reactive count readers.

## 0.28.1

### Fixed

- **Serialized monotonic outbox cursors.** Every outbox operation refreshes the
  persisted acknowledgement cursor, so a stale handle cannot regress replay or
  retention semantics after another handle advances the same durable store.

## 0.28.0

### Added

- **`CrdtTree` (`#lzcrdttree`).** A runtime-checkable lossless document CRDT
  protocol implemented by `TextCrdt`, with one identity-preserving delta format
  for snapshots, incremental sync, and merge.
- **Storage-independent durable outbox (`#lzdurableoutbox`).** `OutboxStore`
  exposes five ordered-byte operations, `Outbox` owns monotonic cursors and
  replay semantics, and `SqliteStore` / `SqliteOutbox` provide crash-safe
  ecosystem persistence using the Python standard library.

## 0.27.0

### Added

- **`TopicCell` broadcast topics (`#lztopiccell`).** Independent absolute
  subscriber cursors, durable offline replay, ephemeral disconnect lifecycle,
  per-subscriber reactive invalidation, snapshot restore, and safe prefix GC at
  the slowest durable cursor.

## 0.26.0

### Added

- **RelayCell — Phases 2–6 (`#relaycell`).** The algebra-typed conflating relay,
  ported from lazily-rs: `RelayCell` (hot head under a `MergePolicy`, reactive
  `BackpressurePolicy`, `Overflow` block/drop-newest/drop-oldest/conflate/spill,
  demand-driven `depth`/`is_full`/`is_empty` slots; rejects `Conflate` for a
  non-conflating policy), `SpillStore` paged durable tail (`reconstruct`
  spill_lossless, `replay_unacked` idempotent replay, ack-before-reclaim),
  `Transport` protocol (`InProcTransport`/`FramedTransport`), `Outbox`/`Inbox`
  role facades (producer backpressure via `is_full`; remote credit meter), and
  the Phase-6 policies `RatePolicy`/`WindowPolicy`/`ExpiryPolicy`/
  `PriorityStorage`/`KeyedRelay`. Completes RelayCell parity across all 8
  bindings.

## 0.25.0

### Added

- **Merge algebra + `MergeCell` (Phase 1, `#relaycell`).** `MergePolicy` (an
  associative fold `⊕` with `commutative`/`idempotent`/`conflates` flags) with
  policies `KeepLatest`/`Sum`/`Max`/`SetUnion`/`RawFifo`; `MergeCell` generalizes
  `Cell` (`Cell ≡ MergeCell(KeepLatest)`), a source whose write is a merge,
  inheriting the `!=` store-guard + store-without-cascade. Property-based
  law-tests + replay of the cross-language `mergecell_algebra.json` fixture.

## 0.24.0

### Changed

- **Demand-driven queue reader-kinds + optional `peek`/`capacity` (Phase 0,
  `#relaycell`).** `QueueCell` reader-kinds (`head`/`len`/`is_empty`/`is_full`)
  are now demand-driven memoized Slots (were eagerly-set Cells): a successful
  push/pop derives no reader value and invalidates only the readers whose value
  provably changed. `peek`/`capacity` become optional `QueueStorage`
  capabilities — the minimal contract is `try_push`/`try_pop`/`len`/`is_closed`/
  `close`, so a raw-channel-style backend conforms directly (no `head`/`is_full`
  reader). Observable semantics are unchanged; all conformance fixtures stay
  green.
- **BREAKING:** `QueueReaderHandles` `head`/`len`/`is_empty`/`is_full` are now
  `Slot`s (were `Cell`s); `closed` stays a `Cell`.

## 0.23.0

Adds **Reliable Sync** (`#lzsync` + `#sync-driver`), the delivery-reliability
layer over the `Snapshot`/`Delta`/`CrdtSync` planes (lazily-spec § Reliable
Sync), matching the `lazily-rs` / `lazily-js` reference bindings:

* **`ResyncCoordinator`** — receiver-side decision function (`Apply` /
  `RequestSnapshot` / `Ignore`) over the inbound frame stream, multi-epoch-span
  aware; single-request-per-gap resync suppression.
* **`DurableOutbox`** (ABC) + **`InMemoryOutbox`** — sender-side at-least-once
  contract: append-before-send, `ack_through` retention, `replay_from` cursor.
* **`OrSet`** / **`WireLwwRegister`** — the OR-set (add-wins) and LWW liveness
  cells that ride the CrdtSync plane.
* **`SyncDriver`** + `IpcSink`/`IpcSource`/`Clock`/`SnapshotProvider` seams —
  the full-duplex loop (drain → retain-on-fail → receive/route → advertise ack).
* **`ResyncRequest`** / **`OutboxAck`** — two new `IpcMessage` control frames
  (FFI message kinds 4 / 5), JSON round-tripping like the state frames.

Replays the five canonical `conformance/reliable-sync/` fixtures plus the
SyncDriver loop-shape tests. Full parity — Python is now ✅ on both reliable-sync
coverage rows.

## 0.22.0

Unifies the keyed collections on **one** generic primitive `ReactiveMap[K, V, H]`
over a handle-kind seam (`#reactivemap`), mirroring lazily-spec v0.27.0 and the
`lazily-rs` reference. Two specializations are the concrete types a caller uses:

* **`ReactiveMap`** — the generic keyed reactive collection: reactive
  membership + order signals, `get_or_insert_with` (mint-on-access), `remove`,
  and atomic `move_to`/`move_before`/`move_after`.
* **`CellMap`** = `ReactiveMap` over the cell handle — adds cell-only `set` and
  eager value-minting (`entry` / `entry_with`).
* **`SlotMap`** = `ReactiveMap` over the slot handle — `get_or_insert_with` mints
  a derived slot on first access (lazy materialization); `materialize_all`
  pre-mints the keyset (eager). A slot's value is derived, so `SlotMap` has **no
  `set`**. There is no eager/lazy mode flag — eager is a pre-mint loop, lazy is
  mint-on-access.
* Execution-context flavors follow the same shape: `ThreadSafeReactiveMap` /
  `ThreadSafeCellMap` / `ThreadSafeSlotMap` (materialization confluence) and
  `AsyncReactiveMap` / `AsyncCellMap` / `AsyncSlotMap` (eventual transparency).

**BREAKING**: removes `ReactiveFamily`, `CellFamily`, `MaterializationMode` (and
its `.mode()` accessor), and the `*ReactiveFamily` types; the `FamilyHandle` seam
is renamed `MapHandle`. Behavior is unchanged — the three materialization
conformance suites (sync/thread-safe/async) pass against the same lazily-spec
fixtures (now `"model": "SlotMap"`). `CellMap` mutation is now `entry`/`set`
(was `insert`/`set_value`). `EntryKind` is retained.

## 0.21.0

Completes the **execution-context flavors** of the keyed reactive family and the
**family-granularity sync** layer, flipping the last three `lazily-spec`
feature-coverage rows to `✅` for Python — full parity across every row. Pinned
by the shared `lazily-spec/conformance/familysync/*.json` fixture and the Lean
`LazilyFormal.Materialization` (confluence), `AsyncMaterialization` (eventual
transparency), and `FamilySync` formal models.

* **`ThreadSafeReactiveFamily`** (`lazily.thread_safe_reactive_family`) — the
  `ThreadSafeContext` analog of `ReactiveFamily`. Keys map to per-entry `Cell`
  inputs / `slot` derived nodes whose writes are serialized through an owning
  `ThreadSafeContext`; the present set is guarded by its own lock. Carries the
  same eager/lazy, present-set-monotone, transparency laws **plus
  materialization confluence** — `_materialize_key` builds the node *outside* the
  family lock, then commits **first-writer-wins**, so a raced key keeps a single
  stable handle and any lock-admitted order yields the same present set and
  observed values (`materialize_present_comm` / `materialize_observe_comm`).
* **`AsyncReactiveFamily`** (`lazily.async_reactive_family`) — the async-context
  analog. Cell entries are always-resolved `Cell`s; derived entries are
  `AsyncSlot`s resolved asynchronously. The transparency law weakens to
  **eventual**: non-blocking `observe` returns `None` while pending and the
  canonical value once resolved (`await resolve(key)` drives it), never a stale
  value (`observe_pending_is_none` / `eventual_transparency` /
  `async_resolved_matches_sync`). The per-key factory stays the same sync
  callable across all three flavors.
* **Reactive family sync (`#lzfamilysync`)** — `CrdtPlaneRuntime` gains
  `register_family_lww` / `family_set_lww` / `family_value_lww` / `family_keys` /
  `membership_epoch`. A keyed op for a registered family whose entry is not yet
  known now **materializes on ingest** (membership grows, the epoch bumps, the
  value is adopted) instead of being dropped/mis-addressed — so a keyed family
  syncs as a unit: membership propagates, a later last-writer-wins update
  converges, re-ingest is idempotent, and a derived aggregate (count of `true`
  entries) converges across replicas.

## 0.20.0

Adds the **`ReactiveFamily`** vehicle and its **materialization mode**
(`#lzmatmode`) — the unified keyed reactive family of which the keyed cell
collection (`CellFamily`) is the input-cell specialization. This flips the
`lazily-spec` feature-coverage row *Reactive family (`ReactiveFamily`) — keyed
cell/slot family + materialization mode* to `✅` for Python, completing Python
parity across every coverage row. Pinned by the three shared
`lazily-spec/conformance/materialization/*.json` fixtures and the Lean
`LazilyFormal.Materialization` formal model.

* **`ReactiveFamily`** (`lazily.reactive_family`) — maps keys `K` to per-entry
  reactive nodes, abstracting over the entry's **handle kind** (Rust's
  `ReactiveFamily<K, V, H>`):
  * **Cell entries** (`EntryKind.CELL`) are input `Cell` nodes — **always
    materialized** regardless of mode (an input has no derivation to defer).
    `CellFamily` is this input-cell specialization.
  * **Slot entries** (`EntryKind.SLOT`) are derived `slot` nodes — what
    materialization mode governs.
* **`MaterializationMode`** — an axis orthogonal to entry kind that fixes *when*
  a derived node is allocated, never its value:
  * **`EAGER`** (default) — every derived node is allocated at build; a read is
    a direct node access.
  * **`LAZY`** (opt-in) — a derived node is allocated on its **first read**
    ("materialize on pull"), addressed by key; a never-read derived cell is
    never allocated. Lazy is a keyed overlay on the eager core, not a second
    engine — the first read of key `k` builds the *same* node the eager build
    would have, then caches it.
* **Constructors** — `ReactiveFamily.eager` / `.lazy` / `.new` (eager alias),
  plus `.cell_family` / `.slot_family`; reads via `get` (handle) / `observe`
  (value); present-set introspection via `is_present` / `present_keys` /
  `present_count` / `mode` / `entry_kind`.
* **Observational transparency** — a lazy read returns the value an eager read
  would (`observe_canonical`); materializing one node never changes another's
  observed value; the present set only *grows* (deferral, not de-allocation) and
  the lazy set is a subset of the eager set; reactivity (leaving off-viewport
  derived cells dirty) is orthogonal to materialization. Mirrors the Lean
  `Materialization` module and `lazily-rs/src/reactive_family.rs`.

## 0.19.0

Adds the **cross-process zero-copy transport** (`#lzzcpy`) — the pluggable
blob-backend adapter seam so large `Snapshot` / `Delta` / `CrdtSync` payloads
cross the IPC plane as small **descriptors** instead of being copied through the
wire codec. This flips the `lazily-spec` feature-coverage row *Cross-process
zero-copy transport (`BlobBackend` / shm / arrow)* to `✅` for Python. Pinned by
the shared `lazily-spec/conformance/delta_zero_copy_arrow.json` fixture and the
Lean `LazilyFormal.ZeroCopyTransport` formal model.

* **Pluggable blob backends** (`lazily.transport`) — the `BlobBackend` adapter
  seam: a backend *mints* a descriptor via `write(bytes)` and *resolves* it
  zero-copy via `read_view(descriptor)`. Three backends ship:
  * **`InProcessBackend`** — wraps `ShmBlobArena` for the single-address-space
    case (the FFI host / an editor plugin loaded in the same process).
  * **`ArrowBackend`** — holds Apache Arrow IPC-stream bytes; the descriptor's
    bytes *are* an Arrow IPC stream a columnar consumer imports zero-copy (bring
    your own `pyarrow` around the resolved `memoryview`).
  * **`ShmBackend`** — a named POSIX shared-memory region (via
    `multiprocessing.shared_memory`, `shm_open` + `mmap`) resolvable across
    processes on the same host: a second handle opened by name resolves a
    descriptor minted by the creator with no copy.
* **`backend` discriminator** — `ShmBlobRef` gains an optional `backend` field
  (`BlobBackendKind`: `shm` / `arrow` / `in_process`). It defaults to `shm` and
  is omitted on the wire when default, so every legacy descriptor validates and
  round-trips unchanged — the transport is a strict superset of the pre-existing
  shared-memory blob path. A receiver routes resolution by `backend`, so a `shm`
  descriptor never resolves in an Arrow table and vice versa.
* **Spill policy + `BlobRouter`** — `spill_message` replaces `Inline` /
  `Payload` payloads above a session-defined threshold with a `SharedBlob`
  descriptor across every message payload site (Snapshot node states, Delta
  `CellSet` / `SlotValue` / `NodeAdd`, `CrdtSync` op states); sub-threshold
  payloads stay inline. `BlobRouter` is the receiver-side multi-backend resolver
  that routes a descriptor to the matching backend by its `backend` kind.
* **Backend-agnostic guarantees** — the spill-then-resolve identity, backend
  isolation, ABA generation safety, and checksum-integrity laws hold uniformly
  for every backend that satisfies the contract (they are stated only over a
  backend's issued-blob table), mirroring the Lean `ZeroCopyTransport` model.

## 0.18.0

Adds the **reactive queue** (`QueueCell`) — the SPSC/MPSC reactive FIFO with a
pluggable `QueueStorage` backend — so the `lazily-spec` feature-coverage table is
`✅` across every row for Python. Pinned by the five canonical
`lazily-spec/conformance/collections/queuecell_*.json` fixtures and the Lean
`LazilyFormal.QueueCell` formal model.

* **Reactive queue** (`lazily.queue`) — `QueueCell`, a FIFO collection composed
  of reactive cells (not a new cell kind). SPSC primitive with an MPSC usage
  rule (multiple producers push inside one `batch`); no separate
  `MPSCQueueCell` type.
  * **Reader-kind invalidation** — the shell owns five reader-kind version cells
    (`head` / `len` / `is_empty` / `is_full` / `closed`) and invalidates by
    reader kind. A push to a non-empty queue does NOT invalidate the `head`
    reader; a pop does. This independence comes for free from the Cell `!=`
    (PartialEq) guard — after each op the shell re-derives each reader-kind cell
    from storage, and a cell whose value did not change is not invalidated.
  * **Pluggable storage** — the shell / storage split (`QueueStorage` protocol)
    keeps the reactive shell storage-agnostic. The default `VecDequeStorage`
    (unbounded deque) is the reference backend; a bounded variant
    (`with_capacity`) exposes reactive backpressure via `is_full`.
  * **Bounded reactive backpressure** — when the backend is bounded, `is_full`
    is a reactive read. A consumer's pop that transitions full → not-full
    invalidates `is_full` readers, enabling push-side effects to react to
    capacity recovery without polling.
  * **Closure lifecycle** — pop on closed+non-empty drains; pop on closed+empty
    returns `Closed` (distinct from `Empty`); push on closed is an error; close
    is idempotent and terminal.

## 0.17.0

Completes the two remaining `lazily-spec` feature rows for Python — the lossless
tree CRDT and the command / RPC message plane — so every row in the
`lazily-spec` Feature coverage table is `✅` for Python. Each is pinned by its
canonical `lazily-spec/conformance/` fixtures and a wire-schema compliance test.

* **Lossless tree CRDT** (`lazily.lossless_tree_crdt`) — `LosslessTreeCrdt`, the
  M1 syntax-agnostic core (#lzlosstree): a single rooted concrete-syntax tree
  whose leaves own every rendered byte (`render(tree) == source_text` for valid,
  invalid, and unknown source). Create / tombstone / intra-parent reorder /
  leaf-edit / split-leaf / merge-adjacent-leaves; leaf text embeds `TextCrdt`
  wholesale; child order is a fractional index (`key_between`); the clock is a
  Lamport `TreeOpId`.
  * **Dotted-frontier anti-entropy** — `TreeVersionFrontier` is a dot *set*
    (contiguous prefix + sparse holes), never a per-peer max, so a missing
    non-contiguous op stays representable and re-requestable (`diff` /
    `apply_update` converge through delivery gaps). UTF-8 byte-offset leaf edits
    (`byte_to_char`), idempotent + order-tolerant apply, and the externally-tagged
    wire codec (`tree_update_to_wire` / `tree_update_from_wire`).
  * **Concurrent merge convergence** — `Reorder` is LWW on `sort_stamp`;
    `Tombstone` is sticky-min; concurrent inserts in the same gap survive with
    byte-identical sort keys; incompatible shapes both survive (text
    preservation wins over semantic shape).
* **Command / RPC message plane** (`lazily.command`, `command-plane-v1`) — the
  additive sibling to `Snapshot` / `Delta` / `CrdtSync`: `CommandSubmit` /
  `CommandCancel` / `CommandEvents` / `CommandProjection`. The pure
  `CommandProjection` reducer folds frames + terminal `CausalReceipt` authority;
  `CommandRpcClient` is the derived RPC facade. Terminal authority is the receipt
  (never a transport ACK / `accepted` / `started` event); generation guards,
  idempotency, cancel-before-terminal-only, terminal-conflict-fails-closed, and
  reconnect equivalence all hold.

## 0.16.0

Full cross-language feature coverage. Every row in the `lazily-spec` Feature
coverage table is now `✅` for Python, each pinned by its canonical
`lazily-spec/conformance/` fixture and a property test.

* **Reactive graph completed** — sync `Effect` (`lazily.effect`: a side-effecting
  observer that reruns on dependency change, with cleanup-before-body and
  terminal `dispose`) plus the top-level `batch(run)` / `batch_context()`
  boundary (`lazily.batch`: coalesces cell writes into one invalidation + effect
  flush at the outermost boundary). `Cell` now tracks auto-discovered parents by
  identity (fixing a `functools.partial` non-deduplication fan-out leak) and
  routes changes through the batch-aware `notify_change` hook.
* **Cell-model layers** — `SemTree` (memoized semantic tree: ancestor-chain-only
  recompute + memo equality guard), `stable_id` (manufactured identity for text:
  anchors / content hashes / word-LCS alignment), `TextCrdt` (Fugue/RGA character
  CRDT + `#lztextsync` delta sync — `version_vector` / `delta_since` /
  `apply_delta`), `SeqCrdt` (move-aware sequence CRDT — single-LWW-reassignment
  moves converge without duplication).
* **CRDT registers** (`lazily.crdt_registers`) — `LwwRegister`, `MvRegister`
  (causal-context multi-value), `PnCounter`, and `CellCrdt` (CRDT cell that
  propagates into the reactive `Cell` plane, PartialEq-guarded after merge).
* **Distributed CRDT plane** (`lazily.crdt_plane`) — `CrdtPlaneRuntime`
  anti-entropy: state-based `CrdtOp` ingress, greatest-stamp-wins per
  `(node, key)`, `(node, stamp)` op-log dedup (idempotent redelivery), per-peer
  stamp frontier, and the causal-stability watermark.
* **Signaling plane** (`lazily.signaling`) — `SignalingFrame` envelope +
  `RoomCore` room state machine implementing the anti-spoof routing invariant
  (server-stamped `from`, self-excluding `welcome` roster, `to`/`from` never
  both present). `open` and `allowlist` permission modes.
* **State projection / mirror** (`lazily.projection`) — `StateMirror` projects a
  local reactive context onto the `Snapshot`/`Delta` wire plane with the
  value-mirror default and `PeerPermissions` omission filtering.
* **Instrumentation / benchmarks** (`lazily.benchmarks`) — `run_benchmarks()`
  micro-benchmarks for the reactive core, keyed reconciliation, `CellMap`,
  `TextCrdt`, and `CrdtPlaneRuntime`; runnable as `python -m lazily.benchmarks`.
* **Coverage table** — all Python marks `✅` (including the Reactive graph row);
  the table is regenerated from `lazily-spec/coverage.json` via
  `sync-coverage.mjs`.

## 0.15.0

`lazily-spec` causal-receipt compliance. The receipt plane is the generic
outcome projection for commands and effect requests keyed by a stable
`causation_id` — deliberately **not** a transport ACK. This release ships the
full `CausalReceipts` wire frame, the pure projection kernel that mirrors
`LazilyFormal.Receipt`, and replays the canonical
`lazily-spec/conformance/receipts/causal_receipts.json` fixture.

* **Causal receipts** (`#lzreceipts`) — `CausalReceipt` / `CausalReceipts`
  wire types (a standalone externally-tagged frame, not an `IpcMessage`
  variant, matching `receipts.json`) plus `ReceiptOutcome`
  (`observed`/`accepted` non-terminal, `applied`/`rejected` terminal).
  `to_wire()`/`from_wire()`/`encode_json()`/`decode_json()` round-trip the
  canonical fixture byte-for-byte.
* **`ReceiptProjection`** — the pure reducer that folds receipts into the
  authoritative outcome for one causation id, porting
  `LazilyFormal.Receipt.apply`: duplicate `receipt_id`s are idempotent
  no-ops, stale-generation receipts are discarded, non-terminal receipts
  record without conflicting, the first terminal receipt fixes the outcome,
  and a second *different* terminal outcome fails closed (no winner). The
  authority `current_generation` is caller-supplied; `from_receipts`
  defaults it to the max generation seen when replaying a frame without
  external authority.
* **Conformance + schema coverage** — `test_conformance.py` replays the
  `causal_receipts.json` fixture (parse, round-trip, projection assertions);
  `test_schema_compliance.py` proves lazily-py's `to_wire()` output validates
  against the normative `receipts.json` schema; `test_receipt_properties.py`
  replays every named `LazilyFormal.Receipt` theorem.

## 0.14.0

Full `lazily-spec` compute-layer compliance + `lazily-formal` test-suite
integration. This release closes the remaining `MUST` layers of the
`lazily-spec` conformance matrix (keyed collections, async reactive context,
thread-safe context, C-ABI FFI) by porting the corresponding Lean formal models
in `lazily-formal`, and makes `lazily-formal` part of the lazily-py test suite:
`lake build` gates the suite on every theorem still checking.

* **Keyed reactive collections** (`#lzcellmap`) — `CellMap` and `CellFamily`
  with the three independent reactive signals (per-entry value, set-membership,
  order), atomic move (`move_to`/`move_before`/`move_after`), and per-key
  identity-stable minting. A pure reorder bumps the order signal only — `len`/
  `contains` readers are not invalidated (the wire-level "a pure reorder MUST
  NOT invalidate set-membership readers" invariant). Ports
  `LazilyFormal.Collection`.
* **Ordered keyed tree** (`CellTree`) — per-node value reactivity and per-level
  membership/order reactivity; atomic child move preserves identity. Ports
  `LazilyFormal.Tree`.
* **Keyed reconciliation** (`reconcile_ops`) — the move-minimized
  `{insert, remove, move, update}` op set over a longest-increasing-subsequence
  (LIS) kernel: stable (LIS) keys never move, and a stable entry with an
  unchanged value is neither moved nor updated. Replays the
  `lazily-spec/conformance/collections/keyed_reconciliation_lis.json` fixture.
  Ports `LazilyFormal.Reconciliation`.
* **Async reactive context** (`#lzasync`) — `AsyncSlot` with the exact
  `Empty / Computing / Resolved / Error` lifecycle and revision-tracked
  stale-completion discard (a stale completion is never published); `AsyncEffect`
  with cleanup-before-body ordering and batch-boundary scheduling (invalidation
  only queues, never runs inline); disposal is terminal. Pure `step` kernels
  mirror `LazilyFormal.AsyncSlotState` / `AsyncEffect`; the theorems are
  replayed as property tests.
* **Thread-safe reactive context** (`ThreadSafeContext`) — a lock-serialized
  `batch` boundary that coalesces concurrent cell writes into one invalidation
  pass (the coalesced frontier); a singleton batch refines the single-threaded
  `setCell`. Ports `LazilyFormal.ThreadSafe`.
* **C-ABI FFI boundary** (`lazily.ffi`) — `LazilyFfiStatus` (0..5),
  `LazilyFfiMessageKind` (including `CrdtSync = 3`, as the spec mandates),
  `LazilyFfiBytes` (`ptr`/`len`), and `encode_message`/`decode_message`
  re-encoding `IpcMessage` to canonical JSON bytes byte-compatible with the
  Rust/Zig FFI boundaries.
* **lazily-formal test-suite integration** — `tests/test_formal_build.py` runs
  `lake build` over the sibling `lazily-formal` Lean model and fails the suite
  if any theorem regresses (skipped when `lake`/`lazily-formal` is absent, e.g.
  a standalone PyPI sdist). Property tests mirror the named Lean theorems for
  every new module.

## 0.13.0

Full `lazily-spec` wire-protocol compliance: the IPC types the binding was
missing versus `protocol.md` (matching the `lazily-rs` reference for
cross-language byte parity), plus a schema-compliance test proving lazily-py's
serializer output validates against the canonical `lazily-spec` JSON Schemas.

* **NodeKey** (`#lzwirekey`) — the optional wire-stable keyed address the spec
  explicitly names lazily-py as needing. `NodeKey`/`NodeKeyError` with
  `NODE_KEY_MAX_LEN`/`NODE_KEY_MAX_SEGMENTS` bounds (validated on construction
  and on the wire); optional `key` on `NodeSnapshot` (`.with_key`) and
  `DeltaOp.node_add`, omitted from JSON when `None` so pre-`key` encoders and
  existing conformance fixtures round-trip unchanged.
* **Distributed CRDT plane** (`#lzcrdtplane5a`) — `WireStamp`, `CrdtOp`
  (`new`/`keyed`, state-based CvRDT), `CrdtSync` (frontier + ops,
  `filter_readable`). `IpcMessage` gains a third variant `{"CrdtSync": …}`.
* **Capability negotiation** — `CapabilityHandshake` +
  `PROTOCOL_ID`/`PROTOCOL_MAJOR_VERSION`; `is_compatible_with` fails closed on
  protocol id / major version / codec / `ordered_reliable` disagreement.
* **Schema compliance** — `tests/test_schema_compliance.py` validates
  lazily-py's `to_wire()` output (Snapshot, Delta with keyed NodeAdd, CrdtSync)
  against the sibling `lazily-spec/schemas`, closing the binding↔schema loop.

## 0.12.0

* Add `StateChart` — a full Harel/SCXML state-chart interpreter backed by a
  reactive `Cell[frozenset[str]]`, conforming to `lazily-spec`
  `docs/state-charts.md` and the Lean `StateChart` formal model
  (`lazily-formal`). The `Cell`'s `!=` (PartialEq) guard suppresses no-op
  self-transitions, and any `Slot`/`Signal` reading `configuration()` /
  `active_leaves()` / `matches()` is invalidated on a real transition.
* Implemented subset: compound states, orthogonal (parallel) regions, shallow +
  deep history, entry/exit/transition actions, named guards (fail-closed),
  external + internal transitions, `final`-as-leaf. `run` actions and
  `{expr: ...}` context guards are rejected explicitly per spec.
* Replays all `lazily-spec/conformance/statechart/*.json` fixtures (flat cycle,
  hierarchical player, guarded door, parallel regions, shallow/deep history,
  entry/exit actions).
* Exports `ChartDef` and `StateChart`.

## 0.11.1

* Docs: refresh README — Signal family, `lazily.ipc` wire protocol, ShmBlobArena,
  and the lazily language family.
* Docs: reference the sibling `lazily-spec` (wire protocol + conformance) and
  `lazily-formal` (Lean 4 formal model) projects.

## 0.11.0

* Add `StateMachine[S, E]` — a finite state machine backed by a reactive `Cell`,
  so any `Slot` that reads the machine's state is invalidated on transition.
  Mirrors `lazily-rs` `StateMachine<S, E>` and `lazily-zig` `StateMachine(S, E)`.
* Add `ShmBlobArena` host capability to `lazily.ipc` — ports the `lazily-rs`
  `ShmBlobArena<B>` shared-memory blob arena and mirrors `lazily-zig`
  `ShmBlobArena`: a `bytearray`-backed arena with a 40-byte header
  (`LZSH` magic + FNV-1a-64 checksum), append-only writes with wraparound, and
  full read validation. A Python process can now host blob payloads (not just
  carry `ShmBlobRef` descriptors). Descriptors are byte-compatible with the
  Rust and Zig arenas. Exports `ShmBlobArena`, `ShmBlobArenaError` (with six
  variant subclasses), and `SHM_BLOB_HEADER_LEN`.
* Add `lazily.ipc`: the language-agnostic `lazily-spec` IPC wire protocol
  (Snapshot / Delta) with JSON byte-compatible with the Rust and Zig bindings —
  `IpcMessage`, `Snapshot`, `Delta`, the 7 `DeltaOp` variants, `NodeState` /
  `IpcValue` / `ShmBlobRef`, epoch sequencing (`apply_status` / resync), and a
  default-deny `PeerPermissions` boundary that omits non-readable nodes.
* Add conformance tests against the canonical `lazily-spec/conformance` fixtures
  (vendored under `tests/conformance/` for standalone CI).
* Add the eager `Signal` primitive — the third member of the
  `Slot → Cell → Signal` family (`signal` / `signal_def` decorators, eager
  recompute with a memo guard, `dispose()` reverts to lazy).
* Fix `RecursionError` when a `Signal` reads a `Slot` dependency: `Slot.__call__`
  no longer fires `touch()` during computation (computation must not cascade
  invalidation), and `Slot.reset` clears subscribers before notification so a
  subscriber-triggered reset cannot mutually recurse.

## D.10.0

* Fix cell type inference.
* Drop support for python version <3.12.
* Use uv + mise for development package management.

## 0.9.0

* Add type-aware context to slot and cell functions.
* The ctx argument type can be enforced via mypy.
* mypy tests now pass

## 0.8.0

* Add Cell.get() as an alias to Cell.value
* Add Cell.set(value) as an alias to Cell.value = value

## 0.7.0

* Add @slot_def and @cell_def decorators...which both take a resolve_ctx callable to resolve the ctx object.
* Allows for calling a slot function with an object that contains the ctx, such as a Request or Graphene
  GraphQLResolveInfo.

## 0.6.0

* cell() is supported with a default value of None.

## 0.5.0

* Slot now support subscriptions. Resetting a slot will also reset it's descendents.

## 0.4.0

* BREAKING CHANGE: Rename Cell to Slot and @cell to @slot.
* BREAKING CHANGE: Cell is now a subscribable that resets a parent Slots when it changes.

## 0.3.0

* BREAKING CHANGE: Rename be to cell and Be to Cell.

## 0.2.0

* BREAKING CHANGE: Remove be_class due to complexity. If a subclass of Be is needed, then users are encouraged to use
  create a singleton instance of the subclass.

## 0.1.0

* Initial release
