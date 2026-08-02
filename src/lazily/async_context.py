"""Async reactive context — the coordination layer over the async primitives.

The Python counterpart of ``lazily-spec/docs/async.md``. This is the eighth
binding's async surface; the closest structural sibling is ``lazily-dart``'s
``AsyncContext``, because both languages are garbage-collected and object-shaped.

**What this module is.** A *thin coordination wrapper*, in the same idiom as
:class:`~lazily.thread_safe.ThreadSafeContext`. The reactive state machines
already live in :mod:`lazily.async_slot` (``Empty``/``Computing``/``Resolved``/
``Error`` with revision-tracked stale-completion discard and one-in-flight-per-
revision deduplication) and :mod:`lazily.async_effect` (queued reruns,
cleanup-before-body, terminal disposal). :class:`AsyncContext` adds only what a
single primitive cannot own by itself:

* the **dependency graph** between handles (dependency -> dependents), so an
  input write invalidates the transitive cone,
* the **synchronous batch boundary** (``batch``), so writes coalesce into one
  invalidation pass and async reruns fire only after the outermost batch exits,
* **executor scheduling** of effect reruns (a rerun is a task on the loop, never
  inline within ``set_cell``/``batch``), and
* **context disposal**, which cancels in-flight computations and awaits every
  active cleanup future.

**What this module deliberately is not.** It is *not* a node arena. ``lazily-rs``
models its async context as a table of nodes addressed by integer handles, so
its context necessarily carries ``dispose_computed``/``dispose_source``, teardown
scopes, and degree introspection. In ``lazily-py`` every reactive primitive is a
real object that owns its own state, disposal is handle-side
(:meth:`AsyncEffectHandle.dispose_async`, matching :meth:`lazily.effect.Effect.dispose`),
and the synchronous surface has no ``Context`` object at all. Adding an arena
here would be a foreign architecture, so the context owns edges and scheduling
only.

**Dependency tracking is compute-context based, never ambient.** ``async.md``
§ "Dependency tracking" forbids a thread-local tracking stack because it does not
survive suspension across ``await``. Python makes this especially sharp: calling
an ``async def`` returns a coroutine without executing a single line of its body,
so an ambient "currently-computing" global would capture nothing at all. Reads
therefore go through :class:`AsyncComputeContext`, which registers the edge
*before* the awaited read.
"""

from __future__ import annotations


__all__ = [
    "AsyncCellHandle",
    "AsyncComputeContext",
    "AsyncComputed",
    "AsyncComputedState",
    "AsyncContext",
    "AsyncContextDisposedError",
    "AsyncEffectHandle",
    "AsyncSlotHandle",
    "AsyncSource",
    "AsyncTeardownScope",
]

import asyncio
import warnings
from typing import TYPE_CHECKING, Any

from .async_effect import AsyncEffect, EffectState
from .async_slot import AsyncSlot, SlotState
from .slot import DisposedError


if TYPE_CHECKING:
    import builtins
    from collections.abc import Awaitable, Callable


_MISSING: Any = object()


class AsyncContextDisposedError(RuntimeError):
    """Raised when an async read is attempted on a disposed context."""


class AsyncSource[T]:
    """A mutable input cell on the async graph — the *synchronous* input layer.

    Writes are synchronous and invalidate the dependent cone (or queue it, inside
    a :meth:`AsyncContext.batch`). Reads through :meth:`get` are non-reactive;
    an async compute or effect body registers a dependency edge by reading via
    :meth:`AsyncComputeContext.get_cell`, per ``async.md`` § "Dependency
    tracking".
    """

    __slots__ = ("_ctx", "_disposed", "_value")

    def __init__(self, ctx: AsyncContext, value: T) -> None:
        self._ctx = ctx
        self._value = value
        self._disposed = False

    @property
    def disposed(self) -> bool:
        """Whether this cell has been torn down (terminal)."""
        return self._disposed

    def get(self) -> T:
        """Read the current value (synchronous, non-reactive)."""
        if self._disposed:
            raise DisposedError("read of disposed async cell")
        return self._value

    @property
    def peek(self) -> T:
        """Alias of :meth:`get`, for parity with the other bindings."""
        return self.get()

    def set(self, value: T) -> None:
        """Write a new value. Guarded by ``!=`` (the same PartialEq guard the
        synchronous :class:`~lazily.cell.Cell` uses), so an equal write is a
        no-op and never invalidates. A write to a disposed cell is inert."""
        if self._disposed:
            return
        if value != self._value:
            self._value = value
            self._ctx._invalidate_dependents(self)

    def __repr__(self) -> str:
        return f"AsyncSource({self._value!r})"


