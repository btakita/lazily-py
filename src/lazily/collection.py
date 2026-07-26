"""Keyed reactive collections: the generic ``ReactiveMap`` and its ``SourceMap`` /
``ComputedMap`` specializations (``#reactivemap``).

The Python counterpart of ``lazily-rs/src/cell_family.rs``, the Lean
``LazilyFormal.Collection`` / ``Materialization`` formal models in
``lazily-formal``, and ``lazily-spec/cell-model.md`` § "Keyed cell collections".

There is **one** keyed primitive, generic over the entry's **handle kind**
(the axis Rust expresses as ``ReactiveMap<K, V, H>`` over the ``MapHandle``
trait); the two specializations a binding exposes are the concrete types:

- **:class:`SourceMap` = ``ReactiveMap`` over the cell handle** — **input-cell**
  entries. Adds cell-only :meth:`SourceMap.set` (an input is settable) and eager
  value-minting (:meth:`SourceMap.entry` / :meth:`SourceMap.entry_with`).
- **:class:`ComputedMap` = ``ReactiveMap`` over the slot handle** — **derived-slot**
  entries. :meth:`ReactiveMap.get_or_insert_with` mints a slot on first access
  (**lazy materialization**); :meth:`ComputedMap.materialize_all` pre-mints the
  keyset (**eager**). A slot's value is derived, so ``ComputedMap`` has **no
  ``set``**. There is **no eager/lazy mode flag** — eager is a pre-mint loop,
  lazy is mint-on-access.

The shared surface — ``get_or_insert_with`` / ``remove`` / ``move_*`` /
membership / order / ``keys`` / ``len`` / ``contains`` — lives on the generic
:class:`ReactiveMap`. ``set`` and eager value-minting are the ``SourceMap``-only
specialization; the pre-mint eager helper is the ``ComputedMap``-only specialization.

Three independent reactive signals are exposed, mirroring ``lazily-rs``'s
``membership`` and ``order_signal`` cells:

- **per-entry value** — one reactive node per key (read via :meth:`ReactiveMap.get`);
- **set-membership** — :meth:`ReactiveMap.membership_signal` (bumped on add/remove
  only; ``len``/``contains`` readers subscribe here);
- **order** — :meth:`ReactiveMap.order_signal` (bumped on add/remove *and* on move;
  ``keys`` readers subscribe here).

The independence laws fixed by the formal model are observable here: a pure
reorder (:meth:`ReactiveMap.move_to`) bumps the order signal only — membership and
every entry's value node are untouched, so a ``len``/``contains`` reader is not
invalidated. An atomic move keeps each entry's handle identity (not remove +
re-mint).
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from enum import Enum, EnumMeta
from typing import TYPE_CHECKING, Any, TypeVar

from .cell import Cell
from .slot import Slot


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


__all__ = [
    "CellMap",
    "ComputedMap",
    "EntryKind",
    "MapHandle",
    "ReactiveMap",
    "SlotMap",
    "SourceMap",
]

K = TypeVar("K")
V = TypeVar("V")

#: A map entry's reactive handle: an input :class:`Cell` or a derived
#: storage-sense :class:`~lazily.slot.Slot`.
type MapHandle = Cell | Slot


#: Deprecated :class:`EntryKind` member name -> its current name. The v2 kernel
#: renamed the node kinds to ``Source`` and ``Computed``; the member names follow
#: (the member **values** do not — see :class:`EntryKind`).
_DEPRECATED_ENTRY_KINDS = {
    "CELL": "SOURCE",
    "SLOT": "COMPUTED",
}


class _EntryKindMeta(EnumMeta):
    """Serves the deprecated ``EntryKind`` member names, warning once per access
    site — the enum-member counterpart of the package root's ``__getattr__``
    deprecation shim. Only reached when normal lookup fails, so a current member
    never pays for it and the deprecated names stay out of ``__members__`` and
    out of iteration."""

    def __getattr__(cls, name: str) -> Any:
        current = _DEPRECATED_ENTRY_KINDS.get(name)
        if current is None:
            raise AttributeError(
                f"{cls.__name__!r} enum has no member {name!r}"
            ) from None
        warnings.warn(
            f"EntryKind.{name} is deprecated; use EntryKind.{current} instead. "
            "The v2 kernel renamed the node kinds to Source and Computed. The "
            "member VALUE is unchanged — this is a rename of the name only.",
            DeprecationWarning,
            stacklevel=2,
        )
        return cls[current]


class EntryKind(Enum, metaclass=_EntryKindMeta):
    """Which kind of reactive node a :class:`ReactiveMap` entry is — the
    handle-kind axis the map abstracts over.

    Mirrors ``EntryKind`` in ``lazily-rs`` and ``lazily-formal``.

    The member **values** (``"cell"`` / ``"slot"``) are wire data: nine
    independent binding runners read them out of the shared conformance
    fixtures, so the v2 ``Source`` / ``Computed`` rename moved the member NAMES
    only. The pre-v2 member names remain resolvable as deprecated aliases of
    :attr:`SOURCE` / :attr:`COMPUTED` (see :data:`_DEPRECATED_ENTRY_KINDS`).
    """

    #: An **input** cell (:class:`Cell`) — always materialized on read.
    #: Deprecated alias: the pre-v2 ``CELL`` spelling.
    SOURCE = "cell"
    #: A **derived** slot (:class:`slot`) — materialized eagerly (pre-mint) or
    #: lazily on first read. Deprecated alias: the pre-v2 ``SLOT`` spelling.
    COMPUTED = "slot"


# ---------------------------------------------------------------------------
# Sealed handle-kind seam (source | computed only — bindings add no kinds)
# ---------------------------------------------------------------------------


class _HandleKind(ABC):
    """The entry-handle axis a :class:`ReactiveMap` abstracts over. Sealed: only
    :class:`_SourceHandleKind` (input cells) and :class:`_ComputedHandleKind`
    (derived slots) — the two node kinds of the cell model — implement it, so a
    binding does not add new kinds (mirrors Rust's sealed ``MapHandle`` trait)."""

    KIND: EntryKind

    @abstractmethod
    def materialize(self, ctx: dict, compute: Callable[[Any], V]) -> MapHandle:
        """Allocate the node for one entry in ``ctx``, with ``compute(view)``
        producing its canonical value from a per-recompute compute ``view`` (the
        value-threaded tracking surface, ``#lzcellkernel``). An input cell seeds
        its value once (untracked); a derived slot wraps ``compute`` as its
        recomputation so the factory's reads value-thread through the slot's
        own view."""

    @abstractmethod
    def observe(self, ctx: Any, handle: MapHandle) -> V:
        """Read the entry's value through ``ctx`` — the caller's compute view
        (subscribes the running Slot/Effect by value-threading) or a bare dict /
        ``None`` for an untracked top-level read."""


def _reads(ctx: Any) -> Any:
    """The value-threaded read surface for ``ctx``: the compute view itself when
    it exposes ``read``, else an untracked :class:`~lazily.compute.Context` over
    the dict (``#lzcellkernel`` bare-read removal)."""
    if getattr(ctx, "read", None) is not None:
        return ctx
    from .compute import Context

    return Context(ctx)


class _SourceHandleKind(_HandleKind):
    KIND = EntryKind.SOURCE

    def materialize(self, ctx: dict, compute: Callable[[Any], V]) -> MapHandle:
        # An input has no derivation: seed its value once, untracked (an input
        # cell does not subscribe to whatever its seed factory read).
        return Cell(ctx, compute(_reads(ctx)))

    def observe(self, ctx: Any, handle: MapHandle) -> V:
        return _reads(ctx).read(handle)


class _ComputedHandleKind(_HandleKind):
    KIND = EntryKind.COMPUTED

    def materialize(self, ctx: dict, compute: Callable[[Any], V]) -> MapHandle:
        # A derived node: the same node an eager pre-mint would allocate. Its body
        # receives the slot's own compute view, threaded into ``compute`` so the
        # factory's dependency reads attribute to this member.
        return Slot(lambda _ctx: compute(_ctx))

    def observe(self, ctx: Any, handle: MapHandle) -> V:
        return handle(ctx)  # type: ignore[operator]


_SOURCE_HANDLE = _SourceHandleKind()
_COMPUTED_HANDLE = _ComputedHandleKind()


class ReactiveMap[K, V]:
    """A keyed reactive collection generic over the entry handle kind: a hash map
    of ``K -> handle`` with reactive membership and independently-tracked
    per-entry nodes (``#reactivemap``).

    Membership is reactive: reading :meth:`len` / ``len(map)`` or
    :meth:`contains_key` / ``key in map`` inside a Computed/Effect subscribes to the
    membership signal; reading :meth:`keys` subscribes to the order signal;
    reading one entry's value subscribes to that entry's node alone. Editing one
    entry's value invalidates only that entry's readers — never a sibling or a
    membership/order reader.

    Operations run against the owning ``ctx`` dict, like the rest of ``lazily``.
    The two specializations a binding exposes are :class:`SourceMap` (input cells)
    and :class:`ComputedMap` (derived slots). See the module docs.

    Mirrors ``lazily-rs/src/cell_family.rs``'s ``ReactiveMap<K, V, H>``.
    """

    #: The entry handle kind — set by the :class:`SourceMap` / :class:`ComputedMap`
    #: specialization. The generic base defaults to the cell handle.
    _HANDLE: _HandleKind = _SOURCE_HANDLE

    __slots__ = (
        "_entries",
        "_membership_signal",
        "_order",
        "_order_signal",
        "_order_version",
        "_version",
        "ctx",
    )

    def __init__(self, ctx: dict) -> None:
        self.ctx = ctx
        self._entries: dict[K, MapHandle] = {}
        self._order: list[K] = []
        # Reactive membership (add/remove) and order (add/remove + move) signals.
        self._membership_signal: Cell[int] = Cell(ctx, 0)
        self._order_signal: Cell[int] = Cell(ctx, 0)
        # Untracked mirrors so a mutator bumps the reactive cell without reading
        # its `.value` (which would register a spurious dependency when a mint
        # happens inside a running computation).
        self._version = 0
        self._order_version = 0

    # -- signals -------------------------------------------------------- #

    @property
    def membership_signal(self) -> Cell[int]:
        """The set-membership version signal (bumps on add/remove only)."""
        return self._membership_signal

    @property
    def order_signal(self) -> Cell[int]:
        """The order version signal (bumps on add/remove *and* move)."""
        return self._order_signal

    @property
    def order(self) -> list[K]:
        """The authoritative insertion-ordered key list (non-reactive)."""
        return list(self._order)

    @property
    def entry_kind(self) -> EntryKind:
        """This map's entry kind (:attr:`EntryKind.SOURCE` for a :class:`SourceMap`,
        :attr:`EntryKind.COMPUTED` for a :class:`ComputedMap`)."""
        return self._HANDLE.KIND

    # -- internals ------------------------------------------------------ #

    def _bump_order(self) -> None:
        # A pure move bumps only this; add/remove bump it via _bump_membership.
        self._order_version += 1
        self._order_signal.set(self._order_version)

    def _bump_membership(self) -> None:
        # Invalidate len/contains readers; the key set changed so order did too.
        self._version += 1
        self._membership_signal.set(self._version)
        self._bump_order()

    def _mint_with(self, key: K, compute: Callable[[Any], V]) -> MapHandle:
        """Mint the entry node for ``key`` on first access, caching the handle
        and bumping reactive membership. Re-minting an existing key returns the
        cached handle. ``compute(view)`` takes the member's compute view."""
        handle = self._entries.get(key)
        if handle is not None:
            return handle  # warm: already allocated.
        handle = self._HANDLE.materialize(self.ctx, compute)
        self._entries[key] = handle
        self._order.append(key)
        self._bump_membership()
        return handle

    # -- reads / mint-on-access ----------------------------------------- #

    def get_or_insert_with(
        self, key: K, factory: Callable[[Any, K], V], ctx: Any = None
    ) -> V:
        """Get the value at ``key``, minting the entry via ``factory(view, key)``
        first if absent — the mint-on-access recipe. For a :class:`ComputedMap` this
        is the **lazy materialization** pull; for a :class:`SourceMap` it seeds an
        input cell. Bumps reactive membership only on insert; an existing key
        returns its current value without re-running the factory.

        ``factory`` receives the member's compute ``view`` first, so a factory
        that reads a reactive node value-threads (``lambda c, k: c.read(src)``).
        Pass the caller's compute ``ctx`` to value-thread the *read* of the entry
        too; omit it for an untracked top-level read (``#lzcellkernel``)."""
        read_ctx = self.ctx if ctx is None else ctx
        handle = self._entries.get(key)
        if handle is not None:
            return self._HANDLE.observe(read_ctx, handle)
        handle = self._mint_with(key, lambda view: factory(view, key))
        return self._HANDLE.observe(read_ctx, handle)

    def handle(self, key: K) -> MapHandle | None:
        """Return the existing entry handle for ``key``, or ``None``.
        Non-reactive: does not subscribe the caller to membership."""
        return self._entries.get(key)

    #: Alias for :meth:`handle` (the entry's value node).
    value_cell = handle

    def get(self, key: K, ctx: Any = None) -> V | None:
        """Read the value at ``key`` if present, else ``None``. Reactive on that
        entry only (a reader is invalidated when this entry changes, not when a
        sibling changes).

        Pass the caller's compute ``ctx`` to value-thread the dependency edge
        inside a reactive body; omit it for an untracked top-level read
        (``#lzcellkernel`` bare-read removal)."""
        read_ctx = self.ctx if ctx is None else ctx
        handle = self._entries.get(key)
        if handle is None:
            return None
        return self._HANDLE.observe(read_ctx, handle)

    def remove(self, key: K) -> bool:
        """Remove ``key``'s entry. Bumps reactive membership. Returns whether the
        key was present. (No-op if ``key`` is not a member.)"""
        if key not in self._entries:
            return False
        del self._entries[key]
        self._order = [k for k in self._order if k != key]
        self._bump_membership()
        return True

    # -- membership / order reads --------------------------------------- #

    def keys(self, ctx: Any = None) -> list[K]:
        """Reactive snapshot of the keys in their current order. Subscribes the
        caller to **order** changes (add/remove **and** move/reorder), not to
        per-entry value changes.

        Pass the caller's :class:`~lazily.compute.Compute` view (``ctx``) to
        value-thread the edge inside a reactive body; omit for an untracked
        snapshot (``#lzcellkernel`` bare-read removal)."""
        if ctx is None:
            _ = self._order_signal.value
        else:
            ctx.read(self._order_signal)
        return list(self._order)

    def present_keys(self) -> list[K]:
        """The currently-materialized (present) keys, in first-materialization
        order. Non-reactive; the present set only grows (deferral, not
        de-allocation)."""
        return list(self._order)

    def present_count(self) -> int:
        """Number of currently-materialized (present) entries. Non-reactive."""
        return len(self._order)

    def is_present(self, key: K) -> bool:
        """Whether ``key`` is currently materialized (present). Non-reactive."""
        return key in self._entries

    def position(self, key: K) -> int | None:
        """Current 0-based position of ``key`` in the order, or ``None`` if
        absent. Non-reactive."""
        try:
            return self._order.index(key)
        except ValueError:
            return None

    def __len__(self) -> int:
        # Touch membership so a len() reader is invalidated on add/remove but NOT
        # on a pure reorder (move_to).
        _ = self._membership_signal.value
        return len(self._order)

    def len(self, ctx: Any = None) -> int:
        """Reactive entry count. Subscribes the caller to membership changes.
        Pass the caller's compute view (``ctx``) to value-thread the edge; omit
        for an untracked snapshot (``#lzcellkernel``)."""
        if ctx is None:
            return len(self)
        ctx.read(self._membership_signal)
        return len(self._order)

    def is_empty(self, ctx: Any = None) -> bool:
        """Reactive emptiness check. Subscribes the caller to membership changes.
        See :meth:`len` on ``ctx``."""
        return self.len(ctx) == 0

    def __contains__(self, key: object) -> bool:
        _ = self._membership_signal.value
        return key in self._entries

    def contains_key(self, key: K, ctx: Any = None) -> bool:
        """Reactive membership test for ``key``. Subscribes the caller to
        membership changes (add/remove of any key), not to value changes. Pass
        the caller's compute view (``ctx``) to value-thread the edge; omit for an
        untracked snapshot (``#lzcellkernel``)."""
        if ctx is None:
            return key in self
        ctx.read(self._membership_signal)
        return key in self._entries

    def len_untracked(self) -> int:
        """Non-reactive count. Does not subscribe the caller to anything."""
        return len(self._order)

    # -- atomic ordered move (#lzcellmove) ------------------------------ #

    def move_to(self, key: K, index: int) -> bool:
        """Atomically move ``key`` to position ``index`` (``#lzcellmove``). The
        entry keeps the **same** handle, dependents, and lineage (not remove +
        re-mint). Bumps **only** the order signal, so ``keys`` readers recompute
        but ``len``/``contains`` readers stay cached. ``index`` is clamped to
        ``[0, len)``. Returns whether ``key`` was present."""
        if key not in self._entries:
            return False
        from_pos = self._order.index(key)
        to = min(index, len(self._order) - 1)
        if from_pos == to:
            return True  # no-op: do not invalidate readers needlessly.
        self._order.pop(from_pos)
        self._order.insert(to, key)
        self._bump_order()
        return True

    def move_before(self, key: K, anchor: K) -> bool:
        """Atomically move ``key`` to just before ``anchor`` (a pure reorder).
        No-op returning ``False`` if either key is absent."""
        if anchor not in self._entries or key not in self._entries:
            return False
        anchor_idx = self._order.index(anchor)
        from_pos = self._order.index(key)
        target = anchor_idx - 1 if from_pos < anchor_idx else anchor_idx
        return self.move_to(key, target)

    def move_after(self, key: K, anchor: K) -> bool:
        """Atomically move ``key`` to just after ``anchor`` (a pure reorder).
        No-op returning ``False`` if either key is absent."""
        if anchor not in self._entries or key not in self._entries:
            return False
        anchor_idx = self._order.index(anchor)
        from_pos = self._order.index(key)
        target = anchor_idx if from_pos <= anchor_idx else anchor_idx + 1
        return self.move_to(key, target)


class SourceMap[K, V](ReactiveMap[K, V]):
    """A keyed **input-cell** collection: every entry is a settable :class:`Cell`.

    The ``SourceMap`` specialization of :class:`ReactiveMap` adds cell-only
    :meth:`set` and eager value-minting (:meth:`entry` / :meth:`entry_with`) on
    top of the shared reactive keyed surface. Mirrors ``lazily-rs``'s
    ``SourceMap<K, V> = ReactiveMap<K, V, CellHandle<V>>``.
    """

    __slots__ = ()

    _HANDLE = _SOURCE_HANDLE

    def entry_with(self, key: K, default: Callable[[], V]) -> Cell[V]:
        """Return the value cell for ``key``, minting it with ``default`` (called
        lazily) on first access. Subsequent calls return the cached handle.
        Adding a new key bumps reactive membership; re-fetching an existing key
        does not. Cell-only: eager value-minting has no derived-slot analog."""
        handle = self._entries.get(key)
        if handle is not None:
            return handle  # type: ignore[return-value]
        # ``default`` is the 0-arg value-mint factory (an input seed needs no
        # compute view); adapt it to the view-taking ``compute`` contract.
        return self._mint_with(key, lambda _view: default())  # type: ignore[return-value]

    def entry(self, key: K, default: V) -> Cell[V]:
        """Return the value cell for ``key``, minting it with ``default`` on first
        access. Convenience wrapper over :meth:`entry_with`."""
        return self.entry_with(key, lambda: default)

    def set(self, key: K, value: V) -> None:
        """Set the value at ``key``, inserting a new entry (and bumping
        membership) if it does not exist yet. Updating an existing entry leaves
        membership untouched and invalidates only that entry's dependents.
        Cell-only: an input is settable; a derived :class:`ComputedMap` slot is not."""
        handle = self._entries.get(key)
        if handle is not None:
            handle.set(value)  # type: ignore[union-attr]
            return
        self.entry_with(key, lambda: value)


class ComputedMap[K, V](ReactiveMap[K, V]):
    """A keyed **derived-slot** collection: every entry is a :class:`slot` whose
    value is derived. :meth:`ReactiveMap.get_or_insert_with` mints a slot on first
    access (lazy materialization); :meth:`materialize_all` pre-mints the keyset
    (eager). A slot's value is derived, so ``ComputedMap`` has **no ``set``**. Mirrors
    ``lazily-rs``'s ``ComputedMap<K, V> = ReactiveMap<K, V, SlotHandle<V>>``.
    """

    __slots__ = ()

    _HANDLE = _COMPUTED_HANDLE

    def materialize_all(
        self, keys: Iterable[K], factory: Callable[[Any, K], V]
    ) -> None:
        """**Eager materialization**: pre-mint a derived slot for every key in
        ``keys`` via ``factory``, up front. Observationally identical to minting
        each key lazily on first read — it only changes *when* the nodes are
        allocated. ``factory(view, key)`` receives the member's compute view, so
        a factory reading a reactive node value-threads (``#lzcellkernel``)."""
        for key in keys:
            self.get_or_insert_with(key, factory)


# ---------------------------------------------------------------------------
# Deprecated aliases (v2 kernel rename)
# ---------------------------------------------------------------------------
# The v2 kernel renamed the node kinds to ``Source`` and ``Computed``; the map
# names still said ``Cell`` / ``Slot``, a vocabulary the kernel no longer uses.
# The old names stay as plain aliases so existing imports keep working — a
# rename is not a removal. Importing them from the ``lazily`` package root emits
# a :class:`DeprecationWarning` (see ``lazily/__init__.py``); the submodule
# aliases below are the un-warned escape hatch.

#: Deprecated alias of :class:`SourceMap`.
CellMap = SourceMap
#: Deprecated alias of :class:`ComputedMap`.
SlotMap = ComputedMap
