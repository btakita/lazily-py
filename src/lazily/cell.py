__all__ = [
    "Cell",
    "CellSlot",
    "Source",
    "SourceSlot",
    "cell",
    "source",
    "source_def",
]

import warnings
from collections.abc import Callable
from typing import Any, TypeVar

from .batch import notify_change as _notify_change
from .slot import (
    BaseSlot,
    DisposedError,
    Slot,
    _ctx_base,
    _dirty_disposed_dependents,
    _drain_resets,
    _register_edge,
    _reset_work,
    mypyc_attr,
)


C_in = TypeVar("C_in", contravariant=True)
C_ctx = TypeVar("C_ctx", bound=dict)
T = TypeVar("T")


@mypyc_attr(allow_interpreted_subclasses=True)
class Source[T]:
    """A **source cell**: a mutable value, written from outside, that other
    reactives depend on. ``Source`` is the v2 Cell-kernel handle name
    (``#lzcellkernel``): its value comes from *outside* (``set`` / ``merge``),
    making it the writable value kind. It is the native mypyc class, so runtime
    identity, tracebacks, and introspection use the canonical name directly.
    :data:`Cell` remains an identity-preserving migration alias. A
    :class:`~lazily.merge.MergeCell` is a ``Source`` whose write folds under a
    non-``KeepLatest`` policy (``Cell ≡ Source(KeepLatest)``).

    **No reactive in this library exposes an observer API** — not Source, not
    :class:`~lazily.signal.Computed`. Observation in this graph is a declared
    dependency edge, not a registered callback: read the cell from a
    :class:`~lazily.signal.Computed` or :class:`~lazily.effect.Effect` and that
    reader becomes a dependent, which is
    what makes batching and glitch-freedom hold. A callback registry bypasses
    all of that and costs memory on every reactive whether or not anyone
    subscribes.

    Where a caller genuinely needs a stream of *every* transition rather than
    the settled value, that is a :class:`~lazily.queue.Topic`.
    """

    __slots__ = ("_disposed", "_parents", "_value", "ctx")

    _parents: set[Slot[Any, Any, Any]] | None
    _disposed: bool
    _value: T
    ctx: dict

    def __init__(self, ctx: Any, initial_value: T) -> None:
        # ``ctx`` may be a per-recompute :class:`~lazily.compute.Compute` view
        # (when a cell is constructed inside a compute body); capture the STABLE
        # underlying dict, never the transient wrapper (``#lzcellkernel`` item 3).
        self.ctx = _ctx_base(ctx)
        self._value = initial_value
        self._disposed = False
        # Auto-discovered parents (Slots/Signals/Effects reading this cell),
        # tracked by object identity so repeated reads never grow the fan-out.
        # Lazily materialized: an empty CPython ``set()`` is ~216 B, so
        # deferring it keeps quiescent leaf sources cheap.
        self._parents = None

    def __call__(self) -> T:
        return self.value

    def _subscribe(self, reader: Any) -> None:
        """Register ``self -> reader``: the recomputing ``reader`` depends on this
        cell. Called by a :class:`~lazily.slot.BoundHandle` on a tracked read
        (``name(ctx).value``) — value-threaded, no ambient stack."""
        _register_edge(self, reader)

    @property
    def value(self) -> T:
        if self._disposed:
            raise DisposedError("read of disposed cell")
        # Tracking is value-threaded, the sole surface (``#lzcellkernel``): a
        # tracked read subscribes the recomputing node via :meth:`_subscribe`
        # through the compute view (``Compute.read`` / ``BoundHandle``) *before*
        # this getter runs. A bare ``cell.value`` with no threaded ctx is simply
        # untracked — there is no ambient "current node".
        return self._value

    @value.setter
    def value(self, value: T) -> None:
        if self._disposed:
            # Disposal is terminal: a write to a torn-down source is inert
            # rather than an error, so teardown ordering never matters to a
            # writer that outlives the cell.
            return
        _value = self._value
        self._value = value
        if self._value != _value:
            # Coalesce-aware: inside a `batch`, defer the touch to the outermost
            # boundary so multiple writes produce one invalidation wave.
            _notify_change(self)

    def get(self) -> T:
        """Alias for the value property"""
        return self.value

    def set(self, value: T) -> None:
        """Alias for value= property setter"""
        self.value = value

    @property
    def disposed(self) -> bool:
        """Whether :meth:`dispose` has been called (terminal)."""
        return self._disposed

    def dependent_count(self) -> int:
        """How many nodes currently read this cell — reverse edge degree."""
        pare = self._parents
        return 0 if pare is None else len(pare)

    def dependency_count(self) -> int:
        """Always ``0``. A cell is a pure source and reads nothing.

        Present so degree introspection is uniform across node kinds — the
        Python spelling of ``lazily-rs``'s sealed ``GraphNode`` trait.
        """
        return 0

    def dispose(self) -> None:
        """Tear down this source: detach its dependents and dirty them.

        Terminal and idempotent. Unlike :meth:`Slot.dispose` this takes no
        context — a cell already owns the one it was created against.

        A cell has no dependencies, so only the downstream direction needs
        detaching; the dirtying is what stops a surviving reader from serving a
        cached value derived from a source that no longer exists.
        """
        if self._disposed:
            return
        self._disposed = True
        pare = self._parents
        self._parents = None
        if pare:
            _dirty_disposed_dependents(pare, self.ctx)

    def touch(self) -> None:
        if self._disposed:
            return
        # The auto-discovered parents are the only fan-out: they are reactive
        # edges, so rebind-then-clear (they re-establish on recompute) and push
        # them into the coalesced invalidation wave — no tuple alloc.
        pare = self._parents
        if pare:
            self._parents = None
            ctx = self.ctx
            for parent in pare:
                _reset_work.append((parent, ctx))
            _drain_resets()


