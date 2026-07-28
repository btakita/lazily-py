# lazily

Lazy reactive primitives for Python — the **Cell kernel** (`Source` / `Computed`
cells, plus `Effect`) with automatic dependency tracking and cache invalidation,
plus the language-agnostic `lazily-spec` wire protocol for mirroring graph state
across processes and languages.

[![PyPI](https://img.shields.io/pypi/v/lazily.svg)](https://pypi.org/project/lazily/)

## Overview

`lazily` is the Python port of the **Cell kernel** (`#lzcellkernel`): two value
kinds — `Source` and `Computed` — plus the value-less `Effect` sink. `Cell` is
the value node the `Source` handle is bound to.

- **`Source`** — a value written from *outside* (`set` / `merge`); the writable
  kind. Construct with `source` / `cell` (handle `Source`, native class `Cell`,
  slot `SourceSlot`, native class `CellSlot`). A [`MergeCell`](#merge-algebra) is a `Source`
  whose write folds under a non-`KeepLatest` policy (`Cell ≡ Source(KeepLatest)`).
- **`Computed`** — a value computed from *upstream*, via a compute function.
  Construct with `computed(ctx, f)`. **Guarded by default** and **lazy by
  default**.
- **`Effect`** — a value-less sink outside the hierarchy; nothing can depend on
  it.

A `Computed` is **lazy by default**: dependents are marked dirty on invalidation
but only recompute when accessed. When you need eager push-style semantics —
recompute immediately, observe `v1 → v2` with no unset window — make it
**eager**: `computed(ctx, f).eager()`. Going eager attaches a scheduled puller
`Effect` over the backing memo (eagerness is graph state — an `_eager` bit plus a
side table — not a distinct type), so N writes inside one `batch` re-materialize
the computed **once**, at the flush. **Every cell is guarded** — an equal
recompute is suppressed by the `PartialEq` guard (matching TC39
`Signal.Computed`), so unchanged values never cascade downstream work. There is
**no unguarded derived mode**: `computed` *is* the guarded derived constructor,
and the former separate `memo` construction is retired.

> **Migration note (v2 Cell kernel, `#lzcellkernel`).** The v1 value vocabulary
> is **removed**: `Signal` / `signal` / `signal_def`, `formula` / `formula_def` /
> `FormulaCell`, `SourceCell` / `SourceCellSlot`, and the `.drive()` / `.undrive()`
> / `is_driven` / `is_active` methods are gone. Use the v2 spelling instead — an
> eager `Computed` is `computed(ctx, f).eager()`; the lifecycle is
> `.eager()` / `.lazy()` / `.is_eager()`. The construction sugar `cell` /
> `cell_def` is **deprecated** in favour of `source` / `source_def`, and the
> derived-value `slot` decorator is **deprecated** in favour of the guarded
> `computed` (`slot_def` remains as the storage-sense factory). Python has no
> compile-time read/write split (see the design's §4): the split is a
> **convention** — a `Source` has `set` / `merge`, a `Computed` does not — not a
> runtime gate.

There is **no dedicated `Context` class** — a plain `dict` is the context, so the
Rust reference's `ctx.computed(f)` is spelled `computed(ctx, f)` here. `Slot` is
retained as the **storage position** that holds a node: a `Slot` uses itself as
the dictionary key that caches its value, so any dict works as the reactive
"world" (`lazily-spec` §5.0 "`Slot`-as-storage"). It is the Python analog of
`lazily-rs`'s surviving storage-sense `Slot`; construct it directly with
`Slot(callable=…)` when a raw storage node is genuinely needed.

## Feature coverage

The full `lazily` capability set across every binding. Legend: ✅ shipped ·
`~` partial · `—` absent or not applicable. The canonical matrix with per-cell
notes and platform carve-outs lives in
[`lazily-spec` § Cross-Language Coverage](https://github.com/lazily-hub/lazily-spec/blob/main/docs/coverage.md).

<!-- coverage-table:start -->
| Feature | Rust | Python | Kotlin | JS | Dart | Zig | Go | C++ | C# |
| --------- | :----: | :------: | :------: | :--: | :----: | :---: | :--: | :---: | :--: |
| Reactive graph — two cell kinds (nodes `SourceCell` / `ComputedCell`; handles `Source<T, M>` / `Computed<T>`) + `Effect` sink + eager `Computed` (`computed().eager()`) / all cells guarded / batch | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Keyed-map materialization (`ComputedMap`) — mint-on-access derived slots: transparency + deferral (`#lzmatmode`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Thread-safe keyed map (`ThreadSafeComputedMap`) — `Send + Sync` + materialization confluence (`#lzmatmode`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Async keyed map (`AsyncComputedMap`) — eventual transparency (`#lzmatmode`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Keyed-map sync — membership propagation + materialize-on-ingest + derived-aggregate transparency (`#lzfamilysync`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Thread-safe context (lock-backed) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Async reactive context | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Flat state machine | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Harel state charts | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Keyed reactive maps (`ReactiveMap`: `SourceMap` / `ComputedMap`) + `SourceTree` + reconcile | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `ReactiveMap` **Core surface** — single-threaded flavor (cell-model.md § Core surface vs. binding extensions) | ✅ | ✅ | ✅ | ✅ | ✅ | ~ | ✅ | ✅ | ✅ |
| `ReactiveMap` **Core surface** — thread-safe flavor (ordering + membership reactivity) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ~ |
| `ReactiveMap` **Core surface** — async flavor (ordering + membership reactivity) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ~ |
| Atomic ordered move replayed against **all three flavors** (`cellmap_atomic_move` + `cellmap_independence`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ~ |
| Memoized semantic tree (`SemTree`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Stable-id alignment (manufactured identity) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Reactive queue (`QueueCell` SPSC/MPSC + `QueueStorage` adapter) **Core surface** — single-threaded flavor | ✅ | ✅ | ✅ | ~ | ✅ | ~ | ✅ | ✅ | — |
| Reactive queue (`QueueCell` SPSC/MPSC + `QueueStorage` adapter) **Core surface** — thread-safe flavor (reader kinds + closure lifecycle) | ✅ | — | — | — | — | — | — | — | — |
| Reactive queue (`QueueCell` SPSC/MPSC + `QueueStorage` adapter) **Core surface** — async flavor (reader kinds + eventual transparency) | ✅ | — | — | — | — | — | — | — | — |
| Broadcast topic (`TopicCell`) **Core surface** — single-threaded flavor — independent cursors + durable replay + safe GC (`#lztopiccell`) | ✅ | ✅ | ✅ | ~ | ✅ | ~ | ✅ | ✅ | — |
| Broadcast topic (`TopicCell`) **Core surface** — thread-safe flavor (reader kinds + closure lifecycle) | ✅ | — | — | — | — | — | — | — | — |
| Broadcast topic (`TopicCell`) **Core surface** — async flavor (reader kinds + eventual transparency) | ✅ | — | — | — | — | — | — | — | — |
| Competing-consumer work queue (`WorkQueueCell`) **Core surface** — single-threaded flavor — exclusive leases + ack/nack + redelivery + DLQ (`#lzworkqueue`) | ✅ | ✅ | ✅ | ~ | ✅ | ~ | ✅ | ✅ | — |
| Competing-consumer work queue (`WorkQueueCell`) **Core surface** — thread-safe flavor (reader kinds + closure lifecycle) | ✅ | — | — | — | — | — | — | — | — |
| Competing-consumer work queue (`WorkQueueCell`) **Core surface** — async flavor (reader kinds + eventual transparency) | ✅ | — | — | — | — | — | — | — | — |
| Merge algebra + `Source<T, M>` — associative `MergePolicy` (`KeepLatest`/`Sum`/`Max`/`SetUnion`/`RawFifo`), `Cell ≡ Source<KeepLatest>`, read-any-cell/write-`Source` split (`#relaycell`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| RelayCell — conflating relay + `BackpressurePolicy` + `SpillStore` + `Transport` + Inbox/Outbox + Rate/Window/Expiry/Priority/keyed policies (`#relaycell`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Free-text character CRDT (`TextCrdt`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `TextCrdt` delta sync (`version_vector` / `delta_since` / `apply_delta`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `CrdtTree` lossless document contract (`#lzcrdttree`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Move-aware sequence CRDT (`SeqCrdt`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lossless tree CRDT core (`LosslessTreeCrdt`, M1) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lossless tree — dotted-frontier anti-entropy | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lossless tree — concurrent merge convergence | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Registers (LWW / MV) + `PnCounter` + `CellCrdt` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| IPC wire — `Snapshot` + `Delta` + `CrdtSync` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Shared-memory blob path (`ShmBlobArena`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Cross-process zero-copy transport (`BlobBackend` / shm / arrow) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Distributed CRDT plane (`CrdtPlaneRuntime` / anti-entropy) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Reliable sync — resync coordinator + at-least-once durable outbox + OR-set/LWW liveness (`#lzsync`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Storage-independent durable outbox (`OutboxStore` + shared outbox protocol; SQLite/Room/IndexedDB/file adapters) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Reliable-sync transport seam + full-duplex `SyncDriver` loop (`IpcSink`/`IpcSource`, `#sync-driver`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Distributed plane — WebRTC transport + signaling | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| State projection / mirror | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Causal receipts (`CausalReceipts` outcome projection) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Message-passing + RPC command plane (`command-plane-v1`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| C-ABI FFI boundary | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Permission boundary (`PeerPermissions` / `RemoteOp`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Capability negotiation (`SessionHandshake`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Instrumentation / benchmarks | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Temporal sources — `TimerCell` / `IntervalCell` / `CronCell` / `DeadlineCell` over a logical clock (`#lztime`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Rate-shaping operators — `DebounceCell` / `ThrottleCell` / `SampleCell` / `ProbabilisticSampleCell` (`#lzrateshape`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Membership + failure detection — `MembershipCell` (SWIM + Phi-accrual) / `PeerSet` / `PeerChangeEvent` (`#lzmemb`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Distributed coordination — `LeaseCell` / `LeaderCell` / `LockCell` / `SemaphoreCell` / `BarrierCell`+`QuorumCell` (`#lzcoord`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Presence + ephemeral plane — `PresenceCell` / `AwarenessCell` / `EphemeralCell` + `Ephemeral`/`Durable` markers (`#lzpresence`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Stream windowing — `TumblingWindow` / `SlidingWindow` / `SessionWindow` over the merge algebra (`#lzwindow`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Fault tolerance — `CircuitBreakerCell` / `RetryPolicyCell` / `BulkheadCell` / `TimeoutCell` (`#lzresilience`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Embedded-service plane — `HealthCell` / `ReadinessCell` / `DiscoveryCell` / `ServiceRegistry` (`#lzservice`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
<!-- coverage-table:end -->

## Installation

```
pip install lazily
```

## Example usage

```python
from lazily import SourceSlot, source, slot

# Sources hold a value that can be updated.
name = SourceSlot[dict, dict, str]()


# Slots are functions that depend on sources and other slots.
@slot
def greeting(ctx: dict) -> str:
    print("Calculating greeting...")
    return f"Hello, {name(ctx).value}!"


# A SourceSlot can also have a default value.
@source
def response(ctx: dict) -> str:
    return "How are you?"


@slot
def greeting_and_response(ctx: dict) -> str:
    print("Calculating greeting_and_response...")
    return f"{greeting(ctx)} {response(ctx).value}"


ctx = {}

name(ctx).value = "World"

# First access: runs the function
print(greeting(ctx))
# Calculating greeting...
# 'Hello, World!'

# Second access: uses cache (no print)
print(greeting(ctx))
# 'Hello, World!'

# Dependencies also access cached values
print(greeting_and_response(ctx))
# Calculating greeting_and_response...
# 'Hello, World! How are you?'

# Dependencies also cached
print(greeting_and_response(ctx))
# 'Hello, World! How are you?'

# Update cell: invalidates cache
name(ctx).value = "Lazily"

# Access again: re-runs the function
print(greeting_and_response(ctx))
# Calculating greeting_and_response...
# Calculating greeting...
# 'Hello, Lazily! How are you?'

# Another access: uses cache
print(greeting_and_response(ctx))
# 'Hello, Lazily! How are you?'
```

## Core Concepts

### Context

A plain `dict` is the context. It owns all cached Slot values; Slots store their
cache under themselves as keys. The current implementation is single-threaded —
create one dict per reactive graph.

### Slot

A Slot wraps a compute function `(ctx) -> T`; the result is cached after first
access. Dependencies are discovered automatically via a global `slot_stack` —
any Slot or Cell read during computation becomes a dependency and re-subscribes
on every recompute, so conditional branches update the dependency graph with no
manual cleanup. When a dependency invalidates, the Slot only marks its cache
dirty; it does **not** recompute until called again.

| Type | Purpose |
|------|---------|
| `BaseSlot[C_in, C_ctx, T]` | Base slot without subscriber support |
| `Slot[C_in, C_ctx, T]` | Storage-sense slot with dependency tracking and invalidation |
| `slot` | **Deprecated** — use `computed` (guarded) or `Slot(callable=…)` (storage) |
| `slot_def(resolve_ctx)` | Storage-sense decorator factory for a custom context resolver |

### Source cell

A `Source` cell (native class `Cell`) holds a mutable value. Reading `cell.value`
inside a `Computed` or `Effect` auto-subscribes that reader; assigning
`cell.value = x` (or `cell.set(x)`) compares old and new via `!=` and, only if
changed, cascades invalidation to dependents. Construct with `source` (the v1
`cell` name is deprecated).

| Type | Purpose |
|------|---------|
| `Cell[T]` / `Source[T]` | Mutable source value with subscription support |
| `SourceSlot[C_in, C_ctx, T]` (native class `CellSlot`) | Slot that returns a `Source` cell |
| `source` | Decorator: `SourceSlot` with an identity resolver (canonical) |
| `source_def(resolve_ctx)` | Decorator factory for a custom context resolver |
| `cell` / `cell_def` | **Deprecated** v1 aliases of `source` / `source_def` |

### Eager Computed

An **eager** `Computed` — `computed(ctx, f).eager()` — is the counterpart to a
lazy `Computed`. Where a lazy cell marks itself dirty on invalidation and
recomputes on the next read, an eager one recomputes *the instant a dependency is
invalidated*, before the mutating call returns. The value is always materialized,
so observers never see an intermediate unset value.

```python
from lazily import SourceSlot, computed

n = SourceSlot[dict, dict, int]()

ctx: dict = {}
n(ctx).value = 1

doubled = computed(ctx, lambda c: n(c).value * 2).eager()  # eager: materialized now
print(doubled.value)   # 2

n(ctx).value = 5       # doubled recomputes immediately
print(doubled.value)   # 10 — already current, no lazy read needed
```

An eager `Computed` is **composed from existing primitives**, not a parallel
engine: a memoized backing `Slot` supplies glitch-free, guarded recomputation,
and a small puller `Effect` re-materializes it after every invalidation to supply
the eagerness. It inherits the guard (an equal recompute suppresses the
downstream cascade). `.lazy()` (or `.dispose()`) removes the eager puller — the
value stays readable but reverts to lazy (recompute-on-read) behavior.

| Method | Purpose |
|------|---------|
| `computed(ctx, f)` | Lazy, guarded derived value bound to a context |
| `computed(ctx, f).eager()` | The eager form (idempotent; returns the same handle) |
| `.lazy()` / `.is_eager()` | Revert to lazy / query eagerness |
| `computed_def(resolve_ctx)` | Decorator factory for a context-cached lazy `Computed` |

### StateMachine

`StateMachine[S, E]` is a finite state machine backed by a reactive `Cell`, so
its `state` participates in dependency tracking like any other reactive value.
Construct it with `StateMachine(ctx, initial, transition)` where `transition` is
a pure `(state, event) -> next_state | None` (returning `None` rejects the
event). `send(event)` returns whether the transition was accepted; a
self-transition to an equal state is accepted but suppressed by the Cell's
`PartialEq` guard.

### Reactive collections, async, and thread-safe contexts

lazily-py also implements the `lazily-spec` compute-layer `MUST`s, each ported
from its Lean formal model in [`lazily-formal`](https://github.com/lazily-hub/lazily-formal):

- **`ReactiveMap` / `SourceMap` / `ComputedMap` / `SourceTree`** — keyed reactive
  collections (`#reactivemap`) with independent value/membership/order signals and
  atomic move. One generic `ReactiveMap` over a handle kind; `SourceMap` (input
  cells, adds `set` + eager `entry`) and `ComputedMap` (derived slots, lazy
  `get_or_insert_with` + eager `materialize_all`) are its specializations.
- **`QueueCell`** — a reactive FIFO queue (SPSC primitive with an MPSC-via-`batch`
  usage rule) with a pluggable `QueueStorage` backend. Reader-kind invalidation
  (head/len/is_empty/is_full/closed), bounded reactive backpressure via `is_full`,
  and the closure lifecycle (drain / Closed-distinct-from-Empty / idempotent).
- **`reconcile_ops`** — move-minimized keyed reconciliation (LIS kernel).
- **`AsyncSlot` / `AsyncEffect`** — the async slot lifecycle with stale-completion
  discard, and cleanup-before-body effect scheduling.
- **`ThreadSafeContext`** — a lock-serialized `batch` boundary that coalesces
  writes into one invalidation pass.
- **`lazily.ffi`** — the C-ABI FFI boundary (`LazilyFfiStatus`,
  `LazilyFfiMessageKind` incl. `CrdtSync = 3`, `LazilyFfiBytes`).

The test suite gates on `lazily-formal`'s `lake build` (every theorem checks)
and mirrors the named Lean theorems as property tests. See `SPEC.md` for the
full compliance surface.

## Reactive queue — `lazily.queue`

`QueueCell` is a FIFO collection composed of reactive cells — **not a new cell
kind** — that adds queue semantics (push to tail, pop from head) to the reactive
graph. `ThreadSafeQueueCell` and `AsyncQueueCell` carry the same Core surface;
the topic and work-queue families likewise ship `ThreadSafe*` and `Async*`
shells. Nothing in the async family is async-coloured: storage reads, cursor
advances, and lease decisions return plain values.

The reactive shell wraps a pluggable `QueueStorage` backend (default
`VecDequeStorage`) and owns demand-driven reader-kind handles. It invalidates
exactly the handles whose values changed — a push to a non-empty queue does NOT
invalidate the `head` reader, while a pop does. Thread-safe shells serialize
their whole public operation through a `ThreadSafeContext`; the same canonical
fixture corpus and invalidation matrices replay against all three flavors.

```python
from lazily import QueueCell, QueuePopError, batch

ctx = {}
q: QueueCell[str] = QueueCell(ctx)

q.try_push("a")
q.try_push("b")
assert q.head() == "a"
assert q.len() == 2
assert q.try_pop() == "a"

# Bounded queue → reactive backpressure via is_full.
bq = QueueCell[int].with_capacity(ctx, 2)
bq.try_push(1)
bq.try_push(2)
assert bq.is_full()
assert bq.try_push(3).label == "Full"   # reject at capacity
assert bq.try_pop() == 1
assert not bq.is_full()                  # pop freed a slot → is_full reader invalidated

# MPSC: multiple producers push inside one batch → one invalidation pass.
batch(lambda: (q.try_push("p1"), q.try_push("p2")))

# Closure: pop on closed+empty returns Closed (distinct from Empty).
q.close()
assert q.is_closed()
assert q.try_push("x").label == "Closed"
```

### Competing-consumer work queue

`WorkQueueCell` provides exclusive FIFO claims with visibility deadlines,
worker-scoped acknowledgements, tail retries, and bounded dead-letter handling.
Item ids survive retries while each claim gets a fresh delivery id.

```python
from lazily import WorkQueueCell

work = WorkQueueCell[str](ctx, visibility_timeout=10, max_deliveries=3)
work.push("job")
delivery = work.claim("worker-a", 100)
assert delivery is not None
assert work.ack("worker-a", delivery.delivery_id)
```

The reader-kind independence law is explicit: every successful operation
derives its changed-reader set from the before/after state and resets those
memoized handles in one batch. Unchanged reader kinds remain warm.

## IPC — the `lazily-spec` wire protocol

`lazily.ipc` implements the language-agnostic [`lazily-spec`](https://github.com/lazily-hub/lazily-spec)
wire protocol, so a Python graph's state can be mirrored to remote observers
across processes and languages. The JSON encoding is **byte-compatible** with the
Rust (`lazily-rs`), Zig (`lazily-zig`), and TypeScript (`@lazily/signaling`)
bindings, and is validated against the canonical `lazily-spec/conformance`
fixtures (vendored under `tests/conformance/`).

Two message kinds flow over any transport (WebSocket text, WebRTC data, FFI
buffer):

- **`Snapshot`** — the full graph state at an epoch (`nodes`, `edges`, `roots`).
- **`Delta`** — an ordered batch of the 7 `DeltaOp` variants (`CellSet`,
  `SlotValue`, `Invalidate`, `NodeAdd`, `NodeRemove`, `EdgeAdd`, `EdgeRemove`)
  applied with epoch sequencing and fail-closed resync.

```python
from lazily import (
    Snapshot, NodeSnapshot, EdgeSnapshot, ShmBlobRef,
    Delta, DeltaOp, IpcMessage,
)

# Build and serialize a snapshot — encode_json() returns transport-agnostic bytes.
snap = Snapshot(
    epoch=7,
    nodes=[
        NodeSnapshot.payload(1, "i32", bytes([1, 2, 3])),
        NodeSnapshot.opaque(2, "opaque-type"),
        NodeSnapshot.shared_blob(3, "text/plain", ShmBlobRef(0, 16, 1, 7, 999)),
    ],
    edges=[EdgeSnapshot(2, 1), EdgeSnapshot(3, 1)],
    roots=[1, 2],
)
wire = IpcMessage.of_snapshot(snap).encode_json()
assert IpcMessage.decode_json(wire).snapshot == snap

# An incremental delta carrying mutations.
delta = Delta.next(40, [
    DeltaOp.cell_set(1, bytes([10])),
    DeltaOp.invalidate(3),
])
IpcMessage.of_delta(delta).encode_json()
```

A `PeerPermissions` boundary gates what is shared: it is **default-deny**, so only
nodes a peer is explicitly allowed to read are serialized into a snapshot or
delta — non-allowlisted nodes are omitted entirely.

### Shared-memory blobs — `ShmBlobArena`

`ShmBlobArena` lets a Python process **host** blob payloads (not just carry
`ShmBlobRef` descriptors). It is a `bytearray`-backed, append-only arena with a
40-byte header (`LZSH` magic + FNV-1a-64 checksum) and wraparound, ported from
the `lazily-rs` `ShmBlobArena<B>` and byte-compatible with the Rust and Zig
arenas. The module exports `ShmBlobArena`, `ShmBlobArenaError` (with its variant
subclasses), and `SHM_BLOB_HEADER_LEN`.

## Lossless tree CRDT — `lazily.lossless_tree_crdt`

`LosslessTreeCrdt` (#lzlosstree) is a single rooted concrete-syntax tree whose
**leaves own every rendered byte** — `render(tree) == source_text` for valid,
invalid, and unknown source alike. Where `TextCrdt` is a flat lossless floor,
this is the structured tree that can itself be the wire authority. Element nodes
own structure only; all text lives in leaf nodes tagged `Token` / `Trivia` /
`Raw` / `Error`, so unknown/invalid spans round-trip exactly as `Raw`/`Error`
leaves rather than being discarded.

```python
from lazily import LeafKind, LosslessTreeCrdt, SeedElement, SeedLeaf
from lazily.lossless_tree_crdt import ROOT

tree = LosslessTreeCrdt(peer=1)
heading = tree.create_node(ROOT, None, SeedElement("heading"))
tree.create_node(heading, None, SeedLeaf(LeafKind.TOKEN, "# "))
title = tree.create_node(heading, None, SeedLeaf(LeafKind.RAW, "Título"))
assert tree.render() == "# Título"

# Op-based delta sync: fork, diverge, converge through a dotted frontier.
other = tree.fork(peer=2)
other.edit_leaf(title, 0, 0, "X")
tree.apply_update(other.diff(tree.frontier()))
assert tree.render() == other.render()
```

Leaf text embeds `TextCrdt` wholesale; child order is a fractional index
(`key_between`); the clock is a Lamport `TreeOpId`. Anti-entropy is op-based over
a **dotted, non-contiguous version frontier** (`TreeVersionFrontier`) — a dot
*set* (contiguous prefix + sparse holes), never a per-peer max, so a missing
interior op stays representable and re-requestable. Leaf-local wire offsets are
UTF-8 bytes (`byte_to_char`). The wire codec (`tree_update_to_wire` /
`tree_update_from_wire`) validates against `lazily-spec`'s
`lossless-tree-delta.json`, and all nine `conformance/lossless-tree/` fixtures
replay.

## Command / RPC message plane — `lazily.command`

`command-plane-v1` is an **additive sibling** to `Snapshot` / `Delta` /
`CrdtSync`: four evented frames (`CommandSubmit` / `CommandCancel` /
`CommandEvents` / `CommandProjection`) that carry command traffic, not cell
state. lazily owns the envelope; the namespace owns the `IpcValue` payload, which
lazily never decodes.

The single hard rule: **terminal authority is the causal receipt.** A command is
terminal only when a terminal `CausalReceipt` for its `command_id` folds in
(`applied`, or `rejected` — including the `cancelled` / `superseded` /
`timed_out` reasons). `observed` / `accepted` / `started` events are progress
only; a transport ACK is never terminal.

```python
from lazily import (
    CommandPolicy, CommandRpcClient, CommandSubmit, DedupePolicy,
    applied_receipt,
)
from lazily.ipc import IpcValue

class Transport:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)

client = CommandRpcClient(Transport())
cmd_id = client.submit(CommandSubmit(
    command_id="cmd-1", causation_id="cmd-1", source="plugin",
    target="controller", namespace="agent-doc", name="editor_route",
    authority_generation=1, idempotency_key="doc:run", deadline_ms=0,
    policy=CommandPolicy(DedupePolicy.SAME_IDEMPOTENCY_KEY, False, True),
    payload_type="agent-doc.editor_route.v1", payload_hash="sha256:…",
    payload=IpcValue.of(b"{…}"),
    required_features=["command-plane-v1"],
))
# `call` resolves ONLY on a terminal receipt — never an ACK or `accepted`.
client.ingest_receipt(applied_receipt("rcpt-1", cmd_id, "controller", 1))
assert client.poll_call(cmd_id).kind.value == "resolved"
```

`CommandProjection` is the pure reducer (generation guards, idempotency,
cancel-before-terminal-only, terminal-conflict-fails-closed, reconnect
equivalence); `CommandRpcClient` is the derived RPC facade. The wire codec
validates against `lazily-spec`'s `message-passing.json`, and all eight
`conformance/message-passing/` fixtures replay.

## Benchmarks

Wall-clock benchmarks live in [`BENCHMARKS.md`](BENCHMARKS.md), covering both
the in-library micro-suite (reactive core, keyed reconciliation, `SourceMap`,
`TextCrdt`, CRDT plane) and a large spreadsheet-shaped **scale** suite that
mirrors the lazily-rs / lazily-go `scale` groups (`N` input cells + `N` formula
slots, `formula[i] = input[i] + input[i-1]`). The scale suite is measured up to
a full **10,000,000-cell Google Sheets workbook** (`N = 5,000,000`); a one-cell
edit plus a 1,000-cell viewport read stays in the ~75 µs range regardless of
sheet size, because the lazy pull model recomputes only the ~2 formulas that
read the edited input.

```bash
make bench          # micro-suite
make bench-scale    # scale suite (default N = 1,000,000)

# or directly, with a custom size:
uv run python -m lazily.benchmarks
LAZILY_SCALE_N=5000000 uv run python -m lazily.scale_bench   # 10M-cell workbook
```

See [`BENCHMARKS.md`](BENCHMARKS.md) for the full results, hardware, and honest
notes on CPython's per-node overhead.

## The lazily family

`lazily-py` is one binding in a cross-language reactive family that shares the
`lazily-spec` wire protocol:

| Binding | Language | Package |
|---------|----------|---------|
| [`lazily-rs`](https://github.com/lazily-hub/lazily-rs) | Rust | `lazily` (crates.io) |
| **`lazily-py`** | Python | `lazily` (PyPI) |
| [`lazily-zig`](https://github.com/lazily-hub/lazily-zig) | Zig | GitHub |
| [`@lazily/signaling`](https://github.com/lazily-hub/lazily-js) | TypeScript / Cloudflare Worker | npm |
| [`lazily-spec`](https://github.com/lazily-hub/lazily-spec) | — | wire protocol + conformance fixtures |
| [`lazily-formal`](https://github.com/lazily-hub/lazily-formal) | — | Lean 4 formal model (FSM kernel + Harel state chart) |

See [`lazily-spec`](https://github.com/lazily-hub/lazily-spec) for the canonical
Snapshot/Delta schemas, the IPC Lean proofs of the epoch/memo/batch invariants,
and the conformance fixtures every IPC-capable binding validates against. The
language-agnostic formal model — the flat FSM kernel and the full Harel state
chart — lives in [`lazily-formal`](https://github.com/lazily-hub/lazily-formal).

## Development

This project uses [`uv`](https://github.com/astral-sh/uv). Run the local
CI-equivalent suite — type-check (`ty`), lint (`ruff`), the runnable README
example, and the test suite — with:

```bash
uv run poe precommit
```

`SPEC.md` is the authoritative specification for the Python primitives and the
`lazily-spec` compliance notes.