class AsyncComputed[T]:
    """A computed async slot: a lazily-computed, memoized, awaited derived value.

    The lifecycle is *not* reimplemented here — it is delegated wholesale to
    :class:`~lazily.async_slot.AsyncSlot`, which supplies the four-state machine,
    revision-tracked stale-completion discard (conformance point 2), the
    ``get_async`` re-resolve loop across both benign race windows (point 4), and
    one-in-flight-computation-per-revision deduplication. This handle adds the
    two things the bare slot has no way to know about: dependency edges into the
    owning context, and an optional equality memo guard.
    """

    __slots__ = (
        "_compute",
        "_ctx",
        "_dependencies",
        "_disposed",
        "_eq",
        "_memo",
        "_slot",
    )

    def __init__(
        self,
        ctx: AsyncContext,
        compute: Callable[[AsyncComputeContext], Awaitable[T]],
        *,
        eq: Callable[[T, T], bool] | None = None,
    ) -> None:
        self._ctx = ctx
        self._compute = compute
        self._eq = eq
        self._memo: Any = _MISSING
        self._dependencies: set[object] = set()
        self._disposed = False
        self._slot: AsyncSlot[T] = AsyncSlot(self._run)

    @property
    def disposed(self) -> bool:
        """Whether this slot has been torn down (terminal)."""
        return self._disposed

    # -- lifecycle delegation ------------------------------------------- #

    @property
    def state(self) -> SlotState:
        """The delegated :class:`~lazily.async_slot.SlotState`."""
        return self._slot.state

    @property
    def revision(self) -> int:
        """The delegated revision counter."""
        return self._slot.revision

    def get(self) -> T | None:
        """Synchronous cached read: the value when ``Resolved``, else ``None``.
        The warm fast path; never spawns a computation."""
        if self._disposed:
            raise DisposedError("read of disposed async slot")
        return self._slot.get()

    async def get_async(self) -> T:
        """Await the slot's value, attaching to the in-flight computation for the
        current revision rather than spawning a duplicate.

        The delegated read is **shielded**. :class:`AsyncSlot` hands every
        concurrent waiter the *same* :class:`asyncio.Future`, and cancelling a
        task that is awaiting a future cancels that future — so without the
        shield, one caller abandoning its ``get_async`` would cancel the shared
        in-flight computation out from under the remaining waiters. Cancellation
        property 1 requires the opposite: dropping one waiter is safe.
        """
        if self._disposed:
            raise DisposedError("read of disposed async slot")
        if self._ctx._disposed:
            raise AsyncContextDisposedError("AsyncContext disposed")
        return await asyncio.shield(self._slot.get_async())

    def invalidate(self) -> None:
        """Mark the slot stale and propagate through the dependent cone."""
        self._slot.invalidate()
        self._ctx._invalidate_dependents(self)

    def hard_clear(self) -> None:
        """Reset to ``Empty`` and bump the revision, discarding any in-flight
        completion (explicit cancellation, conformance point 3)."""
        self._slot.hard_clear()

    # -- internal -------------------------------------------------------- #

    async def _run(self) -> T:
        """The compute body handed to the delegated :class:`AsyncSlot`.

        Detaches the previous dependency set before re-tracking (``async.md``:
        "on rerun, stale dependencies are removed and new ones registered"), runs
        the user compute against a fresh :class:`AsyncComputeContext`, and applies
        the memo guard.
        """
        if self._ctx._disposed:
            # Disposal is checked *inside* the compute, not only at the
            # ``get_async`` entry: the re-resolve loop lives in the delegated
            # :class:`AsyncSlot`, so a waiter that is mid-loop when the context
            # is disposed would otherwise respawn and publish a value from a
            # disposed graph (cancellation property 4).
            raise AsyncContextDisposedError("AsyncContext disposed")
        self._detach()
        prior = self._memo
        value = await self._compute(AsyncComputeContext(self._ctx, self))
        eq = self._eq
        if eq is not None and prior is not _MISSING and eq(prior, value):
            # Memo equality suppression: keep the previously published value
            # (identity preserved), so nothing downstream sees a new object.
            return prior
        self._memo = value
        return value

    def _detach(self) -> None:
        for dep in self._dependencies:
            edges = self._ctx._dependents.get(dep)
            if edges is not None:
                edges.discard(self)
        self._dependencies.clear()

    def _track(self, dependency: object) -> None:
        if dependency not in self._dependencies:
            self._dependencies.add(dependency)
            self._ctx._dependents.setdefault(dependency, set()).add(self)