def none_callable(_: dict) -> None:
    return None


def _none_as_t(_: dict) -> Any:
    return None


def _apply(fn: Any, ctx: Any) -> Any:
    """Call ``fn(ctx)`` with no static arg coercion.

    The wrapped source/derived callable runs under a per-recompute ``Compute``
    view (not a raw dict). Routing the call through an ``Any``-typed indirection
    stops the compiled caller from coercing the view to ``dict`` (which mypyc
    would reject at runtime), while the callable still receives the view so its
    reads are value-threaded."""
    return fn(ctx)


@mypyc_attr(allow_interpreted_subclasses=True)
class SourceSlot[C_in, C_ctx: dict, T](BaseSlot[C_in, C_ctx, Source[T]]):
    __slots__ = ()

    def __init__(
        self,
        callable: Callable[[C_ctx], T] = _none_as_t,
        resolve_ctx: Callable[[C_in], C_ctx] | None = None,
    ) -> None:
        # The wrapped ``callable`` runs under a per-recompute ``Compute`` view;
        # route the call through :func:`_apply` so the compiled call does not
        # coerce the view to ``dict`` (which mypyc would reject at runtime).
        super().__init__(
            callable=lambda ctx: Source(ctx, _apply(callable, ctx)),
            resolve_ctx=resolve_ctx,
        )


# -- Cell kernel vocabulary (#lzcellkernel) ---------------------------------- #
# v2: ``Source`` / ``SourceSlot`` are the native mypyc classes. Keep the old
# Python spellings as identity aliases so values created through either import
# remain usable across the migration; the intentionally breaking surface is the
# native class name/qualified identity, which is now canonical.
Cell = Source
CellSlot = SourceSlot


def source[C_ctx: dict, T](
    callable: Callable[[C_ctx], T] = _none_as_t,
) -> SourceSlot[C_ctx, C_ctx, T]:
    """Create a slot that returns a :data:`Source` cell (default ``KeepLatest``).

    The canonical Cell-kernel source-cell constructor; for a non-``KeepLatest``
    fold use :func:`~lazily.merge.merge_cell`, which builds a
    :class:`~lazily.merge.MergeCell` (a ``Source`` with policy ``M``).

    Note: this is intentionally a function (not a class) so type checkers
    correctly treat ``@source`` as transforming the function type from ``T`` to
    ``Source[T]``.
    """
    return SourceSlot(callable=callable)


def source_def[C_in, C_ctx: dict, T](
    resolve_ctx: Callable[[C_in], C_ctx],
) -> Callable[[Callable[[C_ctx], T]], SourceSlot[C_in, C_ctx, T]]:
    """Decorator factory: a context-cached :data:`Source` cell, custom resolver."""

    def outer(callable: Callable[[C_ctx], T]) -> SourceSlot[C_in, C_ctx, T]:
        return SourceSlot[C_in, C_ctx, T](
            callable=callable,
            resolve_ctx=resolve_ctx,
        )

    return outer


def cell[C_ctx: dict, T](
    callable: Callable[[C_ctx], T] = _none_as_t,
) -> SourceSlot[C_ctx, C_ctx, T]:
    """Deprecated v1 alias for :func:`source`.

    ``cell`` was the v1 name for the source-cell constructor; use :func:`source`.
    """
    warnings.warn(
        "cell() is deprecated; use source() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return SourceSlot(callable=callable)
