"""Library dispatch audit: every silent default in the shipped package is
either PINNED as deliberate wire leniency or PROVEN to fail closed.

The sibling sweep over the *conformance runners* converted 174 fail-open
dispatch sites, because in a runner a silent default is always a bug. In a
LIBRARY it is sometimes the wire contract — an unknown blob-backend token, an
unrecognised statechart marker, an FFI boundary that may not raise. So this file
is deliberately two-sided:

* ``test_pin_*`` feeds an unknown value at a site whose leniency is intentional
  and asserts the lenient outcome. An undocumented default and a deliberate one
  are indistinguishable from the outside; these tests are what makes them
  distinguishable, and they will fail if someone "hardens" the site without
  reading the wire reason at it.
* ``test_reject_*`` feeds an unknown value at a site that was converted to fail
  closed and asserts the named rejection. Each one was mutation-checked by
  restoring the old lenient arm and confirming the test goes red.

Every site named here has a matching comment in the library stating its wire
reason.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

import pytest

from lazily.async_context import AsyncContext
from lazily.ffi import (
    LazilyFfiMessageKind,
    LazilyFfiStatus,
    decode_message,
    encode_message,
    kind_of,
)
from lazily.ingress_core import IngressChange, IngressReceiptChannel
from lazily.interop_peer import InteropPeer
from lazily.ipc import BlobBackendKind, CapabilityHandshake, Delta, IpcMessage
from lazily.lossless_tree_crdt import ROOT as TREE_ROOT
from lazily.lossless_tree_crdt import (
    CreateNode,
    LosslessTreeCrdt,
    SortKey,
    TreeOp,
    TreeOpId,
)
from lazily.relay import SpillMode, SpillStore
from lazily.reliable_sync import InMemoryOutbox, SyncDriver
from lazily.statechart import ChartDef, StateChart


# ===========================================================================
# PINS — deliberate leniency. Feeding the unknown value must NOT raise.
# ===========================================================================


def test_pin_blob_backend_unknown_wire_token_resolves_as_shm() -> None:
    """``BlobBackendKind.from_wire`` — an unrecognised backend token reads as
    :attr:`SHM`, the omit-when-default value.

    A legacy producer that predates the field and a future producer naming a
    backend this build has never heard of both present as "no usable token", and
    the descriptor body is backend-independent, so SHM reads the same bytes the
    legacy form would.
    """
    assert BlobBackendKind.from_wire("quantum-tunnel") is BlobBackendKind.SHM
    assert BlobBackendKind.from_wire("") is BlobBackendKind.SHM
    # The known tokens still resolve to themselves — the default is a fallback,
    # not a blanket.
    assert BlobBackendKind.from_wire("shm") is BlobBackendKind.SHM
    assert BlobBackendKind.from_wire("arrow") is BlobBackendKind.ARROW
    assert BlobBackendKind.from_wire("in_process") is BlobBackendKind.IN_PROCESS


def test_pin_capability_handshake_optional_fields_default_conservatively() -> None:
    """``CapabilityHandshake.from_wire`` — the three optional capability fields
    default to the conservative reading of silence, and unknown extra keys are
    ignored so a capability addition is not a breaking change.
    """
    minimal = {
        "protocol_id": "lazily",
        "protocol_major_version": 1,
        "codec": "json",
        "max_frame_size": 65536,
        "peer_id": 7,
        "session_id": "s-1",
        # fragmentation_supported / ordered_reliable / features all omitted
        "a_capability_this_build_has_never_heard_of": {"nested": True},
    }
    hs = CapabilityHandshake.from_wire(minimal)
    assert hs.fragmentation_supported is False, (
        "a peer that never claimed fragmentation is assumed unable to reassemble"
    )
    assert hs.ordered_reliable is True, "the base transport contract"
    assert list(hs.features) == [], "an unnamed feature is an ungated feature"

    # Identity fields are NOT lenient — the leniency is scoped to capabilities.
    for required in ("protocol_id", "codec", "peer_id"):
        broken = dict(minimal)
        del broken[required]
        with pytest.raises(KeyError):
            CapabilityHandshake.from_wire(broken)


def test_pin_statechart_unrecognised_state_marker_reads_as_atomic() -> None:
    """``_parse_state`` — a state carrying only markers this build has no arm
    for is an atomic LEAF, so a partially-understood chart still runs.
    """
    defn = ChartDef.from_chart(
        {
            "initial": "root",
            "states": {
                "root": {"initial": "known"},
                "known": {"parent": "root"},
                "future": {"parent": "root", "kind": "hyperstate"},
            },
        }
    )
    assert defn.kind("future") == "atomic"
    assert defn.is_leaf("future") is True
    # The *closed* vocabularies in the same parser still fail closed.
    with pytest.raises(TypeError, match="unknown history kind"):
        ChartDef.from_chart(
            {
                "initial": "root",
                "states": {
                    "root": {"initial": "h"},
                    "h": {"parent": "root", "history": "sideways"},
                },
            }
        )


def test_pin_statechart_undeclared_transition_target_reads_as_atomic() -> None:
    """``ChartDef.kind`` — a transition target that names no declared state is
    treated as an atomic leaf, so one unknown target does not kill the chart.
    """
    defn = ChartDef.from_chart(
        {
            "initial": "root",
            "states": {
                "root": {"initial": "a"},
                "a": {"parent": "root", "on": {"GO": "never_declared"}},
            },
        }
    )
    assert defn.kind("never_declared") == "atomic"

    ctx: dict = {}
    chart = StateChart(ctx, defn)
    assert chart.send("GO") is True, "the transition fires rather than raising"
    assert "never_declared" in chart.configuration()


def test_pin_statechart_unknown_guard_name_denies() -> None:
    """``_guard_passes`` — a guard the host did not supply evaluates False, so
    the transition does not fire. The unknown value denies rather than admits.
    """
    defn = ChartDef.from_chart(
        {
            "initial": "root",
            "states": {
                "root": {"initial": "closed"},
                "closed": {
                    "parent": "root",
                    "on": {"OPEN": {"target": "open", "guard": "unlocked"}},
                },
                "open": {"parent": "root"},
            },
        }
    )
    ctx: dict = {}
    chart = StateChart(ctx, defn)

    assert chart.send("OPEN", {}) is False, "absent guard name -> denied"
    assert chart.send("OPEN", {"a_different_guard": True}) is False, (
        "an unrelated guard key does not admit"
    )
    assert chart.matches("closed")
    # Supplying it True is the only thing that admits.
    assert chart.send("OPEN", {"unlocked": True}) is True
    assert chart.matches("open")


def test_pin_ffi_boundary_reports_unknown_frames_as_status_not_exception() -> None:
    """``kind_of`` / ``encode_message`` / ``decode_message`` — no Python
    exception may unwind through the C ABI, so an unrepresentable message
    becomes a status code. It is a rejection, never a misclassification.
    """
    empty = IpcMessage()
    assert kind_of(empty) is LazilyFfiMessageKind.Unknown
    status, kind, payload = encode_message(empty)
    assert status is LazilyFfiStatus.InvalidMessage
    assert kind is LazilyFfiMessageKind.Unknown
    assert payload == b""

    # A well-formed JSON frame carrying an envelope tag this build has no arm
    # for: rejected as a status, and no message is handed back.
    status, message = decode_message(b'{"Wormhole":{"epoch":1}}')
    assert status is LazilyFfiStatus.InvalidMessage
    assert message is None

    # Missing required field inside a known variant -> the same status.
    status, message = decode_message(b'{"Delta":{}}')
    assert status is LazilyFfiStatus.InvalidMessage
    assert message is None

    # A frame this build DOES understand still round-trips.
    good = IpcMessage.of_delta(Delta.new(0, 1, []))
    status, _kind, payload = encode_message(good)
    assert status is LazilyFfiStatus.Ok
    assert decode_message(payload) == (LazilyFfiStatus.Ok, good)


# ===========================================================================
# REJECTIONS — converted fail-open sites. The unknown value must now raise.
# ===========================================================================


@dataclass(frozen=True)
class _AlienOpKind:
    """A ``TreeOpKind`` value outside the closed six-variant union."""

    node: TreeOpId


def _tid(counter: int, peer: int = 1) -> TreeOpId:
    return TreeOpId(counter=counter, peer=peer)


def test_reject_unknown_tree_op_kind_in_dependency_check() -> None:
    """``_dependencies_ready`` used to return False for an unknown kind, which
    re-buffered the op forever: never applied, never reported, ``_buffered``
    growing without bound."""
    crdt = LosslessTreeCrdt(peer=1)
    op = TreeOp(id=_tid(1), kind=_AlienOpKind(node=_tid(1)))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="unknown TreeOp kind: _AlienOpKind"):
        crdt._dependencies_ready(op)


def test_reject_unknown_tree_op_kind_in_apply() -> None:
    """``_apply_op``'s ``match`` had no wildcard, so an unknown kind fell
    through and did NOTHING while ``_record`` still put it in the frontier and
    log — a silent divergence between two replicas that both claim to have
    applied it."""
    crdt = LosslessTreeCrdt(peer=1)
    op = TreeOp(id=_tid(1), kind=_AlienOpKind(node=_tid(1)))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="unknown TreeOp kind: _AlienOpKind"):
        crdt._apply_op(op)


@dataclass(frozen=True)
class _AlienSeed:
    """A ``NodeSeed`` value outside ``SeedElement | SeedLeaf``."""

    kind: str


def test_reject_unknown_node_seed_variant() -> None:
    """The seed ``match``'s wildcard *ran the SeedElement arm*, so a third seed
    shape silently materialised as an element shell and its text was dropped."""
    crdt = LosslessTreeCrdt(peer=1)
    op = TreeOp(
        id=_tid(9),
        kind=CreateNode(
            id=_tid(9),
            parent=TREE_ROOT,
            sort=SortKey(frac=(128,), peer=1),
            seed=_AlienSeed(kind="paragraph"),  # type: ignore[arg-type]
        ),
    )
    with pytest.raises(TypeError, match="unknown NodeSeed variant: _AlienSeed"):
        crdt._apply_op(op)


def test_reject_unknown_ingress_receipt_channel() -> None:
    """``mark_channel``'s ``else`` ran the ERROR arm, so a channel outside the
    three-member enum would have been misrouted into the supervisor's reader."""
    change: IngressChange[str] = IngressChange()
    with pytest.raises(ValueError, match="unknown ingress receipt channel"):
        change.mark_channel("quarantined")  # type: ignore[arg-type]

    # The three real channels each mark their own reader and nothing else.
    for channel, attr in (
        (IngressReceiptChannel.ACCEPTED, "accepted_receipts"),
        (IngressReceiptChannel.DROPPED, "dropped_receipts"),
        (IngressReceiptChannel.ERROR, "error_receipts"),
    ):
        one: IngressChange[str] = IngressChange()
        one.mark_channel(channel)
        assert getattr(one, attr) is True
        others = {"accepted_receipts", "dropped_receipts", "error_receipts"} - {attr}
        assert not any(getattr(one, o) for o in others)


