# lazily-py Specification

Python library for lazy evaluation with context-aware dependency injection and cache invalidation.

## Core Concepts

### Context

A plain `dict` serves as the context. Slots use themselves as dictionary keys to store cached values. No dedicated Context class — any dict works.

### Slot

Lazily-computed cached value with automatic dependency tracking via a global `slot_stack`.

**Types:**

| Type | Purpose |
|------|---------|
| `BaseSlot[C_in, C_ctx, T]` | Base slot without dependency tracking |
| `Slot[C_in, C_ctx, T]` | Slot with dependency tracking and invalidation |
| `slot[C_ctx, T]` | Convenience: Slot with identity context resolver |
| `slot_def(resolve_ctx)` | Decorator factory for custom context resolvers |

**BaseSlot methods:**

| Method | Purpose |
|--------|---------|
| `__call__(ctx)` | Compute or return cached value |
| `get(ctx)` | Return cached value or `None` |
| `reset(ctx)` | Clear cached value |
| `is_in(ctx)` | Check if value is cached |

**Slot additions:**

| Method | Purpose |
|--------|---------|
| `touch(ctx)` | Invalidate dependents |
| `reset(ctx)` | Clear cache + invalidate dependents. Downstream edges are rebound to `None` before propagating, so a re-entered reset finds an empty set. |

### Cell

Mutable value holder that notifies dependent slots when changed.

**Types:**

| Type | Purpose |
|------|---------|
| `Cell[T]` | Mutable value with subscription support |
| `CellSlot[C_in, C_ctx, T]` | Slot that returns a Cell |
| `cell[C_ctx, T]` | Convenience: CellSlot with identity resolver |
| `cell_def(resolve_ctx)` | Decorator factory for custom context resolvers |

**Cell operations:**

| Property/Method | Purpose |
|-----------------|---------|
| `cell.value` (get) | Read value; auto-subscribes calling slot |
| `cell.value = x` (set) | Update value; invalidate dependents if changed |
| `cell.get()` | Alias for value getter |
| `cell.set(x)` | Alias for value setter |
| `cell.touch()` | Invalidate dependents |

**No reactive exposes an observer API** — neither `Cell` nor `Computed`.
Observation is a declared dependency edge — read the cell from a `Computed` or
`Effect` — not a registered callback. For a stream of every transition, use
`Topic`.

### Eager Computed

Eager derived value — `computed(ctx, f).eager()`. Where a lazy `Computed`
marks itself dirty on invalidation and recomputes on the next read, an **eager**
`Computed` computes once at construction and recomputes immediately whenever a
tracked dependency changes. It is composed from existing primitives — a memoized
backing `Slot` plus a puller `Effect` that re-pulls the slot when it is
invalidated — and applies the PartialEq guard so an eager recompute that yields
an equal value suppresses the downstream cascade. (`.eager()` is idempotent and
returns the same handle; `.lazy()` reverses it.)

**Types:**

| Type | Purpose |
|------|---------|
| `Signal[T]` | Eager derived value bound to a single context |
| `signal(callable)` | Decorator: context-cached eager-Signal factory (one Signal per context) |
| `signal_def(resolve_ctx)` | Decorator factory with a custom context resolver |

**Signal operations:**

| Property/Method | Purpose |
|-----------------|---------|
| `signal.value` (get) | Read current value; auto-subscribes calling slot |
| `signal.get()` / `signal()` | Aliases for the value getter |
| `signal.touch()` | Invalidate dependents |
| `signal.is_active()` | Whether the eager puller is still installed |
| `signal.dispose()` | Remove the eager puller; value reverts to lazy (recomputed on next read) |

**Semantics:**

- **Eager activation:** the value is materialized at creation, so there is no
  intermediate unset state.
- **Memo guard:** an eager recompute that yields an equal value suppresses
  `touch()` and downstream invalidation, exactly like a memoized slot.
- **Once per flush, not once per write:** the puller is an `Effect`, so it is
  scheduled rather than run inline. N writes to a Signal's dependencies inside
  one `batch` (or one `ThreadSafeContext.batch`) re-materialize it exactly once,
  at the outermost exit.
- **Disposal removes only the puller:** the backing memo is untouched, so after
  `dispose()` the value stays readable, stays correct on read, and no longer
  re-materializes on write.
