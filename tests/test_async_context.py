"""``AsyncContext`` conformance tests (``lazily-spec/docs/async.md``).

Each test names the conformance point from ``async.md`` § "Conformance" that it
pins. There are no JSON fixtures for the async surface in ``lazily-spec``
(``conformance/`` has no async directory), so — as ``async.md`` § "Implementation
status" itself prescribes — coverage is "targeted deterministic tests rather than
exhaustive interleaving exploration". These mirror the shape of ``lazily-rs``'s
``async_integration.rs`` / ``async_state_machine.rs`` / ``async_resolve_loop.rs``.
"""

from __future__ import annotations

import asyncio

import pytest

from lazily import (
    AsyncCellHandle,
    AsyncComputeContext,
    AsyncComputed,
    AsyncComputedState,
    AsyncContext,
    AsyncContextDisposedError,
    AsyncSlotHandle,
    AsyncSource,
    EffectState,
    SlotState,
)


async def _settle() -> None:
    """Yield to the loop enough times for spawned drive/flush tasks to advance
    to their next suspension point."""
    for _ in range(8):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Point 1 — the slot state machine and its transitions
# --------------------------------------------------------------------------- #


def test_slot_starts_empty_and_resolves() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        slot = ctx.computed(lambda cc: _const(7))
        assert slot.state is SlotState.EMPTY
        assert slot.get() is None
        assert await slot.get_async() == 7
        assert slot.state is SlotState.RESOLVED
        # Warm fast path: the cached read no longer spawns.
        assert slot.get() == 7

    asyncio.run(scenario())


async def _const[T](value: T) -> T:
    return value


def test_error_state_then_retry_transitions_back_to_computing() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        attempts: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise ValueError("boom")
            return 42

        slot = ctx.computed(compute)
        with pytest.raises(ValueError, match="boom"):
            await slot.get_async()
        assert slot.state is SlotState.ERROR
        # Error -> Computing on the next get_async (retry).
        assert await slot.get_async() == 42
        assert slot.state is SlotState.RESOLVED

    asyncio.run(scenario())


def test_invalidation_moves_resolved_back_to_computing() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(2)
        slot = ctx.computed(lambda cc: _const(cc.get(src) * 10))
        assert await slot.get_async() == 20
        assert slot.state is SlotState.RESOLVED
        ctx.set(src, 3)
        assert slot.state is SlotState.COMPUTING
        assert slot.get() is None  # never a stale cached value
        assert await slot.get_async() == 30

    asyncio.run(scenario())


def test_equal_write_is_guarded_and_does_not_invalidate() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(2)
        runs: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get(src)
            runs.append(v)
            return v

        slot = ctx.computed(compute)
        assert await slot.get_async() == 2
        ctx.set(src, 2)  # equal write — PartialEq guard
        assert slot.state is SlotState.RESOLVED
        assert runs == [2]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 2 — revision tracking discards every stale completion
# --------------------------------------------------------------------------- #


