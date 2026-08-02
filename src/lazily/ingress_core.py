"""The graph-agnostic admission algebra behind every ingress flavor.

``#designimplementtransport``. Same core/shell split :mod:`lazily.queue` makes
for the broadcast family and ``KeyedOrder`` makes for the map family, and for the
same reason: deciding whether an inbound envelope is *admissible* touches no
reader handle and awaits nothing, so the single-threaded, thread-safe, and async
shells share it verbatim — while **reactivity deliberately stays out**.
Invalidation is a graph write, so each flavor mints its own per-scope reader
handles on its own context and clears them itself.

Every mutator therefore returns an :class:`IngressChange` — *which* reader kinds
the transition dirtied — rather than performing the invalidation. That return
value is the whole contract between the core and a shell, and it is a pure
function of the transition, which is what makes the plane portable across flavors
without re-deriving values per flavor.

**Transport-agnostic by construction.** The core never touches a transport. An
envelope is a value (:class:`IngressEnvelope`) carrying its own provenance —
``generation``, ``sequence``, ``stamped_at`` — so a WebSocket frame, an RPC
response, and a polled page are the *same* input once decoded. That is what makes
the admission decisions (stale rejection, dedupe, reorder, freshness,
backpressure) independent of how bytes arrived, and it is why
:class:`IngressTransportKind` exists only to derive a *schedule*.

**What is a derive and what is a call.** Readiness, authority, and retry are not
imperative refresh calls; they are pure functions of scope state
(:meth:`ScopeView.readiness`, :meth:`ScopeView.authority`,
:meth:`ScopeView.retry`) that each shell exposes as a reader. Freshness is
time-dependent, so it enters through an explicit :meth:`IngressCore.tick` rather
than a hidden clock read — the same discipline the temporal sources use, and the
reason staleness transitions are deterministic and fixture-replayable.

Spec: ``lazily-spec/docs/transport-ingress.md``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from .relay import Overflow


if TYPE_CHECKING:
    from collections.abc import Iterator

    from .merge import MergePolicy


__all__ = [
    "InProcIngress",
    "IngressAdmission",
    "IngressAdmissionKind",
    "IngressAuthority",
    "IngressChange",
    "IngressConfigError",
    "IngressCore",
    "IngressDropReason",
    "IngressEnvelope",
    "IngressError",
    "IngressLifecycle",
    "IngressPolicy",
    "IngressReadiness",
    "IngressReceipt",
    "IngressReceiptChannel",
    "IngressReceiptOutcome",
    "IngressRetry",
    "IngressSchedule",
    "IngressScopeChange",
    "IngressTransport",
    "IngressTransportKind",
    "ReplayRequest",
    "ScopeView",
]


# -- Transport provenance ----------------------------------------------------


class IngressTransportKind(StrEnum):
    """How envelopes reach a scope.

    Event delivery is the default and needs no schedule; the other two exist so a
    deployment without an event channel still has a *bounded* fallback rather
    than an unbounded refresh loop.
    """

    #: Server-initiated delivery (WebSocket, SSE, in-proc channel). Preferred.
    EVENT_CHANNEL = "event_channel"
    #: Client-initiated, but triggered by an out-of-band event rather than a
    #: timer — an RPC issued *because* something happened.
    RPC_TRIGGERED = "rpc_triggered"
    #: Client-initiated on a bounded interval. The fallback of last resort.
    BOUNDED_POLLING = "bounded_polling"


@dataclass(frozen=True, slots=True)
class IngressSchedule:
    """When, if ever, a scope should ask the transport for more data.

    ``poll_interval`` is not ``None`` only for
    :attr:`IngressTransportKind.BOUNDED_POLLING` — making "we polled a transport
    that pushes" unrepresentable rather than merely discouraged.
    """

    kind: IngressTransportKind
    poll_interval: int | None = None

    @classmethod
    def for_kind(
        cls, kind: IngressTransportKind, poll_interval: int
    ) -> IngressSchedule:
        """Derive the schedule for ``kind``.

        A poll interval is offered only where event delivery is unavailable, and
        never zero — a zero interval is an unbounded refresh loop.
        """
        if kind == IngressTransportKind.BOUNDED_POLLING:
            return cls(kind, max(1, poll_interval))
        return cls(kind, None)


@dataclass(frozen=True, slots=True)
class IngressEnvelope[K, T]:
    """One decoded inbound message, with the provenance admission needs.

    ``generation`` fences a producer incarnation (a reconnect, a redeploy, a build
    skew); ``sequence`` orders within a generation; ``stamped_at`` is the
    producer's logical time, which is what freshness is measured against.
    """

    key: K
    generation: int
    sequence: int
    stamped_at: int
    payload: T


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    """What a reconnect needs from the transport to close its gap."""

    generation: int
    from_sequence: int


class IngressTransport[K, T]:
    """A decoded source of envelopes.

    The core never calls this — a shell's ``pump`` does — which is exactly what
    keeps admission independent of delivery. Implementations decode; they do not
    decide.
    """

    def kind(self) -> IngressTransportKind:
        """How this transport delivers. Drives :class:`IngressSchedule` only."""
        raise NotImplementedError

    def drain(self) -> list[IngressEnvelope[K, T]]:
        """Take everything decoded since the last call. Never blocks."""
        raise NotImplementedError

    def request_replay(self, key: K, request: ReplayRequest) -> bool:
        """Ask the producer to resend from ``request.from_sequence``.

        Returns whether the transport could carry the request — a polling
        transport that cannot address history answers ``False``, which is what
        makes "this gap will never close" observable rather than silent.
        """
        raise NotImplementedError


class InProcIngress[K, T](IngressTransport[K, T]):
    """An in-process event channel: the reference :class:`IngressTransport`.

    ``kind`` is configurable so one implementation exercises all three delivery
    modes — including the ``BOUNDED_POLLING`` case that cannot serve a replay.
    """

    __slots__ = ("_inbound", "_kind", "_replays")

    def __init__(self, kind: IngressTransportKind) -> None:
        self._kind = kind
        self._inbound: deque[IngressEnvelope[K, T]] = deque()
        self._replays: list[tuple[K, ReplayRequest]] = []

    def kind(self) -> IngressTransportKind:
        return self._kind

    def push(self, envelope: IngressEnvelope[K, T]) -> None:
        """Queue one envelope for the next :meth:`drain`."""
        self._inbound.append(envelope)

    def drain(self) -> list[IngressEnvelope[K, T]]:
        batch = list(self._inbound)
        self._inbound.clear()
        return batch

    def request_replay(self, key: K, request: ReplayRequest) -> bool:
        # A bounded poll has no addressable history: it can only wait for the
        # next page, so it cannot honour a replay.
        if self._kind == IngressTransportKind.BOUNDED_POLLING:
            return False
        self._replays.append((key, request))
        return True

    def replays(self) -> list[tuple[K, ReplayRequest]]:
        """Replay requests observed so far, oldest first."""
        return list(self._replays)


# -- Decisions ---------------------------------------------------------------


class IngressDropReason(StrEnum):
    """Why an envelope was refused.

    Every member is a *decision*, not a failure — dropping a superseded envelope
    is correct behaviour and is receipted as such.
    """

    #: ``generation`` is below the scope's fence: a zombie producer.
    STALE_GENERATION = "stale_generation"
    #: ``sequence`` was already delivered in this generation.
    DUPLICATE_SEQUENCE = "duplicate_sequence"
    #: ``sequence`` is already sitting in the reorder buffer.
    DUPLICATE_BUFFERED = "duplicate_buffered"
    #: The reorder buffer is at ``reorder_window`` and this does not fill the gap.
    REORDER_WINDOW_OVERFLOW = "reorder_window_overflow"
    #: ``now - stamped_at`` exceeds the freshness horizon.
    EXPIRED = "expired"
    #: The hot window is at ``high_water`` under a bounding overflow policy.
    BACKPRESSURE = "backpressure"
    #: The scope is closed; it admits nothing until reopened.
    SCOPE_CLOSED = "scope_closed"


class IngressError(StrEnum):
    """A transport- or decode-level failure attributed to a scope.

    Distinct from a drop: an error means we could not *decide*, so it drives
    retry rather than a receipted refusal.
    """

    #: The transport closed or reset under us.
    TRANSPORT_CLOSED = "transport_closed"
    #: The frame could not be decoded into an envelope.
    DECODE_FAILED = "decode_failed"
    #: The producer reported that our generation is no longer authoritative.
    AUTHORITY_LOST = "authority_lost"


class IngressAdmissionKind(StrEnum):
    """Which admission outcome an :class:`IngressAdmission` carries."""

    ACCEPTED = "accepted"
    CONFLATED = "conflated"
    BUFFERED = "buffered"
    GENERATION_HANDOFF = "generation_handoff"
    DROPPED = "dropped"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class IngressAdmission:
    """The outcome of admitting one envelope.

    A tagged record rather than a bare enum, because four of the six outcomes
    carry data a producer needs: the resulting watermark, the surviving gap, the
    fence handoff, or the refusal reason.
    """

    kind: IngressAdmissionKind
    #: Highest in-order sequence now delivered (``ACCEPTED``/``CONFLATED``/handoff).
    delivered_through: int | None = None
    #: The first sequence still missing (``BUFFERED``).
    gap_from: int | None = None
    #: The fence we held (``GENERATION_HANDOFF``).
    from_generation: int | None = None
    #: The fence we now hold (``GENERATION_HANDOFF``).
    to_generation: int | None = None
    #: The receipted refusal (``DROPPED``).
    reason: IngressDropReason | None = None

    @classmethod
    def accepted(cls, delivered_through: int) -> IngressAdmission:
        """Delivered in order; the window holds exactly this one op."""
        return cls(IngressAdmissionKind.ACCEPTED, delivered_through=delivered_through)

    @classmethod
    def conflated(cls, delivered_through: int) -> IngressAdmission:
        """Delivered in order and coalesced with at least one other op."""
        return cls(IngressAdmissionKind.CONFLATED, delivered_through=delivered_through)

    @classmethod
    def buffered(cls, gap_from: int) -> IngressAdmission:
        """Held pending an earlier sequence. Nothing is visible yet."""
        return cls(IngressAdmissionKind.BUFFERED, gap_from=gap_from)

    @classmethod
    def generation_handoff(
        cls, from_generation: int, to_generation: int, delivered_through: int
    ) -> IngressAdmission:
        """A newer producer incarnation took over; the envelope was delivered."""
        return cls(
            IngressAdmissionKind.GENERATION_HANDOFF,
            delivered_through=delivered_through,
            from_generation=from_generation,
            to_generation=to_generation,
        )

    @classmethod
    def dropped(cls, reason: IngressDropReason) -> IngressAdmission:
        """Refused, with the reason receipted."""
        return cls(IngressAdmissionKind.DROPPED, reason=reason)

    @classmethod
    def blocked(cls) -> IngressAdmission:
        """Refused by :attr:`Overflow.BLOCK`; retry after a drain."""
        return cls(IngressAdmissionKind.BLOCKED)

    def is_delivered(self) -> bool:
        """Whether the envelope became visible to readers."""
        return self.kind in {
            IngressAdmissionKind.ACCEPTED,
            IngressAdmissionKind.CONFLATED,
            IngressAdmissionKind.GENERATION_HANDOFF,
        }


# -- Lifecycle and derives ---------------------------------------------------


class IngressLifecycle(StrEnum):
    """Where a scope is in its lifecycle.

    Scopes are keyed and independent: closing one never touches another.
    """

    #: Opened, nothing delivered yet.
    OPENING = "opening"
    #: Delivering.
    LIVE = "live"
    #: Disconnected but retained: state and cursors survive for replay.
    SUSPENDED = "suspended"
    #: Terminal until reopened. Admits nothing.
    CLOSED = "closed"


class IngressReadiness(StrEnum):
    """The derived answer to "can a consumer trust this scope right now?"."""

    #: No such scope.
    UNKNOWN = "unknown"
    #: Open, nothing delivered yet.
    WARMING = "warming"
    #: Delivered and inside the freshness horizon.
    READY = "ready"
    #: Delivered, but the newest accepted stamp is older than the horizon.
    STALE = "stale"
    #: Disconnected; retained state may be replayed.
    SUSPENDED = "suspended"
    #: Terminal.
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class IngressAuthority:
    """What the scope currently claims authority over.

    The fence plus the in-order watermark a replay request must resume from.
    """

    generation: int
    delivered_through: int | None
    stamped_at: int


@dataclass(frozen=True, slots=True)
class IngressRetry:
    """The derived retry decision for a scope that has errored."""

    attempt: int
    backoff: int
    resume_from: int


class IngressReceiptChannel(StrEnum):
    """Which receipt channel a receipt belongs to.

    The three are separate reader kinds because they have separate consumers: a
    projection wants accepts, a dashboard wants drops, a supervisor wants errors.
    """

    ACCEPTED = "accepted"
    DROPPED = "dropped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class IngressReceiptOutcome:
    """The decision a receipt records, tagged by its channel."""

    channel: IngressReceiptChannel
    #: Highest in-order sequence delivered after this envelope (``ACCEPTED``).
    delivered_through: int | None = None
    #: Whether the payload coalesced into a non-empty window (``ACCEPTED``).
    conflated: bool = False
    #: The refusal (``DROPPED``).
    reason: IngressDropReason | None = None
    #: The failure (``ERROR``).
    error: IngressError | None = None

    # Named ``for_*`` because a bare ``error`` classmethod would be shadowed by
    # the ``error`` field's slot descriptor — a silent
    # ``'member_descriptor' object is not callable`` at the first failure.
    @classmethod
    def for_accepted(
        cls, delivered_through: int, conflated: bool
    ) -> IngressReceiptOutcome:
        return cls(
            IngressReceiptChannel.ACCEPTED,
            delivered_through=delivered_through,
            conflated=conflated,
        )

    @classmethod
    def for_dropped(cls, reason: IngressDropReason) -> IngressReceiptOutcome:
        return cls(IngressReceiptChannel.DROPPED, reason=reason)

    @classmethod
    def for_error(cls, error: IngressError) -> IngressReceiptOutcome:
        return cls(IngressReceiptChannel.ERROR, error=error)


@dataclass(frozen=True, slots=True)
class IngressReceipt[K]:
    """One durable record of an admission decision.

    ``offset`` is monotone and survives eviction, so a consumer can tell "I have
    seen everything" from "the log wrapped".
    """

    offset: int
    key: K
    generation: int
    sequence: int | None
    outcome: IngressReceiptOutcome

    @property
    def channel(self) -> IngressReceiptChannel:
        """The channel this receipt is read from."""
        return self.outcome.channel


# -- Policy ------------------------------------------------------------------


class IngressConfigError(ValueError):
    """A policy was refused at construction time.

    Mirrors :class:`~lazily.relay.RelayConfigError`: the overflow choice is
    validated against the merge algebra, because ``CONFLATE`` bounds nothing for
    a non-conflating ``⊕``.
    """

    #: ``Overflow.CONFLATE`` chosen for a non-conflating merge policy.
    CONFLATE_NOT_BOUNDING = "ConflateNotBounding"
    #: A zero receipt capacity would discard every receipt it just minted.
    ZERO_RECEIPT_CAPACITY = "ZeroReceiptCapacity"


@dataclass(frozen=True, slots=True)
class IngressPolicy:
    """Bounds and taxes, all flavor-neutral."""

    #: How many out-of-order envelopes may be held per scope. ``0`` disables
    #: reordering: a gap drops immediately.
    reorder_window: int = 8
    #: ``now - stamped_at`` above this marks a scope
    #: :attr:`IngressReadiness.STALE`; an *arriving* envelope that old is dropped
    #: as :attr:`IngressDropReason.EXPIRED`.
    freshness_horizon: int = 1_000
    #: Merged-op count at which ``overflow`` engages.
    high_water: int = 64
    #: What to do at ``high_water``.
    overflow: Overflow = Overflow.CONFLATE
    #: Retained receipts, oldest evicted first.
    receipt_capacity: int = 256
    #: First retry backoff; doubles per consecutive error.
    retry_base: int = 10
    #: Backoff clamp.
    retry_ceiling: int = 10_000


# -- The invalidation set ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngressScopeChange:
    """Which of a scope's reader kinds a transition dirtied.

    Four kinds exist because they have four different invalidation boundaries: a
    buffered envelope moves nothing but its own gap, a ``tick`` across the
    horizon moves only readiness, and an error moves only retry.
    """

    value: bool = False
    readiness: bool = False
    authority: bool = False
    retry: bool = False

    def is_empty(self) -> bool:
        """Nothing changed — the shell must not clear a reader."""
        return not (self.value or self.readiness or self.authority or self.retry)

    def union(self, other: IngressScopeChange) -> IngressScopeChange:
        return IngressScopeChange(
            self.value or other.value,
            self.readiness or other.readiness,
            self.authority or other.authority,
            self.retry or other.retry,
        )

    @classmethod
    def all(cls) -> IngressScopeChange:
        return cls(True, True, True, True)

    @classmethod
    def readiness_only(cls) -> IngressScopeChange:
        return cls(readiness=True)

    @classmethod
    def value_only(cls) -> IngressScopeChange:
        return cls(value=True)

    @classmethod
    def retry_only(cls) -> IngressScopeChange:
        return cls(retry=True)

    @classmethod
    def creation(cls) -> IngressScopeChange:
        """What materializing a previously-unknown scope changes.

        An unknown scope reads ``UNKNOWN``/``None``, so its first appearance
        moves readiness and authority — and nothing else. A reader that observed
        a key before it opened must learn that it did.
        """
        return cls(readiness=True, authority=True)


@dataclass(slots=True)
class IngressChange[K]:
    """The pure invalidation set of one transition.

    The whole contract between the core and a flavor shell.
    """

    #: Per-scope dirty reader kinds, in transition order.
    scopes: list[tuple[K, IngressScopeChange]] = field(default_factory=list)
    #: The accepted-receipt reader grew.
    accepted_receipts: bool = False
    #: The dropped-receipt reader grew.
    dropped_receipts: bool = False
    #: The error-receipt reader grew.
    error_receipts: bool = False

    def is_empty(self) -> bool:
        """Whether this transition dirtied nothing at all."""
        return not (
            self.scopes
            or self.accepted_receipts
            or self.dropped_receipts
            or self.error_receipts
        )

    def mark(self, key: K, change: IngressScopeChange) -> None:
        if not change.is_empty():
            self.scopes.append((key, change))

    def mark_channel(self, channel: IngressReceiptChannel) -> None:
        if channel == IngressReceiptChannel.ACCEPTED:
            self.accepted_receipts = True
        elif channel == IngressReceiptChannel.DROPPED:
            self.dropped_receipts = True
        elif channel == IngressReceiptChannel.ERROR:
            self.error_receipts = True
        else:
            # `IngressReceiptChannel` is produced only inside this package, so a
            # value outside the three-member enum is a defect here, not a peer
            # sending something newer. The old `else` ran the ERROR arm, which
            # would have misrouted a fourth channel into the supervisor's reader
            # and dirtied it on every transition.
            raise ValueError(f"unknown ingress receipt channel: {channel!r}")


# -- The read-only projection every derive is computed from ------------------


@dataclass(frozen=True, slots=True)
class ScopeView:
    """Read-only projection of one scope.

    A shell's reader bodies call these and nothing else, which is why the three
    flavors cannot disagree about readiness, authority, or retry.
    """

    lifecycle: IngressLifecycle
    generation: int
    delivered_through: int | None
    stamped_at: int
    buffered: int
    window_depth: int
    consecutive_errors: int
    observed_now: int
    policy: IngressPolicy

    def is_fresh(self) -> bool:
        """Whether the newest delivered stamp is inside the freshness horizon."""
        return (
            max(0, self.observed_now - self.stamped_at) <= self.policy.freshness_horizon
        )

    def readiness(self) -> IngressReadiness:
        """Derived readiness.

        A scope that has never delivered is ``WARMING``, not ``STALE``: there is
        no stamp to be old.
        """
        if self.lifecycle == IngressLifecycle.CLOSED:
            return IngressReadiness.CLOSED
        if self.lifecycle == IngressLifecycle.SUSPENDED:
            return IngressReadiness.SUSPENDED
        if self.lifecycle == IngressLifecycle.OPENING:
            return IngressReadiness.WARMING
        if self.delivered_through is None:
            return IngressReadiness.WARMING
        return IngressReadiness.READY if self.is_fresh() else IngressReadiness.STALE

    def authority(self) -> IngressAuthority | None:
        """Derived authority. A closed scope claims none."""
        if self.lifecycle == IngressLifecycle.CLOSED:
            return None
        return IngressAuthority(
            self.generation, self.delivered_through, self.stamped_at
        )

    def resume_from(self) -> int:
        """The first sequence not yet delivered in order."""
        return 0 if self.delivered_through is None else self.delivered_through + 1

    def has_gap(self) -> bool:
        """Whether the scope is holding a gap open.

        An out-of-order buffer that a replay, not a retry, is the fix for.
        """
        return self.buffered > 0

    def retry(self) -> IngressRetry | None:
        """Derived retry.

        ``None`` while no error is outstanding — a healthy scope has no backoff,
        rather than a zero one.
        """
        if self.consecutive_errors == 0:
            return None
        shift = min(31, self.consecutive_errors - 1)
        backoff = min(self.policy.retry_base * (1 << shift), self.policy.retry_ceiling)
        return IngressRetry(self.consecutive_errors, backoff, self.resume_from())


class _Scope[T]:
    """Mutable per-key admission state. Internal to the core."""

    __slots__ = (
        "consecutive_errors",
        "delivered_through",
        "generation",
        "lifecycle",
        "pending",
        "stamped_at",
        "window",
        "window_depth",
        "window_present",
    )

    def __init__(self, generation: int) -> None:
        self.lifecycle = IngressLifecycle.OPENING
        self.generation = generation
        self.delivered_through: int | None = None
        self.stamped_at = 0
        self.pending: dict[int, tuple[T, int]] = {}
        self.window: T | None = None
        # An explicit presence flag: ``None`` is a legal payload for a merge
        # algebra over optional values, so "is the window occupied?" must not be
        # inferred from the value.
        self.window_present = False
        self.window_depth = 0
        self.consecutive_errors = 0

    def view(self, observed_now: int, policy: IngressPolicy) -> ScopeView:
        return ScopeView(
            lifecycle=self.lifecycle,
            generation=self.generation,
            delivered_through=self.delivered_through,
            stamped_at=self.stamped_at,
            buffered=len(self.pending),
            window_depth=self.window_depth,
            consecutive_errors=self.consecutive_errors,
            observed_now=observed_now,
            policy=policy,
        )

    def next_expected(self) -> int:
        return 0 if self.delivered_through is None else self.delivered_through + 1

    def stamp(self) -> tuple[IngressLifecycle, int, int | None, bool]:
        """Everything a reader can observe *about shape rather than payload*.

        The buffered path diffs these to derive its invalidation set, so "a
        buffered envelope invalidates nothing" is a computed fact rather than a
        claim — and the handoff-then-buffer case (which clears the window) cannot
        slip through.
        """
        return (
            self.lifecycle,
            self.generation,
            self.delivered_through,
            self.window_present,
        )

    def live_or_opening(self) -> IngressLifecycle:
        return (
            IngressLifecycle.LIVE
            if self.delivered_through is not None
            else IngressLifecycle.OPENING
        )

    def clear_window(self) -> None:
        self.window = None
        self.window_present = False
        self.window_depth = 0


@dataclass(frozen=True, slots=True)
class _Decision:
    """What the admission algebra decided, before any receipt is minted.

    Splitting the decision from its bookkeeping is what keeps the scope mutation
    from interleaving with the receipt log.
    """

    kind: str
    reason: IngressDropReason | None = None
    gap_from: int | None = None
    delivered_through: int | None = None
    conflated: bool = False
    handoff: tuple[int, int] | None = None


class IngressCore[K, T]:
    """Keyed lifecycle scopes, an admission algebra, and a bounded receipt log.

    No context, no reader handles, no reactivity — each flavor wraps this in its
    own shell and owns its own invalidation.
    """

    __slots__ = (
        "_merge",
        "_next_receipt_offset",
        "_observed_now",
        "_policy",
        "_receipts",
        "_scopes",
    )

    def __init__(self, policy: IngressPolicy, merge: MergePolicy[T]) -> None:
        """Build a core over ``policy``.

        The overflow choice is validated against the merge algebra exactly as
        :class:`~lazily.relay.RelayCell` does: ``CONFLATE`` bounds nothing for a
        non-conflating ``⊕``.
        """
        if policy.overflow == Overflow.CONFLATE and not merge.conflates:
            raise IngressConfigError(IngressConfigError.CONFLATE_NOT_BOUNDING)
        if policy.receipt_capacity == 0:
            raise IngressConfigError(IngressConfigError.ZERO_RECEIPT_CAPACITY)
        self._policy = policy
        self._merge = merge
        self._scopes: dict[K, _Scope[T]] = {}
        self._receipts: deque[IngressReceipt[K]] = deque()
        self._next_receipt_offset = 0
        self._observed_now = 0

    # -- Projections ---------------------------------------------------------

    @property
    def policy(self) -> IngressPolicy:
        """The bounds in force."""
        return self._policy

    @property
    def merge_policy(self) -> MergePolicy[T]:
        """The merge algebra the hot window folds under."""
        return self._merge

    def scope_keys(self) -> list[K]:
        """Every known scope key, for a shell rebuilding its reader table."""
        return list(self._scopes)

    def view(self, key: K) -> ScopeView | None:
        """Read-only projection of one scope, or ``None`` when unknown."""
        scope = self._scopes.get(key)
        if scope is None:
            return None
        return scope.view(self._observed_now, self._policy)

    def readiness(self, key: K) -> IngressReadiness:
        """Readiness of a scope.

        Unknown scopes are :attr:`IngressReadiness.UNKNOWN` rather than an error:
        a reader may legitimately observe a key before it opens.
        """
        view = self.view(key)
        return IngressReadiness.UNKNOWN if view is None else view.readiness()

    def authority(self, key: K) -> IngressAuthority | None:
        """Authority claimed by a scope."""
        view = self.view(key)
        return None if view is None else view.authority()

    def retry(self, key: K) -> IngressRetry | None:
        """Retry decision for a scope."""
        view = self.view(key)
        return None if view is None else view.retry()

    def peek(self, key: K) -> T | None:
        """The coalesced window awaiting drain."""
        scope = self._scopes.get(key)
        return None if scope is None else scope.window

    def receipts(self, channel: IngressReceiptChannel) -> list[IngressReceipt[K]]:
        """Receipts on one channel, oldest first."""
        return [
            receipt for receipt in self._receipts if receipt.outcome.channel == channel
        ]

    def all_receipts(self) -> Iterator[IngressReceipt[K]]:
        """Every retained receipt, oldest first, across all channels."""
        return iter(tuple(self._receipts))

    # -- Lifecycle -----------------------------------------------------------

    def open(self, key: K, generation: int) -> IngressChange[K]:
        """Open (or reopen) a scope at ``generation``.

        Reopening a suspended scope preserves its watermark so a replay can
        resume from the gap; reopening a *closed* scope resets it, because a
        closed scope's producer is gone and its sequence space is not resumable.
        """
        change: IngressChange[K] = IngressChange()
        scope = self._scopes.get(key)
        if scope is None:
            self._scopes[key] = _Scope(generation)
            change.mark(key, IngressScopeChange.creation())
            return change
        before = (scope.lifecycle, scope.generation, scope.delivered_through)
        if scope.lifecycle == IngressLifecycle.CLOSED:
            scope = _Scope(generation)
            self._scopes[key] = scope
        else:
            scope.lifecycle = scope.live_or_opening()
            if generation > scope.generation:
                scope.generation = generation
                scope.delivered_through = None
                scope.pending.clear()
        after = (scope.lifecycle, scope.generation, scope.delivered_through)
        if before != after:
            change.mark(
                key,
                IngressScopeChange(
                    value=False,
                    readiness=before[0] != after[0],
                    authority=True,
                    retry=False,
                ),
            )
        return change

    def suspend(self, key: K) -> tuple[IngressChange[K], ReplayRequest | None]:
        """Suspend a scope: retain state and cursors, stop delivering.

        Returns the replay request a reconnect will need, or ``None`` when there
        was nothing to suspend.
        """
        change: IngressChange[K] = IngressChange()
        scope = self._scopes.get(key)
        if scope is None:
            return change, None
        if scope.lifecycle in {IngressLifecycle.SUSPENDED, IngressLifecycle.CLOSED}:
            return change, None
        scope.lifecycle = IngressLifecycle.SUSPENDED
        request = ReplayRequest(scope.generation, scope.next_expected())
        change.mark(key, IngressScopeChange.readiness_only())
        return change, request

    def reconnect(
        self, key: K, generation: int
    ) -> tuple[IngressChange[K], ReplayRequest]:
        """Reconnect a scope at ``generation``, clearing the error streak.

        A higher generation is a producer handoff: the sequence space restarts,
        so the buffered reorder window and the coalesced value are discarded
        rather than replayed against a fence they no longer belong to.
        """
        change: IngressChange[K] = IngressChange()
        created = key not in self._scopes
        scope = self._scopes.setdefault(key, _Scope(generation))
        handoff = generation > scope.generation
        had_window = scope.window_present
        if handoff:
            scope.generation = generation
            scope.delivered_through = None
            scope.pending.clear()
            scope.clear_window()
        before_lifecycle = scope.lifecycle
        scope.lifecycle = scope.live_or_opening()
        had_errors = scope.consecutive_errors > 0
        scope.consecutive_errors = 0
        request = ReplayRequest(scope.generation, scope.next_expected())
        base = IngressScopeChange(
            value=handoff and had_window,
            readiness=before_lifecycle != scope.lifecycle,
            authority=handoff,
            retry=had_errors,
        )
        change.mark(
            key,
            base.union(IngressScopeChange.creation()) if created else base,
        )
        return change, request

    def close(self, key: K) -> IngressChange[K]:
        """Close a scope.

        It admits nothing and claims no authority until reopened.
        """
        change: IngressChange[K] = IngressChange()
        scope = self._scopes.get(key)
        if scope is None or scope.lifecycle == IngressLifecycle.CLOSED:
            return change
        had_window = scope.window_present
        had_errors = scope.consecutive_errors > 0
        scope.lifecycle = IngressLifecycle.CLOSED
        scope.pending.clear()
        scope.clear_window()
        scope.consecutive_errors = 0
        change.mark(
            key,
            IngressScopeChange(
                value=had_window,
                readiness=True,
                authority=True,
                retry=had_errors,
            ),
        )
        return change

    def tick(self, now: int) -> IngressChange[K]:
        """Advance logical time.

        Only scopes that *crossed* the freshness horizon are dirtied — a tick
        inside the horizon invalidates nothing, which is what keeps a polling
        shell from re-rendering on every tick.
        """
        change: IngressChange[K] = IngressChange()
        if now == self._observed_now:
            return change
        policy = self._policy
        before = self._observed_now
        self._observed_now = now
        for key, scope in self._scopes.items():
            if (
                scope.view(before, policy).readiness()
                != scope.view(now, policy).readiness()
            ):
                change.mark(key, IngressScopeChange.readiness_only())
        return change

    def fail(self, key: K, error: IngressError) -> IngressChange[K]:
        """Record a transport/decode failure against a scope, deepening backoff."""
        change: IngressChange[K] = IngressChange()
        created = key not in self._scopes
        scope = self._scopes.setdefault(key, _Scope(0))
        scope.consecutive_errors += 1
        base = IngressScopeChange.retry_only()
        change.mark(
            key,
            base.union(IngressScopeChange.creation()) if created else base,
        )
        channel = self._push_receipt(
            IngressReceipt(
                offset=0,
                key=key,
                generation=scope.generation,
                sequence=None,
                outcome=IngressReceiptOutcome.for_error(error),
            )
        )
        change.mark_channel(channel)
        return change

    def drain(self, key: K) -> tuple[IngressChange[K], T | None]:
        """Drain a scope's coalesced window, resetting its depth.

        Returns ``None`` for an empty window and dirties nothing. A drain is an
        *egress*, not an ack: it never moves the watermark, so a replay after a
        drain still resumes from the same sequence.
        """
        change: IngressChange[K] = IngressChange()
        scope = self._scopes.get(key)
        if scope is None or not scope.window_present:
            return change, None
        value = scope.window
        scope.clear_window()
        change.mark(key, IngressScopeChange.value_only())
        return change, value

    # -- Admission -----------------------------------------------------------

    def admit(
        self, envelope: IngressEnvelope[K, T]
    ) -> tuple[IngressChange[K], IngressAdmission]:
        """Admit one envelope, applying — in this order — scope lifecycle, the
        generation fence, freshness, the generation handoff, dedupe, ordering,
        backpressure, and the merge.

        The order is the contract: a zombie generation is rejected before its
        stale sequence is consulted, and an expired envelope is rejected before
        it can occupy a reorder slot.
        """
        key = envelope.key
        created = key not in self._scopes
        existing = self._scopes.get(key)
        before = None if existing is None else existing.stamp()
        scope = self._scopes.setdefault(key, _Scope(envelope.generation))
        decision = self._decide(scope, envelope)

        # A refused envelope must not leave a scope behind: an expired or blocked
        # message for a key we do not track is not an admission plane, and
        # materializing one would report a readiness change that never happened.
        admitted = decision.kind in {"buffered", "delivered"}
        if created and not admitted:
            del self._scopes[key]

        change: IngressChange[K] = IngressChange()
        surviving = self._scopes.get(key)
        fence = envelope.generation if surviving is None else surviving.generation

        if decision.kind == "refuse":
            assert decision.reason is not None
            channel = self._push_receipt(
                IngressReceipt(
                    offset=0,
                    key=key,
                    generation=fence,
                    sequence=envelope.sequence,
                    outcome=IngressReceiptOutcome.for_dropped(decision.reason),
                )
            )
            change.mark_channel(channel)
            return change, IngressAdmission.dropped(decision.reason)

        if decision.kind == "block":
            channel = self._push_receipt(
                IngressReceipt(
                    offset=0,
                    key=key,
                    generation=fence,
                    sequence=envelope.sequence,
                    outcome=IngressReceiptOutcome.for_dropped(
                        IngressDropReason.BACKPRESSURE
                    ),
                )
            )
            change.mark_channel(channel)
            return change, IngressAdmission.blocked()

        if decision.kind == "buffered":
            # A buffered envelope mints no receipt, and for an already-current
            # scope it dirties no reader, because nothing a reader can observe
            # moved. Two cases are NOT invisible and are derived rather than
            # assumed: the scope's own first appearance (it moves off
            # ``UNKNOWN``), and a generation handoff that buffers — which resets
            # the fence, the watermark, and the window before parking the
            # envelope.
            scope_change = (
                IngressScopeChange.creation() if created else IngressScopeChange()
            )
            after = None if surviving is None else surviving.stamp()
            if before is not None and after is not None:
                scope_change = scope_change.union(
                    IngressScopeChange(
                        value=before[3] != after[3],
                        readiness=before[0] != after[0]
                        or (before[2] is None) != (after[2] is None),
                        authority=before[1] != after[1] or before[2] != after[2],
                        retry=False,
                    )
                )
            change.mark(key, scope_change)
            assert decision.gap_from is not None
            return change, IngressAdmission.buffered(decision.gap_from)

        assert decision.delivered_through is not None
        change.mark(key, IngressScopeChange.all())
        channel = self._push_receipt(
            IngressReceipt(
                offset=0,
                key=key,
                generation=fence,
                sequence=envelope.sequence,
                outcome=IngressReceiptOutcome.for_accepted(
                    decision.delivered_through, decision.conflated
                ),
            )
        )
        change.mark_channel(channel)
        if decision.handoff is not None:
            admission = IngressAdmission.generation_handoff(
                decision.handoff[0], decision.handoff[1], decision.delivered_through
            )
        elif decision.conflated:
            admission = IngressAdmission.conflated(decision.delivered_through)
        else:
            admission = IngressAdmission.accepted(decision.delivered_through)
        return change, admission

    def _decide(self, scope: _Scope[T], envelope: IngressEnvelope[K, T]) -> _Decision:
        """The admission algebra proper.

        Pure over one scope, mutating only that scope, minting nothing.
        """
        policy = self._policy
        generation = envelope.generation
        sequence = envelope.sequence
        stamped_at = envelope.stamped_at

        if scope.lifecycle == IngressLifecycle.CLOSED:
            return _Decision("refuse", reason=IngressDropReason.SCOPE_CLOSED)
        if generation < scope.generation:
            return _Decision("refuse", reason=IngressDropReason.STALE_GENERATION)
        if max(0, self._observed_now - stamped_at) > policy.freshness_horizon:
            return _Decision("refuse", reason=IngressDropReason.EXPIRED)

        handoff: tuple[int, int] | None = None
        if generation > scope.generation:
            # A handoff is a baseline reset, not a continuation: the new
            # incarnation's first envelope is authoritative, so the old
            # incarnation's undrained window and buffered successors are
            # discarded rather than folded into it. Merging a superseded delta
            # into a fresh baseline is exactly the build-skew corruption the
            # generation fence exists to prevent, and it is the same rule
            # ``reconnect`` at a higher generation applies.
            handoff = (scope.generation, generation)
            scope.generation = generation
            scope.delivered_through = None
            scope.pending.clear()
            scope.clear_window()

        expected = scope.next_expected()
        if sequence < expected:
            return _Decision("refuse", reason=IngressDropReason.DUPLICATE_SEQUENCE)
        if sequence > expected:
            if sequence in scope.pending:
                return _Decision("refuse", reason=IngressDropReason.DUPLICATE_BUFFERED)
            if len(scope.pending) >= policy.reorder_window:
                return _Decision(
                    "refuse", reason=IngressDropReason.REORDER_WINDOW_OVERFLOW
                )
            scope.pending[sequence] = (envelope.payload, stamped_at)
            return _Decision("buffered", gap_from=expected)

        # In order. Backpressure is checked here and not earlier: refusing an
        # in-order envelope leaves a gap the reorder buffer cannot close, so
        # ``BLOCK`` must be observable by the producer as its own outcome.
        if scope.window_depth >= policy.high_water:
            if policy.overflow == Overflow.BLOCK:
                return _Decision("block")
            if policy.overflow == Overflow.DROP_NEWEST:
                return _Decision("refuse", reason=IngressDropReason.BACKPRESSURE)
            if policy.overflow == Overflow.DROP_OLDEST:
                scope.clear_window()
            # ``CONFLATE`` *is* the bound; ``SPILL`` degrades to it until a
            # durable tail is wired, exactly as ``RelayCell`` does.

        conflated = self._merge_into(scope, envelope.payload, stamped_at)
        scope.delivered_through = sequence
        scope.lifecycle = IngressLifecycle.LIVE
        scope.consecutive_errors = 0
        delivered_through = sequence

        # Flush every buffered successor this delivery unblocked. One
        # invalidation covers the whole flush: readers observe the coalesced
        # window, never a partial replay.
        while True:
            nxt = scope.next_expected()
            buffered = scope.pending.pop(nxt, None)
            if buffered is None:
                break
            conflated = self._merge_into(scope, buffered[0], buffered[1]) or conflated
            scope.delivered_through = nxt
            delivered_through = nxt

        return _Decision(
            "delivered",
            delivered_through=delivered_through,
            conflated=conflated,
            handoff=handoff,
        )

    def _merge_into(self, scope: _Scope[T], payload: T, stamped_at: int) -> bool:
        """Merge one payload into a scope's hot head.

        Returns whether it coalesced with an existing window.
        """
        if scope.window_present:
            # ``window_present`` is the invariant that makes the cast sound: the
            # window holds a payload, and ``None`` is itself a legal payload for a
            # merge algebra over optional values, so the flag is the only honest
            # occupancy test.
            scope.window = self._merge.merge(cast("T", scope.window), payload)
            conflated = True
        else:
            scope.window = payload
            conflated = False
        scope.window_present = True
        scope.window_depth += 1
        scope.stamped_at = max(scope.stamped_at, stamped_at)
        return conflated

    def _push_receipt(self, receipt: IngressReceipt[K]) -> IngressReceiptChannel:
        stamped = replace(receipt, offset=self._next_receipt_offset)
        self._next_receipt_offset += 1
        self._receipts.append(stamped)
        while len(self._receipts) > self._policy.receipt_capacity:
            self._receipts.popleft()
        return stamped.outcome.channel