- **Wire representation:** a Signal is not a separate wire type. On the
  `lazily-spec` wire it is the ordinary backing slot node that stores its
  materialized value; the puller is local execution state and is never
  serialized. See **lazily-spec Compliance** below.

### Effect (sync)

A side-effecting observer that reruns whenever a tracked dependency invalidates.
An optional cleanup closure returned by the body runs before each rerun and on
dispose (cleanup-before-body ordering). Disposal is terminal.

**Types:**

| Type | Purpose |
|------|---------|
| `Effect` | Sync reactive effect (extends `Slot` for dependency tracking) |
| `effect(body)` | Register an effect; `body(ctx) -> cleanup \| None` |

**Operations:**

| Property/Method | Purpose |
|-----------------|---------|
| `effect(ctx)` | Run (or rerun) the body, auto-tracking dependencies |
| `effect.dispose()` | Deschedule, drop edges, run cleanup; terminal |
| `effect.disposed` | Whether `dispose()` has been called |

**Semantics:**

- **Auto-tracking:** pushes itself onto `slot_stack` during the body so every
  Cell/Slot/Signal read registers a dependency — the same mechanism as `Slot`.
- **Cleanup-before-body:** the previous run's cleanup closure completes before
  the next body starts.
- **Re-entrancy guard:** an invalidation fired while the body is executing
  schedules no extra rerun.
- **Batch coalescing:** inside a `batch`, reruns are queued for the coalesced
  effect flush at the outermost boundary (at most one rerun per batch).

The async counterpart (`AsyncEffect`) queues reruns at the batch boundary for
`asyncio` reactors.

### Batch

A top-level boundary that coalesces several cell writes into one invalidation +
effect flush. Multiple `Cell.value = x` writes inside a `batch` defer their
`touch()` to the outermost boundary, so a dependent reached through many changed
cells appears at most once per batch (the coalesced-frontier invariant).

**Types:**

| Type | Purpose |
|------|---------|
| `batch(run)` | Run `run`, queuing cell writes; flush one coalesced wave at exit |
| `batch_context()` | Context-manager form of `batch` |
| `in_batch()` | Whether the calling thread is currently inside a `batch` |

**Semantics:**

- **Coalesced cell touches:** writes inside the batch set the cell value but
  defer `touch()` to the outermost boundary; each changed cell is touched once.
- **Coalesced effect flush:** effects queued during the invalidation pass are
  deduplicated by identity and rerun once at the boundary.
- **Singleton refinement:** a one-write batch is observationally identical to a
  plain `Cell.set` (the `!=` PartialEq guard applies).
- **Nested:** only the outermost boundary flushes.

The lock-serialized counterpart that also linearizes concurrent writers lives at
`ThreadSafeContext.batch`.

## Dependency Tracking

Uses a global `slot_stack: list[Slot]` (acts as thread-local execution context).

1. When a Slot computes, it pushes itself onto `slot_stack`
2. Any child Slot or Cell accessed during computation sees the parent on the stack
3. The child records that parent in its `_parents` edge set (by identity); a later change calls `parent.reset()`
4. When a Cell value changes (and differs from old value), `touch()` cascades invalidation

**Key invariant:** Dependency edges are cleared on `reset()`, forcing re-registration on next access. This prevents stale edges.

## Invalidation Semantics

- `Cell.value = new_value` → if changed: `touch()` → `parent.reset()` → cascade
- `Slot.__call__(ctx)` → compute or return cached value; does **not** cascade invalidation (computation must not invalidate its own dependents — doing so destroys the edge being registered)
- `Slot.reset(ctx)` → clear cache → rebind downstream edges to `None` → push them onto the iterative invalidation work-stack → drain. Rebinding before propagation keeps a re-entered reset from finding stale edges.
- Value equality check: Cells only invalidate when `new_value != old_value`

## Context Resolvers

Custom context resolvers allow non-dict inputs to resolve to the underlying context dict:

```python
@slot_def(resolve_ctx)
def my_slot(ctx: dict) -> str:
    return "computed"

# Can be called with CustomCtxResolver or plain dict
result = my_slot(custom_resolver)
```

## Type System

- `LazilyCallable[C, T]` — Protocol for context-consuming callables
- `ResolveCallable[R, C]` — Protocol for context resolvers
- Full generic type annotations with `C_in`, `C_ctx` (bound to dict), `T`

## lazily-spec Compliance (IPC Wire Protocol)