class _KeepLatest:
    def merge(self, _a: int, b: int) -> int:
        return b


def test_reject_unknown_spill_mode() -> None:
    """``SpillStore.spill`` treated "not APPEND_COMPACT" as COMPACT_ON_WRITE, so
    an unrecognised mode silently got merge-on-write semantics it never asked
    for."""
    store: SpillStore[int] = SpillStore(SpillMode.APPEND_COMPACT, 4, _KeepLatest())
    store.mode = "AppendCompactV2"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="unknown spill mode"):
        store.spill(1, 10)

    # Both real modes still work and differ.
    append: SpillStore[int] = SpillStore(SpillMode.APPEND_COMPACT, 4, _KeepLatest())
    compact: SpillStore[int] = SpillStore(SpillMode.COMPACT_ON_WRITE, 4, _KeepLatest())
    for i in range(3):
        append.spill(i, 1)
        compact.spill(i, 1)
    assert len(append.manifest()) == 3
    assert len(compact.manifest()) == 1


class _NullSink:
    def send(self, _message: IpcMessage) -> bool:
        return True


class _ScriptedSource:
    def __init__(self, messages: list[IpcMessage]) -> None:
        self._queue = deque(messages)

    def recv(self) -> IpcMessage | None:
        return self._queue.popleft() if self._queue else None


