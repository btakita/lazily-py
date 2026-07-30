"""Ingress-family shells for the thread-safe and async execution flavors.

``#designimplementtransport``. The admission algebra is already independent of the
reactive graph (:class:`~lazily.ingress_core.IngressCore`), and
:class:`~lazily.ingress.IngressCell` already separates *mutating the core* from
*clearing the readers* through the ``_run`` storage-boundary seam. A flavor is
therefore exactly two decisions: what that boundary is, and which graph the
readers are minted on. Everything else — the admission order, the four reader
kinds, the three receipt channels — is shared verbatim, because the family's
claim is that all three flavors obey ONE contract.

**Lock discipline (thread-safe).** ``ThreadSafeIngressCell`` holds the
:class:`~lazily.thread_safe.ThreadSafeContext` lock for the **core mutation
only** and runs invalidation with the lock released. A reader's body takes the
same boundary, so an op that invalidated while still holding it would invert the
lock order against a concurrent reader. Multi-root invalidation then goes through
one :func:`~lazily.batch.batch` boundary, so one admission is **one** frontier
walk: clearing value, readiness, authority, retry, and a receipt channel one at a
time is one walk each, and a concurrent reader can interleave and observe the new
value with the old authority — precisely the partial fan-out a generation handoff
must never expose.

**Nothing here is async-coloured.** Whether an envelope is admissible is a
function of the fence, the watermark, the reorder buffer, and the observed clock —
state the graph does not own and nothing has to await. ``AsyncIngressCell``
therefore has the same synchronous surface as the other two, matching the settled
Rust reference and the existing ``AsyncQueueCell`` / ``AsyncReactiveMap``
precedent. Awaiting belongs to the transport, and the transport is outside the
primitive by construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .ingress import IngressCell
from .ingress_core import IngressTransportKind
from .thread_safe import ThreadSafeContext


if TYPE_CHECKING:
    from collections.abc import Callable

    from .ingress_core import IngressPolicy
    from .merge import MergePolicy


__all__ = ["AsyncIngressCell", "ThreadSafeIngressCell"]


class ThreadSafeIngressCell[K, T](IngressCell[K, T]):
    """A ``Send + Sync``-equivalent keyed ingress serialized by a ``ThreadSafeContext``.

    Core mutations and reader bodies are serialized by the context's re-entrant
    lock; invalidation runs outside it and fans out through one ``batch``.
    """

    __slots__ = ("_thread_safe",)

    def __init__(
        self,
        ctx: dict,
        policy: IngressPolicy | None = None,
        merge: MergePolicy[T] | None = None,
        *,
        transport: IngressTransportKind = IngressTransportKind.EVENT_CHANNEL,
        poll_interval: int = 0,
        ts: ThreadSafeContext | None = None,
    ) -> None:
        self._thread_safe = ts if ts is not None else ThreadSafeContext()
        super().__init__(
            ctx,
            policy,
            merge,
            transport=transport,
            poll_interval=poll_interval,
        )

    @property
    def thread_safe_context(self) -> ThreadSafeContext:
        """The context whose lock serializes this ingress."""
        return self._thread_safe

    def _run[R](self, operation: Callable[[], R]) -> R:
        # The storage boundary, and nothing more: ``_apply`` is deliberately NOT
        # called from inside here (see the module note on lock order).
        with self._thread_safe.lock:
            return operation()


class AsyncIngressCell[K, T](IngressCell[K, T]):
    """The async-graph keyed ingress; admission stays synchronous.

    A distinct flavor rather than an alias: it is the shell an async owner holds,
    and the conformance corpus replays against it as its own row. What it
    deliberately does *not* add is a ``settle`` step — there is nothing to await.
    """

    __slots__ = ()