# Published v1 compatibility names are aliases, not subclasses. Identity is
# load-bearing for isinstance checks and native/mypyc callers.
AsyncCellHandle = AsyncSource
AsyncSlotHandle = AsyncComputed

# Public value-oriented projection of the formal storage-oriented slot state.
# `SlotState` remains the runtime/formal compatibility name.
AsyncComputedState = SlotState


class AsyncEffectHandle:
    """An async effect: a side-effecting observer that reruns when a tracked
    dependency invalidates, with an optional (sync or async) cleanup.

    The scheduling lifecycle is delegated to
    :class:`~lazily.async_effect.AsyncEffect`, which supplies queue-never-fire
    invalidation, cleanup-before-body ordering, and terminal disposal
    (conformance points 5 and 6). This handle adds dependency edges and the
    executor hop: a rerun is dispatched as an :mod:`asyncio` task, so it never
    runs inline within ``set_cell`` or a ``batch`` callback.
    """

    __slots__ = ("_body", "_ctx", "_dependencies", "_disposed", "_effect", "_task")

    def __init__(
        self,
        ctx: AsyncContext,
        body: Callable[[AsyncComputeContext], Awaitable[Any]],
    ) -> None:
        self._ctx = ctx
        self._body = body
        self._disposed = False
        self._dependencies: set[object] = set()
        self._effect = AsyncEffect(self._run)
        self._task: asyncio.Task[None] | None = None

    @property
    def state(self) -> EffectState:
        """The delegated :class:`~lazily.async_effect.EffectState`."""
        return self._effect.state

    @property
    def disposed(self) -> bool:
        """Whether this effect has been torn down (terminal)."""
        return self._disposed

    async def dispose_async(self) -> None:
        """Remove pending reruns, await the in-flight body and cleanup, detach
        dependency edges, and go terminal."""
        self._disposed = True
        self._ctx._effects.discard(self)
        task = self._task
        self._task = None
        if task is not None and not task.done():
            await task
        await self._effect.dispose()
        self._detach()

    async def settle(self) -> None:
        """Await the currently scheduled rerun (if any).

        The spec requires reruns to be *scheduled*, not inline, which means a
        synchronous ``set_cell`` returns before the effect body has run. Tests
        and callers that need the post-invalidation steady state await this."""
        while True:
            task = self._task
            if task is None or task.done():
                return
            await task

    # -- internal -------------------------------------------------------- #

    def _schedule(self) -> None:
        """Queue a rerun and ensure a flush task is pending on the executor."""
        if self._disposed:
            return
        self._effect.invalidate()
        if self._task is not None and not self._task.done():
            # A flush is already in flight; its loop picks up the new queued
            # rerun (after the current cleanup completes).
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: the rerun stays queued and fires on the next
            # settle()/flush from inside a loop.
            self._task = None
            return
        self._task = loop.create_task(self._effect.flush())

    async def _run(self) -> Callable[[], Awaitable[None]] | None:
        if self._disposed or self._ctx._disposed:
            return None
        self._detach()
        result = await self._body(AsyncComputeContext(self._ctx, self))
        if result is None:
            return None
        if callable(result):
            return _as_async_cleanup(result)
        return None

    def _detach(self) -> None:
        for dep in self._dependencies:
            edges = self._ctx._dependents.get(dep)
            if edges is not None:
                edges.discard(self)
        self._dependencies.clear()

    def _track(self, dependency: object) -> None:
        if dependency not in self._dependencies:
            self._dependencies.add(dependency)
            self._ctx._dependents.setdefault(dependency, set()).add(self)


def _as_async_cleanup(fn: Callable[[], Any]) -> Callable[[], Awaitable[None]]:
    """Normalize a sync-or-async cleanup callable into an awaitable one."""

    async def _cleanup() -> None:
        result = fn()
        if asyncio.iscoroutine(result):
            await result

    return _cleanup