class _NoProvider:
    def snapshot(self, from_epoch: int) -> IpcMessage:  # pragma: no cover - unused
        raise AssertionError("no resync expected")


class _ZeroClock:
    def now_millis(self) -> int:
        return 0


def test_reject_ipc_message_with_no_variant_set_in_driver() -> None:
    """``SyncDriver.tick``'s ``else`` fed a message with every variant slot
    unset to the reliable-sync coordinator as if it were a Snapshot/Delta."""
    driver = SyncDriver(
        _NullSink(),
        _ScriptedSource([IpcMessage()]),
        InMemoryOutbox(),
        _ZeroClock(),
        _NoProvider(),
        0,
    )
    with pytest.raises(ValueError, match="carries no Snapshot, Delta, CrdtSync"):
        driver.tick()


def test_reject_non_node_member_in_async_teardown_scope() -> None:
    """``AsyncTeardownScope.aclose``'s ``else`` ran the AsyncComputed arm on
    whatever it was handed, so a non-node member was "disposed" by a code path
    that reads private slot fields and the scope silently leaked it."""

    async def run() -> None:
        ctx = AsyncContext()
        scope = ctx.scope()
        scope.adopt(object())
        with pytest.raises(TypeError, match="not an async reactive node"):
            await scope.aclose()

    asyncio.run(run())