`lazily.ipc` implements the language-agnostic [`lazily-spec`](https://github.com/lazily-hub/lazily-spec)
wire protocol so a Python reactive graph's state can be mirrored to remote
observers across processes and languages. The JSON representation is
**byte-compatible** with the Rust reference (`lazily-rs`) and the Zig binding.

### Wire types

| Type | Wire form |
|------|-----------|
| `IpcMessage` | Externally-tagged: `{"Snapshot": …}`, `{"Delta": …}`, or `{"CrdtSync": …}` |
| `Snapshot` | `{ epoch, nodes[], edges[], roots[] }` |
| `NodeSnapshot` | `{ node, type_tag, state, key? }` (`key` omitted when absent) |
| `NodeState` | `{"Payload": [u8…]}` \| `{"SharedBlob": {…}}` \| `"Opaque"` |
| `NodeKey` | Bare string path (`scores/alice`); optional on `NodeSnapshot` / `NodeAdd` |
| `EdgeSnapshot` | `{ dependent, dependency }` |
| `Delta` | `{ base_epoch, epoch, ops[] }` |
| `DeltaOp` | 7 variants: `CellSet`, `SlotValue`, `Invalidate`, `NodeAdd`, `NodeRemove`, `EdgeAdd`, `EdgeRemove` (`NodeAdd` carries an optional `key`) |
| `IpcValue` | `{"Inline": [u8…]}` \| `{"SharedBlob": {…}}` |
| `ShmBlobRef` | `{ offset, len, generation, epoch, checksum }` |
| `WireStamp` | `{ wall_time, logical, peer }` (CRDT HLC stamp mirror) |
| `CrdtOp` | `{ node, key, stamp, state }` (state-based / CvRDT) |
| `CrdtSync` | `{ frontier[], ops[] }` (anti-entropy multi-writer plane) |
| `CapabilityHandshake` | Standalone frame: `{ protocol_id, protocol_major_version, codec, … }` |

**Conventions matching the normative fixtures:**

- Enums are **externally tagged** (the variant name is the single JSON key).
- Wire-stable identifiers (`NodeId`, `PeerId`) are bare JSON integers; keep them
  ≤ `2**53` for JavaScript/TypeScript peers.
- Serialized value bytes are JSON **arrays of `u8`**, not base64.
- `NodeKey` is **additive**: a missing `key` field decodes to `None` (`null`),
  so pre-`key` encoders and existing conformance fixtures round-trip unchanged.
  A `None` `key` is omitted from `NodeSnapshot` / `NodeAdd` (self-describing
  codecs); `CrdtOp.key` is emitted as `null` when unset (matches the Rust
  derived struct). Path bounds (`NODE_KEY_MAX_LEN = 1024`, `NODE_KEY_MAX_SEGMENTS = 32`)
  are enforced on construction and on the wire.
- `IpcMessage.encode_json()` / `decode_json()` move transport-agnostic bytes
  (unix socket, pipe, WebSocket, WebRTC data channel, shared memory).

### NodeKey

A `NodeKey` is a `/`-joined path (`scores/alice`, `outer/k1/inner/k2`) — an
optional wire-stable keyed address that survives `NodeId` churn. Unlike
`NodeId` (a volatile internal handle a producer may re-mint after a resync or
remove-then-readd), a key is producer-defined and stable, so a peer can
subscribe to "entry `scores/alice`" without an out-of-band key→NodeId map.

- `NodeKey.new(path)` / `NodeKey.from_segments(parts)` — validated construction
  (raises `NodeKeyError`: `Empty`, `TooLong`, `TooManySegments`, `EmptySegment`).
- `NodeSnapshot.with_key(key)` / `DeltaOp.node_add(node, type_tag, state, key)`
  attach a key; the `key` field is omitted from JSON when unset.

### Distributed: CRDT cell plane

`CrdtSync` rides the same `lazily-ipc` transport as `Snapshot`/`Delta` as a
third `IpcMessage` variant. It is the multi-writer anti-entropy plane
(`merge: crdt`): each `CrdtOp` ships a converged state-based register value
tagged with a `WireStamp` (the wire mirror of the runtime HLC stamp); the
`frontier` advertises the sender's per-peer highest observed stamp so the
receiver can compute the causal-stability watermark. Merges are commutative,
associative, and idempotent, so out-of-order or duplicated delivery converges.

- `CrdtSync.filter_readable(permissions, peer)` omits ops for non-readable
  nodes entirely (omission, not redaction) while retaining the full frontier.
- Wiring the plane to live `merge: crdt` root cells is a follow-on runtime slice;
  this binding ships the codec-stable wire types.

### Capability negotiation

`CapabilityHandshake` is the standalone frame exchanged before any graph state
flows (it is not an `IpcMessage` variant). Peers that disagree on
`protocol_major_version`, `codec`, or `ordered_reliable` fail closed before any
`Snapshot` or `Delta` is applied.

- `CapabilityHandshake.new(peer_id, session_id)` — protocol defaults (JSON codec,
  1 MiB frame, ordered-reliable, no features).
- `handshake.is_compatible_with(other)` — fail-closed compatibility check.
- Constants `PROTOCOL_ID = "lazily-ipc"`, `PROTOCOL_MAJOR_VERSION = 1`.

### Epoch sequencing

A context-level monotonic `ipc_epoch` advances once per outermost batch flush.
Each `Delta` carries `{ base_epoch, epoch }` with `epoch == base_epoch + 1`.

- `Delta.is_next_after(last_epoch)` — whether the delta applies in sequence.
- `Delta.apply_status(last_epoch)` — `DeltaApplyStatus.apply()` when sequential,
  else `DeltaApplyStatus.resync_required(last_epoch, base_epoch, epoch)`, which
  tells the receiver to discard the delta and request a fresh `Snapshot`.

### Shared-memory blob arena (host)

`ShmBlobArena` ports the `lazily-rs` `ShmBlobArena<B>` host capability
(`ipc.rs`) and mirrors `lazily-zig` `ShmBlobArena` (`ipc.zig`), so a Python
process can **host** shared-memory blob payloads rather than only carry
`ShmBlobRef` descriptors produced elsewhere. The arena is a flat `bytearray`
plus an append-only write cursor; each write emits a `ShmBlobRef` descriptor and
prepends a 40-byte header (`LZSH` magic, version, header length, generation,
epoch, payload length, FNV-1a-64 checksum). Reads validate bounds, the header,
generation/epoch/length, and the checksum before returning a zero-copy
`memoryview`. Append-only with wraparound; each write bumps a generation counter
so a stale descriptor landing on an overwritten region fails validation instead
of returning torn data. Descriptors are byte-compatible with the Rust and Zig
arenas (identical header layout + FNV-1a-64 constants).

- `ShmBlobArena.with_capacity(n)` / `ShmBlobArena.from_buffer(buf)` — allocate
  a fresh `bytearray` or wrap externally-owned storage (e.g. an `mmap` region
  cast to `bytearray`); caller keeps `buf` ownership in the latter case.
- `arena.write_blob(epoch, payload) -> ShmBlobRef`
- `arena.read_blob(ref) -> memoryview` (zero-copy, read-only)
- `arena.capacity` / `arena.max_blob_len` / `arena.write_offset`
- Errors: `ShmBlobArenaError` base with variants `ShmBlobCapacityTooSmall`,
  `ShmBlobTooLarge`, `ShmBlobDescriptorOutOfBounds`, `ShmBlobDescriptorMismatch`,
  `ShmBlobChecksumMismatch`, `ShmBlobGenerationOverflow` — matching the Rust enum
  and Zig error set. `SHM_BLOB_HEADER_LEN` is exported.

True cross-process OS shared memory (`/dev/shm`, `mmap`) is out of scope here
and is a follow-on that swaps the backing buffer; this port establishes the
in-process arena and host parity across siblings.

### Permission boundary (omission, not redaction)

`PeerPermissions` is a default-deny per-peer allowlist gating `read`, `write`,
and `trigger_effect` (`OpKind`) **independently** — a read grant never implies
write or effect-trigger. Non-readable nodes are **omitted entirely** from a
snapshot/delta (not redacted in place), so a peer cannot infer their existence:

- `Snapshot.filter_readable(permissions, peer)` — drops unreadable nodes; keeps
  an edge only when both endpoints are readable; preserves root order.
- `Delta.filter_readable(permissions, peer)` — drops ops whose target node (or
  either edge endpoint) is unreadable.
- `permissions.check(peer, op)` raises `PermissionDenied` (fail-closed).

### Conformance

`tests/test_conformance.py` validates the canonical `lazily-spec/conformance`
fixtures (preferring the sibling spec repo, falling back to a vendored copy under
`tests/conformance/`). Each test parses the fixture `wire` into a native
`IpcMessage`, asserts the language-agnostic `assertions`, and re-serializes to
confirm round-trip fidelity — the same contract the Rust and Zig bindings run.

## lazily-spec Compute-Layer Compliance

Beyond the wire protocol, lazily-py implements the `lazily-spec` compute-layer
`MUST`s, each ported from its Lean formal model in `lazily-formal` and covered
by property tests that mirror the named Lean theorems.

### Keyed reactive collections (`ReactiveMap` / `SourceMap` / `ComputedMap` / `CellTree`)

One generic keyed primitive `ReactiveMap` over a handle kind (`#reactivemap`),
with two specializations: `SourceMap` (input-cell entries — adds cell-only `set`
and eager value-minting `entry`/`entry_with`) and `ComputedMap` (derived-slot entries
— `get_or_insert_with` mints a slot on first access for lazy materialization,
`materialize_all` pre-mints the keyset for eager; no `set`). No eager/lazy mode
flag — eager is a pre-mint loop, lazy is mint-on-access.

Three independent reactive signals: per-entry value, set-membership, and order.
A pure reorder (`move_to`) bumps the order signal only — `len`/`contains` readers
are not invalidated; an atomic move keeps each entry's handle identity (not remove
+ re-mint). A key resolves to a stable handle across requests (identity
stability). `CellTree` extends the model to an ordered keyed tree with per-node
value and per-level membership/order reactivity.