def test_stale_completion_is_discarded_not_published() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        gate = asyncio.Event()
        seen: list[int] = []
        published: list[int | None] = []

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get(src)
            seen.append(v)
            if v == 1:
                await gate.wait()
            return v * 10

        slot = ctx.computed(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        assert seen == [1]  # suspended mid-compute

        # Invalidate while the first computation is in flight.
        ctx.set(src, 2)
        assert slot.revision == 1
        gate.set()  # the stale computation now completes with 10
        await _settle()
        published.append(slot.get())

        assert await task == 20  # the fresh revision's value, never 10
        assert seen == [1, 2]
        # The stale 10 was never published at any point.
        assert 10 not in [p for p in published if p is not None]
        assert slot.get() == 20

    asyncio.run(scenario())


def test_stale_error_is_discarded_and_the_slot_re_resolves() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        gate = asyncio.Event()

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get(src)
            if v == 1:
                await gate.wait()
                raise ValueError("stale failure")
            return v

        slot = ctx.computed(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        ctx.set(src, 9)
        gate.set()
        # The stale error is discarded — it must not surface to the waiter.
        assert await task == 9
        assert slot.state is SlotState.RESOLVED

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 3 — the five cancellation properties
# --------------------------------------------------------------------------- #


def test_dropping_one_waiter_does_not_cancel_the_shared_computation() -> None:
    """Cancellation property 1 — waiter cancellation is safe."""

    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()
        runs: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            runs.append(1)
            await gate.wait()
            return 5

        slot = ctx.computed(compute)
        a = asyncio.create_task(slot.get_async())
        b = asyncio.create_task(slot.get_async())
        await _settle()
        a.cancel()  # drop one waiter
        await _settle()
        gate.set()
        assert await b == 5  # the surviving waiter still gets the value
        assert runs == [1]  # in-flight deduplication: computed exactly once

    asyncio.run(scenario())


def test_concurrent_get_async_callers_share_one_in_flight_computation() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()
        runs: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            runs.append(1)
            await gate.wait()
            return 3

        slot = ctx.computed(compute)
        waiters = [asyncio.create_task(slot.get_async()) for _ in range(5)]
        await _settle()
        gate.set()
        assert await asyncio.gather(*waiters) == [3, 3, 3, 3, 3]
        assert runs == [1]

    asyncio.run(scenario())


def test_hard_clear_cancels_the_in_flight_revision() -> None:
    """Cancellation property 3 — explicit cancellation."""

    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()
        values = iter([100, 200])

        async def compute(cc: AsyncComputeContext) -> int:
            v = next(values)
            if v == 100:
                await gate.wait()
            return v

        slot = ctx.computed(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        slot.hard_clear()
        assert slot.state is SlotState.EMPTY
        gate.set()
        assert await task == 200  # the cleared revision's 100 was discarded

    asyncio.run(scenario())


def test_context_disposal_awaits_cleanup_and_makes_reads_inert() -> None:
    """Cancellation property 4 — context disposal."""

    async def scenario() -> None:
        ctx = AsyncContext()
        order: list[str] = []

        async def body(cc: AsyncComputeContext) -> object:
            order.append("body")

            async def cleanup() -> None:
                await asyncio.sleep(0)
                order.append("cleanup")

            return cleanup

        effect = ctx.effect_async(body)
        await effect.settle()
        # The cleanup is retained, so it is still outstanding at disposal —
        # without this the assertion below would hold vacuously and prove
        # nothing about what disposal awaits.
        assert order == ["body"]

        slot = ctx.computed(lambda cc: _const(1))
        await ctx.dispose_async()
        # Disposal ran and awaited every active cleanup future before returning.
        assert order == ["body", "cleanup"]
        assert effect.state is EffectState.DISPOSED
        assert ctx.disposed
        with pytest.raises(AsyncContextDisposedError):
            await slot.get_async()

    asyncio.run(scenario())


def test_disposal_cancels_in_flight_computations() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()

        async def compute(cc: AsyncComputeContext) -> int:
            await gate.wait()
            return 1

        slot = ctx.computed(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        await ctx.dispose_async()
        gate.set()
        # The in-flight revision was hard-cleared, so its completion is
        # discarded; the re-resolving waiter sees the disposed context.
        with pytest.raises(AsyncContextDisposedError):
            await task

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 4 — the get_async re-resolve contract
# --------------------------------------------------------------------------- #


def test_resolved_since_get_window_is_benign() -> None:
    """Benign window 1 — the slot may transition Computing -> Resolved between
    the fast-path read and the re-lock. Re-resolving must return the published
    value, not panic."""

    async def scenario() -> None:
        ctx = AsyncContext()
        gate = asyncio.Event()

        async def compute(cc: AsyncComputeContext) -> int:
            await gate.wait()
            return 11

        slot = ctx.computed(compute)
        first = asyncio.create_task(slot.get_async())
        await _settle()
        gate.set()
        assert await first == 11
        # A caller entering after publication takes the cached path.
        assert await slot.get_async() == 11

    asyncio.run(scenario())


def test_repeated_invalidation_during_compute_re_resolves_without_error() -> None:
    """Benign window 2 — repeatedly superseding the in-flight compute must loop,
    not raise."""

    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(0)
        gates: dict[int, asyncio.Event] = {}

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get(src)
            gate = gates.setdefault(v, asyncio.Event())
            if v < 3:
                await gate.wait()
            return v

        slot = ctx.computed(compute)
        task = asyncio.create_task(slot.get_async())
        for nxt in (1, 2, 3):
            await _settle()
            ctx.set(src, nxt)
            for g in gates.values():
                g.set()
        assert await task == 3

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 5 — dependency tracking through the compute context
# --------------------------------------------------------------------------- #


def test_edges_register_before_the_awaited_read() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        gate = asyncio.Event()

        async def compute(cc: AsyncComputeContext) -> int:
            v = cc.get(src)
            await gate.wait()
            return v

        slot = ctx.computed(compute)
        task = asyncio.create_task(slot.get_async())
        await _settle()
        # Suspended at the await — the edge is already live, so a write now
        # supersedes the in-flight computation rather than being missed.
        ctx.set(src, 2)
        assert slot.revision == 1
        gate.set()
        assert await task == 2

    asyncio.run(scenario())


def test_invalidation_propagates_through_the_transitive_cone() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(2)
        a = ctx.computed(lambda cc: _const(cc.get(src) + 1))
        b = ctx.computed(lambda cc: cc.get_async(a))
        assert await b.get_async() == 3
        ctx.set(src, 10)
        # src -> a -> b: the whole cone is stale, not just the direct dependent.
        assert a.state is SlotState.COMPUTING
        assert b.state is SlotState.COMPUTING
        assert await b.get_async() == 11

    asyncio.run(scenario())


def test_stale_dependencies_are_detached_on_rerun() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        which = ctx.source(True)
        left = ctx.source(1)
        right = ctx.source(100)
        runs: list[int] = []

        async def compute(cc: AsyncComputeContext) -> int:
            flag = cc.get(which)
            v = cc.get(left if flag else right)
            runs.append(v)
            return v

        slot = ctx.computed(compute)
        assert await slot.get_async() == 1
        ctx.set(which, False)
        assert await slot.get_async() == 100
        # `left` is no longer a dependency — writing it must not invalidate.
        ctx.set(left, 999)
        assert slot.state is SlotState.RESOLVED
        assert runs == [1, 100]

    asyncio.run(scenario())


def test_memo_guard_republishes_the_previous_value_object() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        slot = ctx.computed_with_equals(
            lambda cc: _const([cc.get(src) % 2]),
            lambda a, b: a == b,
        )
        first = await slot.get_async()
        ctx.set(src, 3)  # 3 % 2 == 1 — equal under the memo guard
        again = await slot.get_async()
        assert again == [1]
        assert again is first  # identity preserved: nothing new published

    asyncio.run(scenario())


def test_computed_ripple_when_gates_on_custom_predicate() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(0)
        # Propagate only when the /10 bucket changes; suppress within a bucket.
        slot = ctx.computed_ripple_when(
            lambda cc: _const((cc.get(src), cc.get(src) // 10)),
            lambda old, new: old[1] != new[1],
        )
        first = await slot.get_async()
        assert first == (0, 0)

        ctx.set(src, 3)  # same bucket -> suppressed, previous object republished
        again = await slot.get_async()
        assert again is first

        ctx.set(src, 12)  # bucket changed -> propagates a fresh value
        crossed = await slot.get_async()
        assert crossed == (12, 1)
        assert crossed is not first

    asyncio.run(scenario())


def test_computed_ripple_when_always_propagate_is_pass_through() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(0)
        # Always-propagate predicate: even an equal recompute publishes anew.
        slot = ctx.computed_ripple_when(
            lambda cc: _const([cc.get(src) * 0]),  # always [0]
            lambda old, new: True,
        )
        first = await slot.get_async()
        ctx.set(src, 5)
        again = await slot.get_async()
        assert again == [0]
        assert again is not first  # pass-through: a fresh object each time

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 6 — async effects: serialized, cleanup-before-body, executor-scheduled
# --------------------------------------------------------------------------- #


def test_effect_reruns_are_scheduled_not_inline() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        runs: list[int] = []

        async def body(cc: AsyncComputeContext) -> None:
            runs.append(cc.get(src))

        effect = ctx.effect_async(body)
        assert runs == []  # the initial run is scheduled, not inline
        await effect.settle()
        assert runs == [1]

        ctx.set(src, 2)
        assert runs == [1]  # still not inline within set_cell
        await effect.settle()
        assert runs == [1, 2]

    asyncio.run(scenario())


def test_effect_cleanup_is_triggered_by_rerun_or_dispose() -> None:
    """Cancellation property 5 — the cleanup *trigger*, and its ordering.

    The ordering half ("cleanup completes before the next body") is satisfied
    vacuously by eager end-of-flush cleanup, so the trigger is asserted first:
    after a body settles, its cleanup has not run.
    """

    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        order: list[str] = []

        async def body(cc: AsyncComputeContext) -> object:
            v = cc.get(src)
            order.append(f"body{v}")

            async def cleanup() -> None:
                await asyncio.sleep(0)
                order.append(f"cleanup{v}")

            return cleanup

        effect = ctx.effect_async(body)
        await effect.settle()
        # The first body has run and its cleanup is retained, not executed:
        # the effect is still live and will rerun.
        assert order == ["body1"]
        # The rerun is the trigger, and the cleanup precedes the next body.
        ctx.set(src, 2)
        await effect.settle()
        assert order == ["body1", "cleanup1", "body2"]
        # Disposal is the other trigger, and runs the last retained cleanup.
        await effect.dispose_async()
        assert order == ["body1", "cleanup1", "body2", "cleanup2"]
        assert effect.state is EffectState.DISPOSED

    asyncio.run(scenario())


def test_effect_accepts_a_synchronous_cleanup() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        order: list[str] = []

        async def body(cc: AsyncComputeContext) -> object:
            order.append("body")
            return lambda: order.append("cleanup")

        effect = ctx.effect_async(body)
        await effect.settle()
        await effect.dispose_async()
        assert order == ["body", "cleanup"]

    asyncio.run(scenario())


def test_effect_reruns_are_serialized_never_overlapping() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(0)
        depth = [0]
        max_depth = [0]

        async def body(cc: AsyncComputeContext) -> None:
            cc.get(src)
            depth[0] += 1
            max_depth[0] = max(max_depth[0], depth[0])
            await asyncio.sleep(0)
            depth[0] -= 1

        effect = ctx.effect_async(body)
        for i in range(1, 5):
            ctx.set(src, i)
        await effect.settle()
        assert max_depth[0] == 1  # bodies never overlap

    asyncio.run(scenario())


def test_disposed_effect_does_not_rerun() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        runs: list[int] = []

        async def body(cc: AsyncComputeContext) -> None:
            runs.append(cc.get(src))

        effect = ctx.effect_async(body)
        await effect.settle()
        await ctx.dispose_async_effect(effect)
        ctx.set(src, 2)
        await _settle()
        assert runs == [1]

    asyncio.run(scenario())


def test_effect_body_error_does_not_wedge_the_effect() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        runs: list[int] = []

        async def body(cc: AsyncComputeContext) -> None:
            v = cc.get(src)
            runs.append(v)
            if v == 1:
                raise ValueError("boom")

        effect = ctx.effect_async(body)
        await effect.settle()
        ctx.set(src, 2)
        await effect.settle()
        assert runs == [1, 2]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Point 7 — batching is synchronous; async reruns fire after the outermost exit
# --------------------------------------------------------------------------- #


def test_batch_coalesces_into_one_effect_rerun() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        a = ctx.source(1)
        b = ctx.source(1)
        runs: list[tuple[int, int]] = []

        async def body(cc: AsyncComputeContext) -> None:
            runs.append((cc.get(a), cc.get(b)))

        effect = ctx.effect_async(body)
        await effect.settle()
        assert runs == [(1, 1)]

        def writes() -> None:
            ctx.set(a, 2)
            ctx.set(b, 3)
            assert runs == [(1, 1)]  # nothing runs inside the batch callback

        ctx.batch(writes)
        await effect.settle()
        # One coalesced rerun observing both writes, not two.
        assert runs == [(1, 1), (2, 3)]

    asyncio.run(scenario())


def test_nested_batches_flush_only_at_the_outermost_boundary() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        runs: list[int] = []

        async def body(cc: AsyncComputeContext) -> None:
            runs.append(cc.get(src))

        effect = ctx.effect_async(body)
        await effect.settle()

        def inner() -> None:
            ctx.set(src, 2)

        def outer() -> None:
            ctx.batch(inner)
            assert runs == [1]  # the inner exit did not flush
            ctx.set(src, 3)

        ctx.batch(outer)
        await effect.settle()
        assert runs == [1, 3]

    asyncio.run(scenario())


def test_batch_returns_the_callback_value() -> None:
    ctx = AsyncContext()
    assert ctx.batch(lambda: "done") == "done"


def test_batched_slot_invalidation_defers_to_the_outermost_exit() -> None:
    async def scenario() -> None:
        ctx = AsyncContext()
        src = ctx.source(1)
        slot = ctx.computed(lambda cc: _const(cc.get(src)))
        assert await slot.get_async() == 1

        def writes() -> None:
            ctx.set(src, 5)
            # Invalidation is queued, not applied, inside the batch.
            assert slot.state is SlotState.RESOLVED

        ctx.batch(writes)
        assert slot.state is SlotState.COMPUTING
        assert await slot.get_async() == 5

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Public API surface
# --------------------------------------------------------------------------- #


def test_context_api_surface_matches_the_spec_table() -> None:
    for name in (
        "cell",
        "get_cell",
        "set_cell",
        "computed",
        "get",
        "get_async",
        "computed_with_equals",
        "computed_ripple_when",
        "effect_async",
        "dispose_async_effect",
        "dispose_async",
        "batch",
    ):
        assert callable(getattr(AsyncContext, name)), name


def test_handles_are_exported_from_the_package_root() -> None:
    import lazily

    for name in (
        "AsyncContext",
        "AsyncCellHandle",
        "AsyncSource",
        "AsyncComputed",
        "AsyncComputedState",
        "AsyncSlotHandle",
        "AsyncEffectHandle",
        "AsyncComputeContext",
        "AsyncContextDisposedError",
    ):
        assert name in lazily.__all__, name
        assert hasattr(lazily, name), name


def test_cell_and_slot_handle_types_are_usable_in_annotations() -> None:
    ctx = AsyncContext()
    c: AsyncSource[int] = ctx.source(1)
    s: AsyncComputed[int] = ctx.computed(lambda cc: _const(2))
    assert c.get() == 1
    assert c.peek == 1
    assert repr(c) == "AsyncSource(1)"
    assert s.get() is None


def test_async_v2_alias_identity_and_deprecated_factories() -> None:
    assert AsyncCellHandle is AsyncSource
    assert AsyncSlotHandle is AsyncComputed
    assert AsyncComputedState is SlotState

    async def scenario() -> None:
        ctx = AsyncContext()
        with pytest.warns(DeprecationWarning, match="computed_async"):
            computed = ctx.computed_async(lambda cc: _const(3))
        assert type(computed) is AsyncComputed
        assert isinstance(computed, AsyncSlotHandle)
        assert await computed.get_async() == 3

        with pytest.warns(DeprecationWarning, match="memo_async"):
            memo = ctx.memo_async(
                lambda cc: _const("value"),
                lambda left, right: left == right,
            )
        assert type(memo) is AsyncComputed
        assert await memo.get_async() == "value"

    asyncio.run(scenario())