class AsyncComputeContext:
    """The reader handed to an async compute or effect body.

    Every read through this object registers the dependency edge **before** the
    awaited read, so an invalidation arriving while the computation is suspended
    supersedes it rather than letting it publish stale data. This replaces the
    ambient tracking stack the synchronous graph uses, which ``async.md`` forbids
    on the async surface — and which would in any case capture nothing in Python,
    where calling an ``async def`` executes none of its body.
    """

    __slots__ = ("_ctx", "_node")

    def __init__(
        self, ctx: AsyncContext, node: AsyncComputed[Any] | AsyncEffectHandle
    ) -> None:
        self._ctx = ctx
        self._node = node

    def get_cell[T](self, cell: AsyncSource[T]) -> T:
        """Deprecated: use :meth:`get`, which reads both source and computed
        handles. Reads a cell synchronously, recording it as a dependency."""
        warnings.warn(
            "get_cell() is deprecated; use get() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self._node._track(cell)
        return cell.get()

    async def get_async[T](self, slot: AsyncComputed[T]) -> T:
        """Await a slot's value, recording the edge *before* the awaited read."""
        self._node._track(slot)
        return await slot.get_async()

    def get[T](self, handle: AsyncSource[T] | AsyncComputed[T]) -> T | None:
        """Non-blocking cached read of a **source cell or computed slot**,
        recording it as a dependency. The unified reader: a source
        (:class:`AsyncSource`) returns its current value; a computed
        (:class:`AsyncComputed`) returns its cached value or ``None``."""
        self._node._track(handle)
        return handle.get()


class AsyncContext:
    """The async reactive surface: a dependency graph, a batch boundary, and an
    executor for effect reruns.

    A distinct graph from the synchronous primitives — not an overload of them —
    because futures introduce in-flight state, cancellation, stale completion,
    and dependency tracking across suspension points that the synchronous graph
    has no notion of. Only *resolved* slot values ever cross IPC/FFI, as ordinary
    cell payloads: this is compute, not protocol.
    """

    __slots__ = (
        "_batch_queue",
        "_dependents",
        "_depth",
        "_disposed",
        "_effects",
        "_slots",
    )

    def __init__(self) -> None:
        self._dependents: dict[object, set[Any]] = {}
        self._effects: set[AsyncEffectHandle] = set()
        self._slots: set[AsyncComputed[Any]] = set()
        self._disposed = False
        self._depth = 0
        self._batch_queue: list[object] = []

    @property
    def disposed(self) -> bool:
        return self._disposed

    # -- handles --------------------------------------------------------- #

    def source[T](self, value: T) -> AsyncSource[T]:
        """Create a mutable source (input) cell — the canonical constructor."""
        return AsyncSource(self, value)

    def cell[T](self, value: T) -> AsyncSource[T]:
        """Deprecated v1 alias for :meth:`source`."""
        warnings.warn(
            "cell() is deprecated; use source() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return AsyncSource(self, value)

    def get[T](self, handle: AsyncSource[T] | AsyncComputed[T]) -> T | None:
        """Read a **source cell or computed slot** (synchronous cached read).

        The unified reader over both handle kinds: a source
        (:class:`AsyncSource`) returns its current value; a computed
        (:class:`AsyncComputed`) returns its cached value or ``None`` (the warm
        fast path — never spawns a computation)."""
        return handle.get()

    def set[T](self, handle: AsyncSource[T], value: T) -> None:
        """Write a **source cell** and invalidate dependents. Only source handles
        are writable — a computed slot has no ``set`` (write protection). Inside a
        :meth:`batch`, the invalidation is queued and fires once at the outermost
        boundary."""
        handle.set(value)

    def get_cell[T](self, handle: AsyncSource[T]) -> T:
        """Deprecated: use :meth:`get`. Read a cell's value (synchronous)."""
        warnings.warn(
            "get_cell() is deprecated; use get() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return handle.get()

    def set_cell[T](self, handle: AsyncSource[T], value: T) -> None:
        """Deprecated: use :meth:`set`. Update a cell and invalidate dependents."""
        warnings.warn(
            "set_cell() is deprecated; use set() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        handle.set(value)

    def computed[T](
        self, compute: Callable[[AsyncComputeContext], Awaitable[T]]
    ) -> AsyncComputed[T]:
        """Create an async computed slot."""
        slot: AsyncComputed[T] = AsyncComputed(self, compute)
        self._slots.add(slot)
        return slot

    def computed_with_equals[T](
        self,
        compute: Callable[[AsyncComputeContext], Awaitable[T]],
        eq: Callable[[T, T], bool],
    ) -> AsyncComputed[T]:
        """Like :meth:`computed` with an equality memo guard: a recompute
        yielding an equal value republishes the *previous* value object."""
        slot: AsyncComputed[T] = AsyncComputed(self, compute, eq=eq)
        self._slots.add(slot)
        return slot

    def computed_async[T](
        self, compute: Callable[[AsyncComputeContext], Awaitable[T]]
    ) -> AsyncComputed[T]:
        """Deprecated v1 alias for :meth:`computed`."""
        warnings.warn(
            "computed_async() is deprecated; use computed() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.computed(compute)

    def memo_async[T](
        self,
        compute: Callable[[AsyncComputeContext], Awaitable[T]],
        eq: Callable[[T, T], bool],
    ) -> AsyncComputed[T]:
        """Deprecated guarded-computed constructor.

        ``memo`` is not a third async node kind.
        """
        warnings.warn(
            "memo_async() is deprecated; use computed_with_equals() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.computed_with_equals(compute, eq)

    def computed_ripple_when[T](
        self,
        compute: Callable[[AsyncComputeContext], Awaitable[T]],
        changed: Callable[[T, T], bool],
    ) -> AsyncComputed[T]:
        """Async :meth:`computed` with an explicit **propagate** predicate.

        The async mirror of the single-threaded
        :func:`lazily.signal.computed_ripple_when`: propagation is gated by
        ``changed(old, new)`` — ``True`` propagates the recompute downstream,
        ``False`` suppresses it (the previous value object is republished, as with
        :meth:`computed_with_equals`). It composes over the memo guard by negation, since
        the memo guard's ``eq`` is "equal = suppress" and ``changed`` is its
        complement: ``computed(f)`` ~ ``computed_ripple_when(f,
        !=)`` and the always-propagate ``changed`` is the pass-through.

        ``changed`` MUST be a **pure** function of ``(old, new)`` — value-carried
        state (a version/counter field) is fine, external mutable state is not.
        """
        return self.computed_with_equals(
            compute, lambda old, new: not changed(old, new)
        )

    def computed_ripple_when_async[T](
        self,
        compute: Callable[[AsyncComputeContext], Awaitable[T]],
        changed: Callable[[T, T], bool],
    ) -> AsyncComputed[T]:
        """Deprecated v1 alias for :meth:`computed_ripple_when`."""
        warnings.warn(
            "computed_ripple_when_async() is deprecated; "
            "use computed_ripple_when() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.computed_ripple_when(compute, changed)

    async def get_async[T](self, handle: AsyncComputed[T]) -> T:
        """Await a slot's value."""
        return await handle.get_async()

    def effect_async(
        self, body: Callable[[AsyncComputeContext], Awaitable[Any]]
    ) -> AsyncEffectHandle:
        """Create an async effect. The body may return a sync or async cleanup
        callable, which completes before the next body starts. The initial run is
        scheduled on the executor, not run inline."""
        handle = AsyncEffectHandle(self, body)
        self._effects.add(handle)
        handle._schedule()
        return handle

    async def dispose_async_effect(self, handle: AsyncEffectHandle) -> None:
        """Dispose an async effect and await its cleanup."""
        await handle.dispose_async()

    # -- per-node disposal (#lzspecedgeindex) ---------------------------- #

    def dispose_computed(self, handle: AsyncComputed[Any]) -> None:
        """Tear down one computed slot: detach both edge directions, discard any
        in-flight computation, and dirty whatever still reads it.

        Terminal and idempotent. Handles are plain references, so dropping every
        reference to a slot reclaims nothing on its own — the context's
        ``_dependents`` table holds a *strong* reference to each dependent, so a
        long-lived source otherwise retains every node that ever read it and
        grows without bound under subscribe/unsubscribe churn.
        """
        if handle._disposed:
            return
        handle._disposed = True
        self._slots.discard(handle)
        # Forward direction: stop being a dependent of everything this slot read.
        handle._detach()
        # Reverse direction: unlink the survivors, then dirty them.
        dependents = self._dependents.pop(handle, None)
        handle._slot.hard_clear()
        if dependents:
            for dependent in dependents:
                dependent._dependencies.discard(handle)
            self._dirty_disposed_dependents(dependents)

    def dispose_source(self, handle: AsyncSource[Any]) -> None:
        """Tear down one source cell. Cells read nothing, so only the downstream
        direction needs detaching. Same contract as :meth:`dispose_computed`."""
        if handle._disposed:
            return
        handle._disposed = True
        dependents = self._dependents.pop(handle, None)
        if dependents:
            for dependent in dependents:
                dependent._dependencies.discard(handle)
            self._dirty_disposed_dependents(dependents)

    def dispose_slot(self, handle: AsyncComputed[Any]) -> None:
        """Deprecated v1 alias for :meth:`dispose_computed`."""
        warnings.warn(
            "dispose_slot() is deprecated; use dispose_computed() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.dispose_computed(handle)

    def dispose_cell(self, handle: AsyncSource[Any]) -> None:
        """Deprecated v1 alias for :meth:`dispose_source`."""
        warnings.warn(
            "dispose_cell() is deprecated; use dispose_source() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.dispose_source(handle)

    def _dirty_disposed_dependents(self, roots: builtins.set[Any]) -> None:
        """Mark the surviving dependent cone stale — and schedule nothing.

        The async twin of :func:`lazily.slot._dirty_disposed_dependents`, and it
        exists for the same two reasons.

        *Dirty, because detaching is not enough.* A dependent holding a resolved
        value computed from the disposed node would keep serving it: the edge
        that would have invalidated it is the one disposal just removed. This is
        the defect ``lazily-rs`` 5db90d2 and ``lazily-js`` 4d20670 both fixed,
        and the async path is where it is hardest to notice.

        *Schedule nothing, because disposal is not a publish.* Reached effects
        are deliberately skipped rather than passed to ``_schedule``: an effect
        rerun during teardown re-enters a body that reads the node being torn
        down, so teardown would stop being idempotent. They stay subscribed and
        error on their next real rerun — the contract is "errors on next
        recompute", not "errors during dispose".
        """
        visited: set[int] = {id(r) for r in roots}
        frontier: list[Any] = list(roots)
        while frontier:
            node = frontier.pop()
            if isinstance(node, AsyncEffectHandle):
                continue
            node._slot.invalidate()
            for dependent in self._dependents.get(node, ()):
                if id(dependent) in visited:
                    continue
                visited.add(id(dependent))
                frontier.append(dependent)

    def scope(self) -> AsyncTeardownScope:
        """Open a teardown scope over this context. See
        :class:`AsyncTeardownScope`."""
        return AsyncTeardownScope(self)

    # -- degree introspection (#lzspecedgeindex) ------------------------- #

    def dependent_count(self, node: object) -> int:
        """How many nodes currently depend on ``node`` — reverse edge degree.

        A count, never the collection. This is the observable the disposal
        contract is written against: a subscribe/unsubscribe cycle that disposes
        what it creates must leave this at its starting value no matter how many
        cycles run, and a binding that leaks reports total-ever-created instead
        of live-subscriber count.
        """
        return len(self._dependents.get(node, ()))

    def dependency_count(self, node: object) -> int:
        """How many nodes ``node`` currently reads — forward edge degree.
        Always ``0`` for a cell, which is a pure source."""
        dependencies = getattr(node, "_dependencies", None)
        return 0 if dependencies is None else len(dependencies)

    # -- batch ----------------------------------------------------------- #

    def batch[R](self, run: Callable[[], R]) -> R:
        """A **synchronous** batch boundary. Cell writes inside ``run`` queue
        invalidation roots; the queued roots propagate once at the outermost
        exit, so a dependent reached through several changed cells is invalidated
        once (the coalesced frontier). Async reruns are scheduled at that exit
        and execute afterwards on the loop, never inside ``run``."""
        self._depth += 1
        try:
            return run()
        finally:
            self._depth -= 1
            if self._depth == 0:
                queue = self._batch_queue
                self._batch_queue = []
                seen: set[int] = set()
                for dep in queue:
                    if id(dep) in seen:
                        continue
                    seen.add(id(dep))
                    self._invalidate_dependents(dep)

    # -- graph ----------------------------------------------------------- #

    def _invalidate_dependents(self, dependency: object) -> None:
        """Propagate an invalidation through the transitive dependent cone.

        Slots are marked stale (their next ``get_async`` respawns against the new
        revision); effects queue a rerun on the executor. A ``visited`` set makes
        the walk terminate on a cyclic or diamond-shaped graph, and means a
        dependent reached through several changed sources is touched once.
        """
        if self._disposed:
            return
        if self._depth > 0:
            self._batch_queue.append(dependency)
            return
        visited: set[int] = {id(dependency)}
        frontier: list[object] = [dependency]
        while frontier:
            node = frontier.pop()
            for dependent in list(self._dependents.get(node, ())):
                if id(dependent) in visited:
                    continue
                visited.add(id(dependent))
                if isinstance(dependent, AsyncEffectHandle):
                    dependent._schedule()
                else:
                    dependent._slot.invalidate()
                    frontier.append(dependent)

    # -- disposal -------------------------------------------------------- #

    async def dispose_async(self) -> None:
        """Dispose the context: cancel every in-flight computation and await
        every active cleanup future before returning (conformance point 4).
        Subsequent cell writes and async reads are inert."""
        self._disposed = True
        effects = list(self._effects)
        if effects:
            await asyncio.gather(*(e.dispose_async() for e in effects))
        for slot in self._slots:
            slot.hard_clear()
        self._dependents.clear()
        self._effects.clear()
        self._slots.clear()


class AsyncTeardownScope:
    """A group of async nodes disposed together — the async twin of
    :class:`lazily.teardown.TeardownScope`.

    An *async* context manager, because disposing an async effect awaits its
    in-flight body and cleanup; a synchronous ``__exit__`` could only drop those
    on the floor::

        async with ctx.scope() as conn:
            doubled = conn.computed(...)
            conn.effect_async(...)
        # disposed here, in reverse creation order, cleanups awaited

    The imperative form the conformance fixtures use is the same object without
    the ``async with``: :meth:`aclose`.
    """

    __slots__ = ("_armed", "_ctx", "_owned")

    def __init__(self, ctx: AsyncContext) -> None:
        self._ctx = ctx
        self._owned: list[Any] = []
        self._armed = True

    # -- membership ------------------------------------------------------ #

    def source[T](self, value: T) -> AsyncSource[T]:
        """Create a source cell owned by this scope."""
        return self.adopt(self._ctx.source(value))

    def computed[T](
        self, compute: Callable[[AsyncComputeContext], Awaitable[T]]
    ) -> AsyncComputed[T]:
        """Create a computed slot owned by this scope."""
        return self.adopt(self._ctx.computed(compute))

    def effect_async(
        self, body: Callable[[AsyncComputeContext], Awaitable[Any]]
    ) -> AsyncEffectHandle:
        """Register an effect owned by this scope."""
        return self.adopt(self._ctx.effect_async(body))

    def adopt[N](self, node: N) -> N:
        """Take ownership of an existing node."""
        self._owned.append(node)
        return node

    # -- lifetime -------------------------------------------------------- #

    def __len__(self) -> int:
        """How many nodes this scope currently owns."""
        return len(self._owned)

    @property
    def armed(self) -> bool:
        """Whether closing this scope will dispose its members."""
        return self._armed

    def disarm(self) -> None:
        """Cancel this scope's teardown; closing it afterwards disposes nothing.
        The nodes themselves are untouched."""
        self._armed = False
        self._owned = []

    async def aclose(self) -> None:
        """Dispose every member in reverse creation order, awaiting each effect's
        cleanup. Idempotent.

        Reverse order because effect cleanups are side effects: a dependent's
        cleanup must not observe a graph where what it read is already gone.
        """
        if not self._armed:
            return
        owned = self._owned
        self._owned = []
        ctx = self._ctx
        for node in reversed(owned):
            if isinstance(node, AsyncEffectHandle):
                await node.dispose_async()
            elif isinstance(node, AsyncSource):
                ctx.dispose_source(node)
            elif isinstance(node, AsyncComputed):
                ctx.dispose_computed(node)
            else:
                # `adopt()` is generic, so anything can be handed to a scope.
                # The old `else` ran the AsyncComputed arm on it: a non-node
                # member was "disposed" by a code path that reads private slot
                # fields, and a scope that owned one silently leaked it.
                raise TypeError(f"scope member is not an async reactive node: {node!r}")

    async def __aenter__(self) -> AsyncTeardownScope:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        await self.aclose()
        return False