- `ReactiveMap.get_or_insert_with` / `.remove` / `.move_to` / `.move_before` /
  `.move_after`; `membership_signal` / `order_signal`; `SourceMap.entry` / `.set`;
  `ComputedMap.materialize_all`.
- `CellTree.set_node_value` / `.insert_child` / `.move_child`.

### Keyed reconciliation (`reconcile_ops`)

The move-minimized `{insert, remove, move, update}` op set a level diff emits by
stable key, over a longest-increasing-subsequence (LIS) kernel. Keys already in
relative order (the LIS) do NOT move; a stable entry with an unchanged value is
neither moved nor updated — so its value cell is untouched. Replays the
`lazily-spec/conformance/collections/keyed_reconciliation_lis.json` fixture.

### Async reactive context (`AsyncSlot` / `AsyncEffect`)

The `Empty / Computing / Resolved / Error` slot lifecycle with revision-tracked
stale-completion discard — a stale completion is never published. `AsyncEffect`
serializes reruns cleanup-before-body and schedules them at the outermost batch
boundary (invalidation only queues, never runs inline); disposal is terminal.
The pure `step` kernels (`lazily.async_slot.step` / `lazily.async_effect.step`)
mirror `LazilyFormal.AsyncSlotState` / `AsyncEffect`.