def test_reject_unknown_feature_token_in_interop_peer_step() -> None:
    """``_feature_step``'s ``else`` ran the revision-barrier arm for every token
    that was not one of the first two, so stepping a feature this build does not
    implement returned barrier observations under that feature's name."""
    peer = InteropPeer()
    # Seed state directly: `_feature_reset` gates on `_supported_feature`, so the
    # drift this guards is a token added to that allowlist and not to the
    # dispatch below it.
    peer._stdlib["stdlib_quantum_v1"] = {"last": None}
    with pytest.raises(ValueError, match="unsupported feature"):
        peer._feature_step({"feature": "stdlib_quantum_v1", "step": {"op": "start"}})


def test_reject_unknown_timeout_operation_outcome() -> None:
    """The timeout ``operation`` fallthrough *was* the ``pending`` arm, so a
    fixture typo or a fourth outcome produced a green "pending" run against an
    assertion nobody wrote."""
    peer = InteropPeer()
    assert peer.handle({"cmd": "feature_reset", "feature": "stdlib_timeout_v1"})["ok"]
    started = peer.handle(
        {
            "cmd": "feature_step",
            "feature": "stdlib_timeout_v1",
            "step": {"op": "start", "now": 0, "duration": 100},
        }
    )
    assert started["observation"]["outcome"] == "pending"

    with pytest.raises(ValueError, match="unknown timeout operation outcome"):
        peer.handle(
            {
                "cmd": "feature_step",
                "feature": "stdlib_timeout_v1",
                "step": {
                    "op": "poll",
                    "now": 1,
                    "operation": "half_completed",
                    "cancellation": "pending",
                },
            }
        )

    # The three real outcomes are all still accepted — the corpus carries each
    # of them (`lazily-spec/conformance/stdlib/timeout.json`).
    for outcome in ("pending", "completed", "unavailable"):
        peer.handle({"cmd": "feature_reset", "feature": "stdlib_timeout_v1"})
        peer.handle(
            {
                "cmd": "feature_step",
                "feature": "stdlib_timeout_v1",
                "step": {"op": "start", "now": 0, "duration": 100},
            }
        )
        polled = peer.handle(
            {
                "cmd": "feature_step",
                "feature": "stdlib_timeout_v1",
                "step": {
                    "op": "poll",
                    "now": 1,
                    "operation": outcome,
                    "value": "v",
                    "cancellation": "pending",
                },
            }
        )
        assert polled["ok"] is True
