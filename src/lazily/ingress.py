"""``IngressCell`` — the single-threaded flavor of the transport-agnostic
reactive ingress family (``#designimplementtransport``).

The admission algebra lives in the flavor-neutral
:class:`~lazily.ingress_core.IngressCore`; this shell adds only the reactivity —
four memoized reader kinds per keyed scope plus three receipt readers and a
derived schedule, minted on *this* context.

**Readiness, authority, and retry are derives, not refresh calls.** That is the
point of the family: nothing here polls a connection to find out whether it is
healthy. :meth:`IngressCell.readiness`, :meth:`IngressCell.authority`, and
:meth:`IngressCell.retry` are reader kinds over scope state, so a consumer that
reads readiness depends on exactly the transitions that can change it — and a
transition that cannot (a buffered out-of-order envelope, a tick inside the
freshness horizon, an empty drain) invalidates nothing. ``IngressCore`` returns
the invalidation set for every transition and this shell clears precisely that
set.

**Why four reader kinds per scope and not one.** Collapsing them would make an
error deepen a backoff *and* re-render a value that did not change. The four
boundaries are distinct in the algebra
(:class:`~lazily.ingress_core.IngressScopeChange`), so they are distinct here.

**No observers.** Like every reactive in this library the ingress exposes no
listener registry: anything that survived an invalidation would not be a graph
edge. A consumer that wants to *act* on an admission writes an
:class:`~lazily.effect.Effect` over a reader handle.

Spec: ``lazily-spec/docs/transport-ingress.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .batch import batch
from .cell import Source
from .ingress_core import (
    IngressAdmission,
    IngressAuthority,
    IngressChange,
    IngressCore,
    IngressEnvelope,
    IngressError,
    IngressPolicy,
    IngressReadiness,
    IngressReceipt,
    IngressReceiptChannel,
    IngressRetry,
    IngressSchedule,
    IngressScopeChange,
    IngressTransportKind,
    ReplayRequest,
    ScopeView,
)
from .merge import KeepLatest
from .slot import Slot


if TYPE_CHECKING:
    from collections.abc import Callable

    from .ingress_core import IngressTransport
    from .merge import MergePolicy


__all__ = ["IngressCell", "IngressScopeReaders"]


@dataclass(frozen=True, slots=True)
class IngressScopeReaders[T]:
    """The four reader kinds one keyed scope exposes.

    Handles rather than values, so a consumer can compose further derives over a
    single reader kind instead of over the whole scope.
    """

    value: Slot[dict, dict, T | None]
    readiness: Slot[dict, dict, IngressReadiness]
    authority: Slot[dict, dict, IngressAuthority | None]
    retry: Slot[dict, dict, IngressRetry | None]


class IngressCell[K, T]:
    """A keyed, lifecycle-scoped reactive ingress.

    One admission plane per key, with readiness, authority, and retry as derives
    rather than calls.
    """

    __slots__ = (
        "_accepted",
        "_core",
        "_dropped",
        "_errors",
        "_poll_interval",
        "_schedule",
        "_scopes",
        "_transport_kind",
        "ctx",
    )

    def __init__(
        self,
        ctx: dict,
        policy: IngressPolicy | None = None,
        merge: MergePolicy[T] | None = None,
        *,
        transport: IngressTransportKind = IngressTransportKind.EVENT_CHANNEL,
        poll_interval: int = 0,
    ) -> None:
        """Build an ingress over ``policy``, folding under ``merge``.

        ``poll_interval`` is retained even for an event channel so a later
        :meth:`set_transport` to ``BOUNDED_POLLING`` has a bound to fall back to
        rather than inventing one.
        """
        self.ctx = ctx
        self._core: IngressCore[K, T] = IngressCore(
            policy if policy is not None else IngressPolicy(),
            merge if merge is not None else KeepLatest,
        )
        self._scopes: dict[K, IngressScopeReaders[T]] = {}
        self._accepted = self._receipt_reader(IngressReceiptChannel.ACCEPTED)
        self._dropped = self._receipt_reader(IngressReceiptChannel.DROPPED)
        self._errors = self._receipt_reader(IngressReceiptChannel.ERROR)
        self._transport_kind: Source[IngressTransportKind] = Source(ctx, transport)
        self._poll_interval: Source[int] = Source(ctx, poll_interval)
        self._schedule: Slot[dict, dict, IngressSchedule] = Slot(
            callable=lambda view: IngressSchedule.for_kind(
                view.read(self._transport_kind), view.read(self._poll_interval)
            )
        )

    # -- Storage boundary ----------------------------------------------------

    def _run[R](self, operation: Callable[[], R]) -> R:
        """Run one core mutation under this flavor's storage boundary.

        The single-threaded flavor has no boundary to take. The seam exists so the
        thread-safe flavor can hold its lock for the *core mutation only* and
        still invalidate with the lock released — a reader's body takes the
        storage boundary, so invalidating while holding it inverts the lock order.
        """
        return operation()

    def _receipt_reader(
        self, channel: IngressReceiptChannel
    ) -> Slot[dict, dict, list[IngressReceipt[K]]]:
        return Slot(
            callable=lambda _view: self._run(lambda: self._core.receipts(channel))
        )

    def _ensure_readers(self, key: K) -> IngressScopeReaders[T]:
        """Mint (or return) one scope's four readers.

        Idempotent, so a consumer may hold a handle for a key that has not opened
        yet — an unknown scope reads ``UNKNOWN``/``None`` rather than raising.
        """
        readers = self._scopes.get(key)
        if readers is not None:
            return readers
        readers = IngressScopeReaders(
            value=Slot(callable=lambda _view: self._run(lambda: self._core.peek(key))),
            readiness=Slot(
                callable=lambda _view: self._run(lambda: self._core.readiness(key))
            ),
            authority=Slot(
                callable=lambda _view: self._run(lambda: self._core.authority(key))
            ),
            retry=Slot(callable=lambda _view: self._run(lambda: self._core.retry(key))),
        )
        self._scopes[key] = readers
        return readers

    def _apply(self, change: IngressChange[K]) -> None:
        """Apply one core-reported invalidation set.

        Every affected reader is cleared in a **single** coalesced frontier walk,
        so no reader observes a partial fan-out — a generation handoff must never
        be visible as "new value, old authority". Called with the storage boundary
        released.
        """
        if change.is_empty():
            return
        roots: list[Slot[dict, dict, Any]] = []
        for key, scope_change in change.scopes:
            self._collect_scope_roots(roots, self._ensure_readers(key), scope_change)
        if change.accepted_receipts:
            roots.append(self._accepted)
        if change.dropped_receipts:
            roots.append(self._dropped)
        if change.error_receipts:
            roots.append(self._errors)
        if not roots:
            return

        def resets() -> None:
            for root in roots:
                root.reset(self.ctx)

        batch(resets)

    @staticmethod
    def _collect_scope_roots(
        roots: list[Slot[dict, dict, Any]],
        readers: IngressScopeReaders[T],
        change: IngressScopeChange,
    ) -> None:
        if change.value:
            roots.append(readers.value)
        if change.readiness:
            roots.append(readers.readiness)
        if change.authority:
            roots.append(readers.authority)
        if change.retry:
            roots.append(readers.retry)

    # -- Mutators ------------------------------------------------------------

    def open(self, key: K, generation: int) -> None:
        """Open (or reopen) a keyed scope at ``generation``."""
        change = self._run(lambda: self._core.open(key, generation))
        self._apply(change)

    def admit(self, envelope: IngressEnvelope[K, T]) -> IngressAdmission:
        """Admit one decoded envelope."""
        change, admission = self._run(lambda: self._core.admit(envelope))
        self._apply(change)
        return admission

    def suspend(self, key: K) -> ReplayRequest | None:
        """Suspend a scope, retaining its watermark.

        Returns the replay request a reconnect will need.
        """
        change, request = self._run(lambda: self._core.suspend(key))
        self._apply(change)
        return request

    def reconnect(self, key: K, generation: int) -> ReplayRequest:
        """Reconnect a scope at ``generation``, clearing its error streak."""
        change, request = self._run(lambda: self._core.reconnect(key, generation))
        self._apply(change)
        return request

    def close(self, key: K) -> None:
        """Close a scope. It admits nothing and claims no authority until reopened."""
        change = self._run(lambda: self._core.close(key))
        self._apply(change)

    def fail(self, key: K, error: IngressError) -> None:
        """Record a transport/decode failure, deepening the scope's backoff."""
        change = self._run(lambda: self._core.fail(key, error))
        self._apply(change)

    def tick(self, now: int) -> None:
        """Advance logical time.

        Only scopes that crossed the freshness horizon are invalidated.
        """
        change = self._run(lambda: self._core.tick(now))
        self._apply(change)

    def drain(self, key: K) -> T | None:
        """Drain a scope's coalesced window. An egress, never an ack."""
        change, value = self._run(lambda: self._core.drain(key))
        self._apply(change)
        return value

    def pump(self, transport: IngressTransport[K, T]) -> list[IngressAdmission]:
        """Admit everything ``transport`` has decoded, then ask it to replay any
        gap still open. Returns the admission outcomes in arrival order.

        The only method that touches a transport, and it makes no decision of its
        own: the gap it replays is the one the algebra reports.
        """
        outcomes: list[IngressAdmission] = []
        touched: list[K] = []
        for envelope in transport.drain():
            key = envelope.key
            outcomes.append(self.admit(envelope))
            if key not in touched:
                touched.append(key)
        for key in touched:
            view = self._run(lambda key=key: self._core.view(key))
            if view is not None and view.has_gap():
                transport.request_replay(
                    key, ReplayRequest(view.generation, view.resume_from())
                )
        return outcomes

    # -- Reactive reads ------------------------------------------------------

    def value(self, key: K, ctx: Any = None) -> T | None:
        """Reactive read: the coalesced window awaiting drain."""
        return self._ensure_readers(key).value(self.ctx if ctx is None else ctx)

    def readiness(self, key: K, ctx: Any = None) -> IngressReadiness:
        """Reactive read: derived readiness."""
        return self._ensure_readers(key).readiness(self.ctx if ctx is None else ctx)

    def authority(self, key: K, ctx: Any = None) -> IngressAuthority | None:
        """Reactive read: derived authority."""
        return self._ensure_readers(key).authority(self.ctx if ctx is None else ctx)

    def retry(self, key: K, ctx: Any = None) -> IngressRetry | None:
        """Reactive read: derived retry decision."""
        return self._ensure_readers(key).retry(self.ctx if ctx is None else ctx)

    def accepted(self, ctx: Any = None) -> list[IngressReceipt[K]]:
        """Reactive read: accepted receipts, oldest first."""
        return self._accepted(self.ctx if ctx is None else ctx)

    def dropped(self, ctx: Any = None) -> list[IngressReceipt[K]]:
        """Reactive read: dropped receipts, oldest first."""
        return self._dropped(self.ctx if ctx is None else ctx)

    def errors(self, ctx: Any = None) -> list[IngressReceipt[K]]:
        """Reactive read: error receipts, oldest first."""
        return self._errors(self.ctx if ctx is None else ctx)

    def schedule(self, ctx: Any = None) -> IngressSchedule:
        """Reactive read: the derived delivery schedule."""
        return self._schedule(self.ctx if ctx is None else ctx)

    # -- Reader handles ------------------------------------------------------

    def reader_handles(self, key: K) -> IngressScopeReaders[T]:
        """The four reader handles for one scope, for composing further derives."""
        return self._ensure_readers(key)

    def value_handle(self, key: K) -> Slot[dict, dict, T | None]:
        """Handle for the scope's value reader."""
        return self._ensure_readers(key).value

    def readiness_handle(self, key: K) -> Slot[dict, dict, IngressReadiness]:
        """Handle for the scope's readiness reader."""
        return self._ensure_readers(key).readiness

    def authority_handle(self, key: K) -> Slot[dict, dict, IngressAuthority | None]:
        """Handle for the scope's authority reader."""
        return self._ensure_readers(key).authority

    def retry_handle(self, key: K) -> Slot[dict, dict, IngressRetry | None]:
        """Handle for the scope's retry reader."""
        return self._ensure_readers(key).retry

    def accepted_handle(self) -> Slot[dict, dict, list[IngressReceipt[K]]]:
        """Handle for the accepted-receipt reader."""
        return self._accepted

    def dropped_handle(self) -> Slot[dict, dict, list[IngressReceipt[K]]]:
        """Handle for the dropped-receipt reader."""
        return self._dropped

    def errors_handle(self) -> Slot[dict, dict, list[IngressReceipt[K]]]:
        """Handle for the error-receipt reader."""
        return self._errors

    def schedule_handle(self) -> Slot[dict, dict, IngressSchedule]:
        """Handle for the schedule reader."""
        return self._schedule

    # -- Cache-validity probes ----------------------------------------------
    #
    # ``invalidates`` is a claim about the *graph*, and only the shell can answer
    # it. These report whether a reader's memoized value is still cached, which is
    # what makes a fixture's ``invalidates: false`` falsifiable: an
    # over-invalidating shell shows up as a cold cache where the corpus says warm.

    def value_is_valid(self, key: K) -> bool:
        """Whether the scope's value reader is still cached (not invalidated)."""
        return self._ensure_readers(key).value.is_in(self.ctx)

    def readiness_is_valid(self, key: K) -> bool:
        """Whether the scope's readiness reader is still cached."""
        return self._ensure_readers(key).readiness.is_in(self.ctx)

    def authority_is_valid(self, key: K) -> bool:
        """Whether the scope's authority reader is still cached."""
        return self._ensure_readers(key).authority.is_in(self.ctx)

    def retry_is_valid(self, key: K) -> bool:
        """Whether the scope's retry reader is still cached."""
        return self._ensure_readers(key).retry.is_in(self.ctx)

    def accepted_is_valid(self) -> bool:
        """Whether the accepted-receipt reader is still cached."""
        return self._accepted.is_in(self.ctx)

    def dropped_is_valid(self) -> bool:
        """Whether the dropped-receipt reader is still cached."""
        return self._dropped.is_in(self.ctx)

    def errors_is_valid(self) -> bool:
        """Whether the error-receipt reader is still cached."""
        return self._errors.is_in(self.ctx)

    # -- Live retuning -------------------------------------------------------

    def set_transport(self, kind: IngressTransportKind) -> None:
        """Retune the transport live.

        Falling back from an event channel to bounded polling is a source-cell
        write, so every schedule dependent reacts.
        """
        self._transport_kind.set(kind)

    def set_poll_interval(self, interval: int) -> None:
        """Retune the poll bound live."""
        self._poll_interval.set(interval)

    # -- Non-reactive projections -------------------------------------------

    def view(self, key: K) -> ScopeView | None:
        """Non-reactive projection of a scope, for assertions and diagnostics."""
        return self._run(lambda: self._core.view(key))

    @property
    def policy(self) -> IngressPolicy:
        """The bounds in force."""
        return self._core.policy

    @property
    def merge_policy(self) -> MergePolicy[T]:
        """The merge algebra the hot window folds under."""
        return self._core.merge_policy

    def scope_keys(self) -> list[K]:
        """Every known scope key."""
        return self._run(self._core.scope_keys)