### Thread-safe reactive context (`ThreadSafeContext`)

A lock-serialized `batch(run)` that queues cell writes and flushes them in one
coalesced invalidation pass at the outermost boundary — the "coalesced frontier:
a dependent reached through many changed cells in one batch appears at most once
per delta" invariant. A singleton batch refines the single-threaded `Cell.set`.

### C-ABI FFI boundary (`lazily.ffi`)

`LazilyFfiStatus` (0 Ok / 1 Empty / 2 NullPointer / 3 InvalidMessage /
4 EncodeFailed / 5 Panic), `LazilyFfiMessageKind` (0 Unknown / 1 Snapshot /
2 Delta / **3 CrdtSync** — the spec mandates the kind discriminant carries
`CrdtSync`), `LazilyFfiBytes` (`ptr`/`len`), and `encode_message` /
`decode_message` re-encoding an `IpcMessage` to canonical JSON bytes
byte-compatible with the Rust/Zig FFI boundaries.

### Cell-model layers (`lazily.semtree` / `stable_id` / `textcrdt` / `seqcrdt`)

The `lazily-spec` cell-model § "Free-text CRDT", § "Move-aware sequence order",
§ "Memoized semantic tree", and § "Manufactured identity" layers, each pinned by
its `conformance/collections/*.json` fixture:

- **`SemTree`** — memoized semantic tree. One memo slot per node folds
  `(node value, child derived values)`; editing one node recomputes only its
  ancestor chain (a sibling subtree stays cached), and a node edit that does not
  change the folded result re-runs no downstream consumer (memo equality guard).
