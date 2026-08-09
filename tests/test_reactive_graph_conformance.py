"""Reactive-graph conformance runner against canonical lazily-spec fixtures.

Executes the JSON fixtures in ``lazily-spec/conformance/reactive-graph``
**directly** — this is a fixture *interpreter*, not a hand-transcribed replay.
Transcriptions and bundled copies drift from the spec, so the canonical files
are read from the sibling checkout and never vendored here.

Fixture path resolution mirrors ``lazily-rs``
(``tests/reactive_graph_conformance.rs``, ``#lzspecconf``): one sibling-relative
``SPEC_DIR`` constant, skip with an explicit message when it is absent, and a CI
guard that asserts the directory exists so the skip can never silently pass —
see the "Assert canonical lazily-spec fixtures are present" step in
``.github/workflows/precommit.yml``.

## Replayed against every context lazily-py ships

The corpus runs against :class:`~lazily.cell.Cell`/:class:`~lazily.slot.Slot`
(the synchronous graph), :class:`~lazily.thread_safe.ThreadSafeContext`, and
:class:`~lazily.async_context.AsyncContext`. Replaying only the default context
is exactly how the ``lazily-dart`` and ``lazily-go`` invalidation-cascade
defects hid: both were correct synchronously and broken asynchronously. The
async path — where staleness is tracked by revision counters and in-flight
state rather than a pull chain — is the one the transitive-depth fixture was
written to discriminate (``#lzdartobservercow``), and it is also where a
disposal that forgets to dirty the survivors is hardest to notice.

## Coverage

The whole corpus replays: the teardown-scope vocabulary (``begin_scope`` /
``end_scope`` / ``disarm``), ``effect``, ``dispose``, ``fanout`` / ``churn`` /
``dispose_fanout`` / ``dispose_stale_handle``, degree introspection
(``dependents_of`` / ``dependencies_of``), and the signal-eagerness vocabulary
(``signal`` / ``dispose_signal`` / ``batch`` with the ``computes_of``
observable). ``computes_of`` is the cumulative number of times a node's compute
has run, counted by the synthesized compute itself: an eager signal and a lazy
memo return identical values for every read sequence in those fixtures, so a
runner that inferred the count instead of measuring it would pass against a
``signal()`` that is really a ``memo()``. Op support is per-context — see
:data:`EXPECTED_SKIPS`. Both fixture shapes execute:
``steps``, and ``scenarios`` — which exists because a claim like
``observationally_equal`` is a *relation between two op streams* that a single
``steps`` array cannot express, so each scenario is replayed in its own context
and the resulting observations compared.

## Findings, not relaxations (``#lzspecconf``)

Assertion failures are *recorded* rather than raised, so one run reports the
whole corpus instead of stopping at the first divergence, and the recorded set
is reconciled against :data:`KNOWN_DIVERGENCES` exactly. A new divergence fails
the build and a fixed one fails it until its entry is removed. No fixture is
ever edited and no assertion ever loosened to make that list shorter.

## Positive assertion (``#lzspecconf``)

An absence guard is not enough: a runner that skips everything must fail. For
each model this asserts (a) the fixture set on disk matches :data:`FIXTURES`
exactly, (b) the set actually replayed matches :data:`EXPECTED_REPLAYED`
exactly, (c) the observed skips match :data:`EXPECTED_SKIPS` exactly, and (d) a
non-zero number of fixtures, ops and assertions actually executed. A runner
that can pass while executing nothing is the anti-pattern this file exists to
kill.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import TYPE_CHECKING, Any

import pytest
from conformance_assert import (
    assert_key,
    assert_key_into,
    corpus_subdir,
    excuse_key,
    instrument,
    sub_entries,
)
from conformance_assert import (
    scenarios as replay_scenarios,
)

from lazily import (
    Cell,
    Computed,
    Effect,
    Slot,
    TeardownScope,
    batch,
    computed,
)
from lazily.async_context import AsyncContext, AsyncEffectHandle, AsyncSource
from lazily.slot import DisposedError
from lazily.teardown import dispose_node
from lazily.thread_safe import ThreadSafeContext


if TYPE_CHECKING:
    from pathlib import Path


# Resolved through the shared corpus seam (#lzoverrideallrunners), never by this
# module joining `../lazily-spec/conformance` itself. A runner that computes the
# path locally is invisible to `LAZILY_SPEC_CONFORMANCE_DIR`, so a perturbation
# probe points the coverage guard at a scratch copy while this runner keeps
# replaying the shared sibling checkout — and reports green over bytes nobody
# perturbed. No bundled copies either way.
_SPEC_PATH = corpus_subdir("reactive-graph")

#: What the skip / assert messages name. Names the copy whenever the override is
#: set, so a message can never point at a corpus the run did not read.
SPEC_DIR = str(_SPEC_PATH)

# The canonical fixture set. Asserted against the directory listing so a fixture
# added or renamed upstream fails loudly instead of going unrun.
FIXTURES = (
    "churn_returns_to_baseline.json",
    "cross_scope_teardown_hazard.json",
    "disarm_disposes_nothing.json",
    "dispose_detaches_edges_both_directions.json",
    "dispose_signal_reverts_to_lazy.json",
    "failed_compute_is_never_cached.json",
    "disposal_does_not_run_surviving_effects.json",
    "exact_fold_paths_stay_exact.json",
    "feedback_drain_bound_reports_exhaustion.json",
    "merge_cell_acquires_no_dependency_edge.json",
    "merge_feed_through_a_formula_coalesces.json",
    "merge_folds_synchronously_in_batch.json",
    "merge_per_settled_cone_not_per_write.json",
    "read_after_dispose_is_an_error.json",
    "recycled_id_inherits_nothing.json",
    "scope_teardown_equals_fold_of_disposals.json",
    "scoping_bounds_teardown_not_visibility.json",
    "signal_materializes_once_per_batch.json",
    "signal_materializes_without_a_read.json",
    "teardown_runs_members_in_reverse_creation_order.json",
    "transitive_invalidation_reaches_depth.json",
)

# The three fixtures that exercise `Signal` eagerness (`#lzsignaleager`). Named
# because support for them is per-context: `Signal` is a construct over the
# *synchronous* graph, so `Context` and `ThreadSafeContext` replay them and
# `AsyncContext` — which ships no signal constructor at all — cannot.
SIGNAL_FIXTURES = frozenset(
    {
        "dispose_signal_reverts_to_lazy.json",
        "signal_materializes_once_per_batch.json",
        "signal_materializes_without_a_read.json",
    }
)

# The six merge-feed fixtures landed on spec main (#lzmergefeed, lazily-spec
# Step 3). This runner does not model them and skips each in *every* context —
# they are accounted-for skips, not silent drops. Five drive the `merge_cell`
# op, the accumulate/fold write surface lazily-py's reactive graph ships no node
# kind for, so the pre-flight rejects them on that op. The sixth,
# `feedback_drain_bound_reports_exhaustion`, uses only supported ops but asserts
# the novel `drain_exhausted` key (parked upstream too), so the pre-flight
# rejects it on that expectation. Each reason below is exactly the string
# `_unsupported_reason` returns for the *first* unsupported op or expectation, so
# an upstream change that makes one replayable — or shifts the offending step —
# surfaces as a loud skip-ledger diff rather than a quiet pass.
_MERGE_CELL_UNSUPPORTED = "unsupported op `merge_cell`"
MERGEFEED_SKIPS: dict[str, str] = {
    "exact_fold_paths_stay_exact.json": _MERGE_CELL_UNSUPPORTED,
    "feedback_drain_bound_reports_exhaustion.json": (
        "unsupported expectation `drain_exhausted`"
    ),
    "merge_cell_acquires_no_dependency_edge.json": _MERGE_CELL_UNSUPPORTED,
    "merge_feed_through_a_formula_coalesces.json": _MERGE_CELL_UNSUPPORTED,
    "merge_folds_synchronously_in_batch.json": _MERGE_CELL_UNSUPPORTED,
    "merge_per_settled_cone_not_per_write.json": _MERGE_CELL_UNSUPPORTED,
}
MERGEFEED_FIXTURES = frozenset(MERGEFEED_SKIPS)

# Fixtures each context executes end to end. Asserted exactly, per context: a
# fixture that stops replaying, and a fixture that starts replaying, both fail
# the build.
EXPECTED_REPLAYED: dict[str, frozenset[str]] = {
    "Context": frozenset(FIXTURES) - MERGEFEED_FIXTURES,
    "ThreadSafeContext": frozenset(FIXTURES) - MERGEFEED_FIXTURES,
    "AsyncContext": frozenset(FIXTURES) - MERGEFEED_FIXTURES - SIGNAL_FIXTURES,
}

# Fixtures skipped per context, with the *first* unsupported op or expectation
# encountered. An entry here is a gap in lazily-py's public surface, never a
# relaxation of the fixture — which is exactly what the `AsyncContext` entries
# record: `lazily.Signal` is bound to a plain-dict sync context and there is no
# `AsyncContext.signal_async`, so the eagerness clauses are unverified on the
# async graph. Building the composition by hand inside the runner would report a
# conformance result for a surface callers cannot reach, so it is skipped and
# reported instead.
_ASYNC_NO_SIGNAL = "unsupported op `signal`"
EXPECTED_SKIPS: dict[str, dict[str, str]] = {
    "Context": dict(MERGEFEED_SKIPS),
    "ThreadSafeContext": dict(MERGEFEED_SKIPS),
    "AsyncContext": {
        **MERGEFEED_SKIPS,
        **dict.fromkeys(sorted(SIGNAL_FIXTURES), _ASYNC_NO_SIGNAL),
    },
}

# Fixture assertions an execution model does not satisfy today, as
# ``<model>/<fixture>[<scenario>]#<step>:<key>``.
#
# Each entry is a finding against the implementation, not a relaxation of the
# fixture: the runner asserts this set matches the observed one exactly, so a
# new divergence fails the build and a fixed one fails it until the entry is
# removed.
#
# Empty, and it must stay empty. The one entry this ledger ever held was
# lazily-py's async effect running a body's cleanup at the end of the same
# flush, observed by `disarm_disposes_nothing` at a step where nothing is
# disposed or invalidated. It was escalated rather than papered over, and the
# spec ruled the trigger normative — cleanup runs on rerun or dispose and at no
# other time, `lazily-spec` b6eb030, `docs/async.md` § Conformance item 5 — so
# py was the family outlier. Fixed in 83bdc68; the entry is gone rather than
# grandfathered.
KNOWN_DIVERGENCES: frozenset[str] = frozenset()


_SUPPORTED_OPS = frozenset(
    {
        "batch",
        "begin_scope",
        "cell",
        "churn",
        "computed",
        "disarm",
        "dispose",
        "dispose_fanout",
        "dispose_signal",
        "dispose_stale_handle",
        "drive",
        "effect",
        "end_scope",
        # Arms the next N computes of an existing node to raise, so a fixture
        # can assert on `computes_of` that a failed compute is never cached.
        "fail_next",
        "fanout",
        "read",
        "set_cell",
        "signal",
        "undrive",
    }
)

# Ops that need an eager-computed constructor. Subtracted from a model's
# vocabulary when the context it drives does not ship one. `drive` / `undrive`
# are v1 back-compat op aliases dual-accepted alongside `signal` /
# `dispose_signal` (#lzcellkernel): an eager `Computed` is the former `Signal`,
# so the same per-context support gate applies.
_SIGNAL_OPS = frozenset({"dispose_signal", "drive", "signal", "undrive"})

_SUPPORTED_EXPECT = frozenset(
    {
        "cleanup_order",
        "computes_of",
        "dependencies_of",
        "dependents_of",
        "error",
        "note",
        "observed_by",
        "observed_count",
        "read",
        "readable",
        "scope_owned_count",
        "value",
    }
)
_SUPPORTED_SHAPES = frozenset({"scenarios", "steps"})

# A read either produced a value or hit a disposed node. Modelled as a tagged
# tuple rather than propagating the exception to the call site, so a failed read
# is *compared* like any other observation instead of aborting the replay.
_ERR: tuple[str, Any] = ("err", None)

# A read whose compute body raised because `fail_next` armed it. Deliberately a
# DIFFERENT observation from `_ERR`: disposal is permanent by contract, a failed
# compute is recoverable by contract (the next read re-runs the body), so the
# replay must not latch the id for this one.
_FAIL: tuple[str, Any] = ("compute_failed", None)


class ComputeFailed(Exception):
    """Raised by a `fail_next`-armed compute body.

    A runner-owned sentinel, not a library error: the contract under test is
    that the library does not CACHE it, so what it is matters less than that the
    same body raises it once per armed run and the node still re-runs afterwards.
    """


def _ok(value: Any) -> tuple[str, Any]:
    return ("ok", value)


# --------------------------------------------------------------------------- #
# Execution models — one per context lazily-py ships.
#
# Every method is `async` so a single replay loop drives all three; the two
# synchronous models simply never suspend. Each model owns the whole op
# vocabulary, so a divergence between contexts shows up as a fixture failure
# under one model and not the others.
# --------------------------------------------------------------------------- #


class SyncModel:
    """The synchronous graph: :class:`Cell` + :class:`slot`, ambient tracking."""

    NAME = "Context"
    SUPPORTED_OPS = _SUPPORTED_OPS

    def __init__(self) -> None:
        self.ctx: dict = {}
        self.scopes: dict[str, TeardownScope] = {}
        self.runs: list[str] = []
        self.cleanups: list[str] = []
        # `computes_of`: cumulative compute invocations per node id, counted
        # from the start of the scenario and never reset. Incremented by the
        # synthesized compute itself, so it counts what actually ran rather than
        # what the runner believes should have run — the whole point of the key
        # is that an eager signal and a lazy memo are value-indistinguishable.
        self.computes: dict[str, int] = {}
        # How many upcoming compute bodies must fail, per node id (`fail_next`).
        self.armed: dict[str, int] = {}

    # -- helpers --------------------------------------------------------- #

    def _value_of(self, node: Any, ctx: Any = None) -> Any:
        """Read a node from inside a computation, registering the edge.

        ``ctx`` is the body's per-recompute compute view; when present, the read
        is value-threaded through it (``ctx.read(node)``) so the edge attributes
        to the recomputing node with no ambient stack (``#lzcellkernel``). A
        raw-dict ctx (an async engine with its own scoping) falls back to a bare
        read.
        """
        read = getattr(ctx, "read", None)
        if read is not None:
            return read(node)
        if isinstance(node, (Cell, Computed)):
            return node.value
        return node(self.ctx)

    def fail_next(self, node_id: str, count: int) -> None:
        self.armed[node_id] = self.armed.get(node_id, 0) + (count if count > 0 else 1)

    def _take_armed(self, node_id: str) -> bool:
        """Consume one arming. Called AFTER the compute counter ticks, so an
        armed run is counted exactly like a successful one."""
        remaining = self.armed.get(node_id, 0)
        if remaining <= 0:
            return False
        self.armed[node_id] = remaining - 1
        return True

    def _compute(self, node_id: str, reads: list[Any], offset: Any) -> Any:
        self.computes.setdefault(node_id, 0)

        def compute(_ctx: dict) -> Any:
            self.computes[node_id] += 1
            if self._take_armed(node_id):
                raise ComputeFailed(node_id)
            total = offset
            for dep in reads:
                total += self._value_of(dep, _ctx)
            return total

        return compute

    def _body(self, name: str, reads: list[Any]) -> Any:
        def body(_ctx: dict) -> Any:
            self.runs.append(name)
            for dep in reads:
                self._value_of(dep, _ctx)

            def cleanup() -> None:
                self.cleanups.append(name)

            return cleanup

        return body

    # -- ops ------------------------------------------------------------- #

    async def cell(self, value: Any, scope: str | None) -> Any:
        if scope is not None:
            return self.scopes[scope].source(value)
        return Cell(self.ctx, value)

    async def computed(
        self, node_id: str, reads: list[Any], offset: Any, scope: str | None
    ) -> Any:
        compute = self._compute(node_id, reads, offset)
        if scope is not None:
            return self.scopes[scope].computed(compute)
        return Slot(compute)

    async def signal(self, node_id: str, reads: list[Any], offset: Any) -> Any:
        # v2 Cell kernel (#lzcellkernel): the fixture `signal` op maps to a
        # guarded `computed` made eager. `computed().eager()` returns the same
        # handle mutated, so the eager reader is what gets registered.
        return computed(self.ctx, self._compute(node_id, reads, offset)).eager()

    async def dispose_signal(self, node: Any) -> None:
        # Clause 4: this reverts the eager computed to lazy (disposes the puller),
        # it does not dispose the node — the fixture `dispose_signal` op maps to
        # `.lazy()`, which is why the runner does not route it through
        # `dispose_node`.
        node.lazy()

    async def drive(self, node_id: str, reads: list[Any], offset: Any) -> Any:
        # v1 back-compat op alias of `signal`: construct a lazy `Computed` and
        # make it eager.
        return computed(self.ctx, self._compute(node_id, reads, offset)).eager()

    async def undrive(self, node: Any) -> None:
        # v1 back-compat op alias of `dispose_signal`: stop eager recomputation
        # and dispose the puller; the value reverts to lazy.
        node.lazy()

    async def batch_writes(self, writes: list[tuple[Any, Any]]) -> None:
        batch(lambda: [cell.set(value) for cell, value in writes])

    async def effect(self, name: str, reads: list[Any], scope: str | None) -> Any:
        body = self._body(name, reads)
        if scope is not None:
            return self.scopes[scope].effect(body)
        handle = Effect(body)
        handle(self.ctx)
        return handle

    async def read(self, node: Any) -> tuple[str, Any]:
        try:
            if isinstance(node, (Cell, Computed)):
                return _ok(node.value)
            return _ok(node(self.ctx))
        except ComputeFailed:
            return _FAIL
        except DisposedError:
            return _ERR

    async def set_cell(self, node: Any, value: Any) -> None:
        node.set(value)

    async def dispose(self, node: Any) -> None:
        dispose_node(node, self.ctx)

    async def begin_scope(self, name: str) -> None:
        self.scopes[name] = TeardownScope(self.ctx)

    async def end_scope(self, name: str) -> None:
        self.scopes.pop(name).close()

    async def disarm(self, name: str) -> None:
        # Left in the map, disarmed: a later `end_scope` under the same name
        # must be a no-op rather than a KeyError.
        self.scopes[name].disarm()

    def scope_owned(self, name: str) -> int:
        return len(self.scopes[name])

    def dependents_of(self, node: Any) -> int:
        return node.dependent_count()

    def dependencies_of(self, node: Any) -> int:
        return node.dependency_count()

    def is_effect(self, node: Any) -> bool:
        return isinstance(node, Effect)

    def is_effect_active(self, node: Any) -> bool:
        return not node.disposed

    async def settle(self) -> None:
        """Synchronous models are quiescent the moment an op returns."""


class ThreadSafeModel(SyncModel):
    """The same graph driven through :class:`ThreadSafeContext`'s write path."""

    NAME = "ThreadSafeContext"

    def __init__(self) -> None:
        super().__init__()
        self.ts = ThreadSafeContext()

    async def set_cell(self, node: Any, value: Any) -> None:
        self.ts.set(node, value)

    async def batch_writes(self, writes: list[tuple[Any, Any]]) -> None:
        # The thread-safe batch, not the single-threaded one: its flush is a
        # separate code path (it applies deferred writes under the lock before
        # touching), so clause 3 is asserted against the boundary this context
        # actually ships rather than assumed equivalent to `batch()`.
        self.ts.batch(lambda: [self.ts.set(cell, value) for cell, value in writes])

    async def begin_scope(self, name: str) -> None:
        # Opened through the thread-safe surface, so its scope passthrough is
        # covered rather than assumed equivalent.
        self.scopes[name] = self.ts.scope(self.ctx)


class AsyncModel:
    """The async graph: :class:`AsyncContext`, explicit edge registration.

    The context that matters most here — revision-counter staleness plus
    in-flight state is where the pull chain that makes the lazy strategy work
    can break without the synchronous path noticing, and where a teardown that
    detaches edges without dirtying the survivors leaves a reader frozen on a
    resolved value forever.
    """

    NAME = "AsyncContext"
    # No signal constructor on the async graph: `lazily.Signal` is bound to a
    # plain-dict sync context and `AsyncContext` exposes no `signal_async`. The
    # signal fixtures skip here rather than being replayed against a
    # hand-rolled composition the runner built for itself — see EXPECTED_SKIPS.
    SUPPORTED_OPS = _SUPPORTED_OPS - _SIGNAL_OPS

    def __init__(self) -> None:
        self.ctx = AsyncContext()
        self.scopes: dict[str, Any] = {}
        self.runs: list[str] = []
        self.cleanups: list[str] = []
        self.computes: dict[str, int] = {}
        # How many upcoming compute bodies must fail, per node id (`fail_next`).
        self.armed: dict[str, int] = {}

    # -- helpers --------------------------------------------------------- #

    async def _value_of(self, cc: Any, node: Any) -> Any:
        if isinstance(node, AsyncSource):
            return cc.get(node)
        return await cc.get_async(node)

    def fail_next(self, node_id: str, count: int) -> None:
        self.armed[node_id] = self.armed.get(node_id, 0) + (count if count > 0 else 1)

    def _take_armed(self, node_id: str) -> bool:
        """Consume one arming. Called AFTER the compute counter ticks, so an
        armed run is counted exactly like a successful one."""
        remaining = self.armed.get(node_id, 0)
        if remaining <= 0:
            return False
        self.armed[node_id] = remaining - 1
        return True

    def _compute(self, node_id: str, reads: list[Any], offset: Any) -> Any:
        self.computes.setdefault(node_id, 0)

        async def compute(cc: Any) -> Any:
            self.computes[node_id] += 1
            if self._take_armed(node_id):
                raise ComputeFailed(node_id)
            total = offset
            for dep in reads:
                total += await self._value_of(cc, dep)
            return total

        return compute

    def _body(self, name: str, reads: list[Any]) -> Any:
        async def body(cc: Any) -> Any:
            self.runs.append(name)
            for dep in reads:
                await self._value_of(cc, dep)

            def cleanup() -> None:
                self.cleanups.append(name)

            return cleanup

        return body

    # -- ops ------------------------------------------------------------- #

    async def cell(self, value: Any, scope: str | None) -> Any:
        if scope is not None:
            return self.scopes[scope].source(value)
        return self.ctx.source(value)

    async def computed(
        self, node_id: str, reads: list[Any], offset: Any, scope: str | None
    ) -> Any:
        compute = self._compute(node_id, reads, offset)
        if scope is not None:
            return self.scopes[scope].computed(compute)
        return self.ctx.computed(compute)

    async def batch_writes(self, writes: list[tuple[Any, Any]]) -> None:
        self.ctx.batch(lambda: [self.ctx.set(cell, value) for cell, value in writes])

    async def effect(self, name: str, reads: list[Any], scope: str | None) -> Any:
        body = self._body(name, reads)
        if scope is not None:
            return self.scopes[scope].effect_async(body)
        return self.ctx.effect_async(body)

    async def read(self, node: Any) -> tuple[str, Any]:
        try:
            if isinstance(node, AsyncSource):
                return _ok(node.get())
            return _ok(await self.ctx.get_async(node))
        except ComputeFailed:
            return _FAIL
        except DisposedError:
            return _ERR

    async def set_cell(self, node: Any, value: Any) -> None:
        self.ctx.set(node, value)

    async def dispose(self, node: Any) -> None:
        if isinstance(node, AsyncEffectHandle):
            await node.dispose_async()
        elif isinstance(node, AsyncSource):
            self.ctx.dispose_source(node)
        else:
            self.ctx.dispose_computed(node)

    async def begin_scope(self, name: str) -> None:
        self.scopes[name] = self.ctx.scope()

    async def end_scope(self, name: str) -> None:
        await self.scopes.pop(name).aclose()

    async def disarm(self, name: str) -> None:
        self.scopes[name].disarm()

    def scope_owned(self, name: str) -> int:
        return len(self.scopes[name])

    def dependents_of(self, node: Any) -> int:
        return self.ctx.dependent_count(node)

    def dependencies_of(self, node: Any) -> int:
        return self.ctx.dependency_count(node)

    def is_effect(self, node: Any) -> bool:
        return isinstance(node, AsyncEffectHandle)

    def is_effect_active(self, node: Any) -> bool:
        return not node.disposed

    async def settle(self) -> None:
        """Drive the loop until every scheduled effect rerun has completed.

        Async effect reruns are *spawned*, so a synchronous ``set_cell`` returns
        before any body has run: ``observed_by``, ``observed_count`` and every
        degree assertion are meaningless until the runtime has been let run.
        This changes *when* the corpus's assertions are evaluated, never *what*
        they assert — an effect that never runs still fails.
        """
        for _ in range(10_000):
            await asyncio.sleep(0)
            pending = [
                e
                for e in list(self.ctx._effects)
                if e._task is not None and not e._task.done()
            ]
            if not pending:
                return
            await asyncio.gather(*(e.settle() for e in pending))
        raise AssertionError("AsyncContext never reached quiescence")


MODELS = (SyncModel, ThreadSafeModel, AsyncModel)


# --------------------------------------------------------------------------- #
# Fixture loading, pre-flight, replay
# --------------------------------------------------------------------------- #


def _fixture_paths() -> list[Path]:
    if not _SPEC_PATH.is_dir():
        return []
    return sorted(_SPEC_PATH.glob("*.json"))


def _unsupported_reason(
    fixture: dict[str, Any], supported_ops: frozenset[str]
) -> str | None:
    """Pre-flight the whole fixture so an unsupported one skips before any op runs.

    Returns the reason — naming the unsupported op or expectation — or ``None``
    when every step is executable. ``supported_ops`` is the *model's* vocabulary
    rather than the runner's: an op one context ships and another does not is a
    per-context gap, and collapsing it into a single global set would either hide
    the gap or stop the contexts that do support the op from replaying it.
    """
    shape = fixture.get("shape")
    if shape is None:
        return "fixture declares no `shape`"
    if shape not in _SUPPORTED_SHAPES:
        return f"unsupported fixture shape `{shape}`"
    if shape == "steps":
        streams = [fixture.get("steps")]
    else:
        scenarios = fixture.get("scenarios")
        if not scenarios:
            return "fixture declares no `scenarios`"
        streams = [s.get("steps") for s in scenarios]
    for steps in streams:
        if not steps:
            return "fixture declares no `steps`"
        for step in steps:
            op = step["op"]
            kind = op.get("type")
            if kind not in supported_ops:
                return f"unsupported op `{kind}`"
            for key in step.get("expect") or {}:
                if key not in _SUPPORTED_EXPECT:
                    return f"unsupported expectation `{key}`"
    return None


class Observation:
    """Everything a scenario leaves behind that ``observationally_equal`` compares."""

    __slots__ = (
        "after_publish_observed",
        "after_publish_reads",
        "cleanup_order",
        "degrees",
        "readable",
        "reads",
    )

    def __init__(self) -> None:
        self.cleanup_order: list[str] = []
        self.readable: dict[str, bool] = {}
        self.reads: dict[str, Any] = {}
        self.after_publish_observed: list[str] = []
        self.after_publish_reads: dict[str, Any] = {}
        self.degrees: dict[str, int] = {}

    def _key(self) -> tuple:
        return (
            self.cleanup_order,
            sorted(self.readable.items()),
            sorted(self.reads.items()),
            self.after_publish_observed,
            sorted(self.after_publish_reads.items()),
            sorted(self.degrees.items()),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Observation):
            return NotImplemented
        return self._key() == other._key()

    def __repr__(self) -> str:
        return f"Observation{self._key()!r}"


class Report:
    __slots__ = ("checks", "failures", "observation", "ops")

    def __init__(self) -> None:
        self.ops = 0
        self.checks = 0
        self.failures: list[tuple[str, str, str]] = []
        self.observation = Observation()


async def _replay(
    model: Any,
    name: str,
    steps: list[dict[str, Any]],
    tail: dict[str, Any] | None,
) -> Report:
    report = Report()
    nodes: dict[str, Any] = {}
    # Handles are kept forever so `dispose_stale_handle` can dispose through a
    # reference to a node that is already gone.
    stale: dict[str, Any] = {}
    # A live reader that once errored on a disposed dependency stays broken
    # until it is itself rebuilt — it never silently recovers.
    poisoned: set[str] = set()
    step_index = -1

    def check(key: str, got: Any, want: Any) -> None:
        report.checks += 1
        if got != want:
            where = "expected" if step_index < 0 else str(step_index)
            report.failures.append((where, key, f"got {got!r}, want {want!r}"))

    def node_of(node_id: str) -> Any:
        if node_id not in nodes:
            raise AssertionError(f"{name}: op names unknown node {node_id}")
        return nodes[node_id]

    def register(node_id: str, handle: Any) -> None:
        nodes[node_id] = handle
        stale[node_id] = handle
        poisoned.discard(node_id)

    async def read_id(node_id: str) -> tuple[str, Any]:
        if node_id in poisoned:
            return _ERR
        result = await model.read(node_of(node_id))
        # Only disposal latches. A `fail_next` compute failure is recoverable by
        # contract, so latching it would make the replay report the very defect
        # `failed_compute_is_never_cached.json` exists to catch, against
        # bindings that are correct.
        if result == _ERR:
            poisoned.add(node_id)
        return result

    async def alive(node_id: str) -> bool:
        handle = nodes.get(node_id)
        if handle is None:
            return False
        if model.is_effect(handle):
            return model.is_effect_active(handle)
        return (await read_id(node_id)) != _ERR

    for index, step in enumerate(steps):
        # Rebound rather than bound by the loop, because `check` closes over it
        # to label a divergence with the step that produced it.
        step_index = index
        op = step["op"]
        kind = op["type"]
        runs_before = len(model.runs)
        op_error = False
        op_value: Any = None
        report.ops += 1

        if kind == "cell":
            register(op["id"], await model.cell(op["value"], op.get("scope")))
        elif kind == "computed":
            reads = [node_of(r) for r in op.get("reads", [])]
            register(
                op["id"],
                await model.computed(
                    op["id"], reads, op.get("offset", 0), op.get("scope")
                ),
            )
        elif kind == "signal":
            reads = [node_of(r) for r in op.get("reads", [])]
            register(op["id"], await model.signal(op["id"], reads, op.get("offset", 0)))
        elif kind == "dispose_signal":
            await model.dispose_signal(node_of(op["id"]))
        elif kind == "drive":
            reads = [node_of(r) for r in op.get("reads", [])]
            register(op["id"], await model.drive(op["id"], reads, op.get("offset", 0)))
        elif kind == "undrive":
            await model.undrive(node_of(op["id"]))
        elif kind == "batch":
            # One op carrying its writes, so the runner needs no nesting state.
            # Every write goes inside ONE boundary: the point of the fixture is
            # that N writes coalesce into one re-materialization at the exit.
            await model.batch_writes(
                [(node_of(w["id"]), w["value"]) for w in op["writes"]]
            )
        elif kind == "effect":
            reads = [node_of(r) for r in op.get("reads", [])]
            register(op["id"], await model.effect(op["id"], reads, op.get("scope")))
        elif kind == "read":
            result = await read_id(op["id"])
            if result in (_ERR, _FAIL):
                op_error = True
            else:
                op_value = result[1]
        elif kind == "fail_next":
            model.fail_next(op["id"], op.get("count", 1))
        elif kind == "set_cell":
            await model.set_cell(node_of(op["id"]), op["value"])
        elif kind == "dispose":
            # The entry stays in the map: a disposed id remains readable-as-an-
            # error, and disposing it again must be a no-op.
            await model.dispose(node_of(op["id"]))
        elif kind == "fanout":
            prefix = op["id_prefix"]
            base = [node_of(r) for r in op.get("reads", [])]
            for i in range(op["count"]):
                # Subscribers are effects, not derived slots: the corpus asserts
                # `observed_count` on a publish, and in a lazy binding only an
                # eager reader observes a publish without being pulled.
                node_id = f"{prefix}_{i}"
                register(node_id, await model.effect(node_id, base, None))
        elif kind == "dispose_fanout":
            prefix = op["id_prefix"]
            for i in range(op["count"]):
                handle = nodes.get(f"{prefix}_{i}")
                if handle is not None:
                    await model.dispose(handle)
        elif kind == "churn":
            source = node_of(op["source"])
            prefix = op["id_prefix"]
            width = op["live_width"]
            mode = op["mode"]
            if mode == "dispose_then_create":
                # Hold `live_width` subscribers; each cycle disposes one and
                # creates its replacement, so the live count is invariant.
                for cycle in range(op["cycles"]):
                    node_id = f"{prefix}_{cycle % width}"
                    handle = nodes.get(node_id)
                    if handle is not None:
                        await model.dispose(handle)
                    nodes[node_id] = await model.effect(node_id, [source], None)
            elif mode == "scope_per_cycle":
                # One teardown scope per cycle; its subscriber is gone by the
                # end of its own cycle.
                scope_name = f"{prefix}__churn"
                effect_name = f"{prefix}_scoped"
                for _ in range(op["cycles"]):
                    await model.begin_scope(scope_name)
                    await model.effect(effect_name, [source], scope_name)
                    await model.end_scope(scope_name)
            else:
                raise AssertionError(f"{name}: unknown churn mode {mode}")
        elif kind == "begin_scope":
            await model.begin_scope(op["scope"])
        elif kind == "end_scope":
            await model.end_scope(op["scope"])
        elif kind == "disarm":
            await model.disarm(op["scope"])
        elif kind == "dispose_stale_handle":
            of = op["handle_of"]
            if of not in stale:
                raise AssertionError(f"{name}: no recorded handle for {of}")
            handle = stale[of]
            want_kind = op["handle_kind"]
            got_kind = (
                "effect"
                if model.is_effect(handle)
                else "cell"
                if isinstance(handle, (Cell, AsyncSource))
                else "slot"
            )
            assert got_kind == want_kind, (
                f"{name}: handle_kind {want_kind} does not match recorded {got_kind}"
            )
            await model.dispose(handle)
        else:  # pragma: no cover - pre-flighted by _unsupported_reason
            raise AssertionError(f"unsupported op `{kind}`")

        await model.settle()
        observed = model.runs[runs_before:]
        # `cleanup_order` is cumulative, not per-step: the individual-disposal
        # scenario spreads three disposals over three steps and pins the whole
        # order on the last one.
        cleaned = list(model.cleanups)

        expect = step.get("expect")
        if not expect:
            continue

        # Sorted, matching lazily-rs, whose `serde_json` object is a `BTreeMap`:
        # the evaluation order is part of what a fixture pins, because a `read`
        # can re-register an edge that a `dependents_of` then counts.
        for key in sorted(expect):
            # Reading the key through `assert_key_with` books the assertion: every
            # arm below hands `want` to `check`, which compares it against the
            # model (#lzconsumednotasserted).
            want = assert_key_into(expect, key, lambda fixture_value: fixture_value)
            if key == "note":
                continue
            if key == "dependents_of":
                # DESCEND (#lzsubblockkeyset): the map's own key set is owned by
                # a child tracker, so a node the corpus adds to it fails as
                # unconsumed instead of being compared by nothing.
                for node_id, degree in sub_entries(expect, key):
                    check(
                        f"dependents_of.{node_id}",
                        model.dependents_of(node_of(node_id)),
                        degree,
                    )
            elif key == "dependencies_of":
                for node_id, degree in sub_entries(expect, key):
                    check(
                        f"dependencies_of.{node_id}",
                        model.dependencies_of(node_of(node_id)),
                        degree,
                    )
            elif key == "error":
                # Any non-null error code means "this op must fail"; null means
                # "must not". The runner does not model error identity -- the
                # fixtures carry the code so the contract is legible, and each
                # binding's own tests pin which error type it raises.
                check("error", op_error, want is not None)
            elif key == "computes_of":
                # Cumulative since the start of the scenario, counted by the
                # synthesized compute itself. Sorts before `readable` and
                # `value`, which matters: those keys read, and a read of a
                # de-eagered signal recomputes.
                for node_id, count in sub_entries(expect, key):
                    check(f"computes_of.{node_id}", model.computes.get(node_id), count)
            elif key == "value":
                if expect.get("error") is not None:
                    pass
                elif kind == "read":
                    check("value", op_value, want)
                else:
                    # A `value` on a non-read op (a `signal` creation step) is a
                    # claim about the node the op names, not about a returned
                    # value.
                    check("value", await read_id(op["id"]), _ok(want))
            elif key == "read":
                for node_id, value in sub_entries(expect, key):
                    check(f"read.{node_id}", await read_id(node_id), _ok(value))
            elif key == "readable":
                for node_id, want_alive in sub_entries(expect, key):
                    check(f"readable.{node_id}", await alive(node_id), want_alive)
            elif key == "observed_by":
                check("observed_by", observed, list(want))
            elif key == "observed_count":
                check("observed_count", len(observed), want)
            elif key == "cleanup_order":
                # Only effects run a cleanup callback, so the expected order is
                # projected onto its effect entries.
                wanted = [i for i in want if model.is_effect(stale.get(i))]
                check("cleanup_order", cleaned, wanted)
            elif key == "scope_owned_count":
                for scope_name, count in sub_entries(expect, key):
                    check(
                        f"scope_owned_count.{scope_name}",
                        model.scope_owned(scope_name),
                        count,
                    )
            else:  # pragma: no cover - pre-flighted by _unsupported_reason
                raise AssertionError(f"{name}: unknown expectation {key}")

    # -- `scenarios`-shaped tail --------------------------------------------
    step_index = -1  # the `expected` tail is not a numbered step
    report.observation.cleanup_order = list(model.cleanups)
    if tail is None:
        return report

    # `final_state` and `after_publish` are object-valued, and so is every map
    # inside them, so the descent runs two levels (#lzsubblockkeyset): a child
    # tracker owns each object's key set and a node the corpus adds to any of
    # them fails as unconsumed rather than being compared by nothing.
    if "final_state" in tail:
        final = tail.sub("final_state")
        for node_id, degree in sub_entries(final, "dependents_of"):
            got = model.dependents_of(node_of(node_id))
            check(f"final.dependents_of.{node_id}", got, degree)
            report.observation.degrees[node_id] = got
        if "readable" in final:
            for node_id, want_alive in sub_entries(final, "readable"):
                got_alive = await alive(node_id)
                check(f"final.readable.{node_id}", got_alive, want_alive)
                report.observation.readable[node_id] = got_alive
        if "read" in final:
            for node_id, value in sub_entries(final, "read"):
                got_read = await read_id(node_id)
                check(f"final.read.{node_id}", got_read, _ok(value))
                report.observation.reads[node_id] = got_read[1]

    if "after_publish" in tail:
        publish = tail.sub("after_publish")
        pop = publish.sub("op")
        assert_key(pop, "type", "set_cell", where=f"{name}: after_publish op")
        # `id` and `value` are the op's INPUT rather than an expectation: they
        # say which cell the tail publishes to and what it sets. What they
        # produce is asserted below by `observed_by`, `read` and
        # `dependents_of`, so there is nothing to compare them against here.
        excuse_key(
            pop,
            "id",
            "the published cell's identifier — an input to the op, whose effect "
            "is asserted by after_publish.observed_by / .read / .dependents_of",
        )
        excuse_key(
            pop,
            "value",
            "the value published — an input to the op, whose effect is asserted "
            "by after_publish.read",
        )
        before = len(model.runs)
        await model.set_cell(node_of(pop["id"]), pop["value"])
        await model.settle()
        report.observation.after_publish_observed = model.runs[before:]
        check(
            "after_publish.observed_by",
            report.observation.after_publish_observed,
            list(
                assert_key_into(
                    publish, "observed_by", lambda fixture_value: fixture_value
                )
                or []
            ),
        )
        for node_id, value in sub_entries(publish, "read"):
            got_read = await read_id(node_id)
            check(f"after_publish.read.{node_id}", got_read, _ok(value))
            report.observation.after_publish_reads[node_id] = got_read[1]
        for node_id, degree in sub_entries(publish, "dependents_of"):
            check(
                f"after_publish.dependents_of.{node_id}",
                model.dependents_of(node_of(node_id)),
                degree,
            )

    return report


async def _run_fixture(
    model_cls: Any, name: str, fixture: dict[str, Any]
) -> list[Report]:
    """Replay one fixture, dispatching on its declared ``shape``.

    Dispatch is on ``shape``, never on the filename: a filename special case
    goes stale silently the moment a second scenarios-shaped fixture arrives.
    """
    if fixture["shape"] == "steps":
        return [await _replay(model_cls(), name, fixture["steps"], None)]
    # `scenarios`: each stream gets its own context, because the claim is a
    # relation *between* the streams and a shared graph would confound it.
    expected = fixture.get("expected")
    return [
        await _replay(model_cls(), name, scenario["steps"], expected)
        for scenario in replay_scenarios(fixture)
    ]


def _run_corpus(model_cls: Any) -> None:
    model_name = model_cls.NAME
    if not _SPEC_PATH.is_dir():
        pytest.skip(
            f"reactive_graph_conformance[{model_name}]: {SPEC_DIR} not found — "
            f"clone lazily-spec as a sibling to run the reactive-graph fixtures"
        )

    # The fixture set on disk must be exactly the one this runner knows about,
    # so an upstream addition cannot arrive unexecuted.
    on_disk = {p.name for p in _fixture_paths()}
    assert on_disk == set(FIXTURES), (
        "reactive-graph fixture set drifted; every fixture must be accounted for "
        f"by this runner (on disk: {sorted(on_disk)}, known: {sorted(FIXTURES)})"
    )

    replayed: set[str] = set()
    skipped: dict[str, str] = {}
    divergences: set[str] = set()
    total_ops = 0
    total_checks = 0

    for name in FIXTURES:
        raw = json.loads((_SPEC_PATH / name).read_text())
        reason = _unsupported_reason(raw, model_cls.SUPPORTED_OPS)
        if reason is not None:
            skipped[name] = reason
            continue

        # Instrumented only once the pre-flight has accepted the fixture. This
        # runner already fails CLOSED on an unrecognised expectation key —
        # `_unsupported_reason` rejects the fixture and the skip lands in the
        # asserted ledger above — so the unconsumed-key guard
        # (#lzassertunknownkeys) covers the replay path, and the skip ledger
        # covers the rest. Tracking a fixture the pre-flight rejected would just
        # report the same gap twice under a weaker name.
        fixture = instrument(raw, name=f"reactive-graph/{name}")

        reports = asyncio.run(_run_fixture(model_cls, name, fixture))

        # `observationally_equal`: the named scenarios must agree on every
        # observable, not merely each satisfy `expected` independently.
        tail_block = fixture.get("expected") or {}
        pair = (
            assert_key_into(
                tail_block,
                "observationally_equal",
                lambda fixture_value: fixture_value,
            )
            if "observationally_equal" in tail_block
            else None
        )
        if pair:
            # Keyed on `id`, the canonical scenario identity
            # (#recommendedconformanceco) — `name` is a prose label.
            names = [s["id"] for s in fixture["scenarios"]]
            index = [names.index(scenario) for scenario in pair]
            for left, right in itertools.pairwise(index):
                assert reports[left].observation == reports[right].observation, (
                    f"{model_name}/{name}: scenarios are not observationally equal\n"
                    f"  {names[left]}: {reports[left].observation}\n"
                    f"  {names[right]}: {reports[right].observation}"
                )
            total_checks += 1

        ops = sum(r.ops for r in reports)
        checks = sum(r.checks for r in reports)
        assert ops > 0, f"{model_name}/{name}: replayed zero ops"
        assert checks > 0, f"{model_name}/{name}: replayed zero assertions"
        total_ops += ops
        total_checks += checks
        replayed.add(name)

        for scenario_index, report in enumerate(reports):
            suffix = f"[{scenario_index}]" if len(reports) > 1 else ""
            for where, key, detail in report.failures:
                entry = f"{model_name}/{name}{suffix}#{where}:{key}"
                print(f"  DIVERGENCE {entry} — {detail}")
                divergences.add(entry)

    # Ledgers: all three fail loudly in both directions. A fixture that stops
    # replaying, one that starts, a skip reason that changes, and a divergence
    # that appears or disappears, all break the build.
    expected_replayed = EXPECTED_REPLAYED[model_name]
    expected_skips = EXPECTED_SKIPS[model_name]
    assert replayed == set(expected_replayed), (
        f"{model_name}: replayed set drifted (replayed: {sorted(replayed)}, "
        f"expected: {sorted(expected_replayed)})"
    )
    assert skipped == expected_skips, (
        f"{model_name}: skip ledger is stale — update EXPECTED_SKIPS "
        f"(observed: {skipped}, documented: {expected_skips})"
    )
    # Entries are model-prefixed, so each model reconciles only its own slice:
    # a divergence recorded for one context must not excuse another.
    expected_divergences = {
        e for e in KNOWN_DIVERGENCES if e.startswith(f"{model_name}/")
    }
    assert divergences == expected_divergences, (
        f"{model_name}: divergence ledger is stale. A new entry is a finding "
        f"against lazily-py, never a reason to edit the fixture "
        f"(observed: {sorted(divergences)}, "
        f"documented: {sorted(expected_divergences)})"
    )

    # Positive assertion: the runner must have actually executed something.
    assert len(replayed) > 0, f"{model_name}: replayed zero fixtures"
    assert total_ops > 0, f"{model_name}: executed zero ops"
    assert total_checks > 0, f"{model_name}: executed zero assertions"


@pytest.mark.parametrize("model_cls", MODELS, ids=lambda m: m.NAME)  # type: ignore[misc]
def test_reactive_graph_conformance(model_cls: Any) -> None:
    """Replay the reactive-graph corpus against one context."""
    _run_corpus(model_cls)


def test_every_shipped_context_is_covered() -> None:
    """Guards the requirement that gives this runner its value.

    Replaying only the default context is how the dart and go async cascade
    defects hid. If lazily-py gains a context, it must be added to MODELS.
    """
    covered = {m.NAME for m in MODELS}
    assert covered == {"Context", "ThreadSafeContext", "AsyncContext"}, (
        f"MODELS does not cover every shipped context: {sorted(covered)}"
    )


def test_spec_fixtures_are_present() -> None:
    """The canonical fixtures must be reachable, or the suite tests nothing.

    Mirrors lazily-rs (#lzspecconf): skip loudly rather than fail locally,
    because a checkout without the sibling is a normal developer state. CI
    closes the hole — ``.github/workflows/precommit.yml`` clones lazily-spec and
    then asserts this directory exists, so the skip cannot mask an untested run
    there.
    """
    if not _SPEC_PATH.is_dir():
        pytest.skip(f"skipping: {SPEC_DIR} absent - run with the lazily-spec sibling")
    assert _fixture_paths(), f"{SPEC_DIR} present but holds no fixtures"