- **`stable_id`** — manufactured identity for text. Three layers: in-band
  anchors (`a:<anchor>`), content-derived hashes (`c:<hash>` over
  whitespace-normalized text), and word-LCS similarity alignment
  (`>= 0.5` ⇒ `Edited`/key-inherited; below ⇒ `Inserted`).
- **`TextCrdt`** — Fugue/RGA-style character CRDT. Order is pre-order DFS of the
  origin tree, siblings sorted DESCENDING by `OpId`; merge is
  commutative/associative/idempotent. Delta sync (`version_vector` /
  `delta_since` / `apply_delta`) preserves every character's `OpId` so a later
  concurrent edit merges without duplication.
- **`SeqCrdt`** — move-aware sequence CRDT. Each element is three independent
  LWW registers (value, position, deleted); a move is a single LWW reassignment
  of position so concurrent moves converge to the later stamp without
  duplication. Order is the lexicographic total order on `(frac, peer)`.

### CRDT registers (`lazily.crdt_registers`)

The `merge: crdt` register kinds (`protocol.md § Cell register types`):
`LwwRegister` (last-write-wins by HLC stamp; peer id is the final tiebreak),
`MvRegister` (multi-value — surfaces concurrent writes via a causal-context
`observed` set), `PnCounter` (positive-negative counter; per-peer `max` merge),
and `CellCrdt` (a CRDT cell wrapping an `LwwRegister` and propagating into the
reactive `Cell` plane, PartialEq-guarded after merge).

### Distributed CRDT plane (`lazily.crdt_plane`)

`CrdtPlaneRuntime` ingests state-based `CrdtOp`s and converges to the
greatest-stamp winner per `(node, key)` regardless of delivery order;
op-log dedup is keyed by `(node, stamp)` so re-delivering an already-seen frame
applies 0 new ops (state-based CvRDT idempotence). The runtime maintains the
per-peer stamp `frontier` and the causal-stability `watermark` (the `min` over
frontier membership) that gates tombstone GC. `to_sync()` / `delta_sync()`
publish anti-entropy `CrdtSync` frames.

### Signaling plane (`lazily.signaling`)

`SignalingFrame` is the typed envelope for the WebSocket signaling protocol;
`RoomCore` is the room state machine that implements the anti-spoof routing
invariant — a directed frame's `from` is the sender's server-registered peer id
(never client-supplied), the `welcome` roster excludes the joining peer's own
id, and `to`/`from` are never both present on one frame. `open` and `allowlist`
permission modes are supported; a concrete WebRTC backend is a platform adapter
behind the transport seam (the portable signaling stack + in-process loopback is
conformance-tested).

### State projection / mirror (`lazily.projection`)

`StateMirror` projects one local reactive context onto the `Snapshot`/`Delta`
wire plane. The value-mirror default resolves each invalidated allowlisted slot
at flush so the delta carries concrete `SlotValue`s; an eager `Computed` whose
value changed publishes a `SlotValue` for its backing slot; an equal recompute
(memo guard) suppresses both `SlotValue` and downstream invalidation. A
`PeerPermissions` boundary omits non-readable nodes entirely from both the
snapshot and the delta.

### Instrumentation / benchmarks (`lazily.benchmarks`)

`run_benchmarks()` micro-benchmarks the reactive core (cached read + invalidate
recompute), keyed reconciliation (LIS move-minimized diff), `SourceMap` insertion,
`TextCrdt` merge, and `CrdtPlaneRuntime` idempotent apply. Each entry reports
sample count, total elapsed, and per-op time; runnable as `python -m
lazily.benchmarks`.

### lazily-formal integration

`tests/test_formal_build.py` runs `lake build` over the sibling `lazily-formal`
Lean model and fails the suite if any theorem regresses (skipped when `lake` or
`lazily-formal` is absent, e.g. a standalone PyPI sdist checkout). The property
tests across `test_statechart_properties.py`, `test_thread_safe_properties.py`,
`test_async_slot_properties.py`, `test_async_effect_properties.py`,
`test_collection.py`, `test_tree.py`, and `test_reconciliation.py` mirror the
named Lean theorems — the universal guarantees no finite fixture suite can
establish.

## Requirements

- Python 3.12+
- Zero external dependencies (the `lazily.ipc` wire protocol uses only the
  standard-library `json` module)
