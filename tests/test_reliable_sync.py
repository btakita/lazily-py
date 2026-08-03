"""Reliable sync conformance + SyncDriver loop-shape tests (``#lzsync``).

Replays the canonical ``lazily-spec/conformance/reliable-sync`` fixtures against
the native :class:`~lazily.ResyncCoordinator` / :class:`~lazily.InMemoryOutbox` /
:class:`~lazily.OrSet` / :class:`~lazily.WireLwwRegister`, round-trips the two
control frames (:class:`~lazily.ResyncRequest` / :class:`~lazily.OutboxAck`)
through JSON, and pins the :class:`~lazily.SyncDriver` loop shape over a scripted
in-memory transport (mirroring lazily-js ``reliable-sync.test.js`` and lazily-rs
``reliable_sync.rs``). Cross-language pin with lazily-rs / lazily-kt / lazily-js;
backstop lazily-formal ``ReliableSync.lean``.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from conformance_assert import (
    assert_key,
    assert_key_with,
    instrument,
    scenario_view,
)

from lazily import (
    Delta,
    DriverError,
    InMemoryOutbox,
    InMemoryStore,
    IpcMessage,
    OrSet,
    OutboxAck,
    Progress,
    ResyncCoordinator,
    ResyncRequest,
    Snapshot,
    SqliteOutbox,
    SqliteStore,
    SyncDriver,
    WireLwwRegister,
    WireStamp,
)
from lazily.ipc import (
    DeltaOp_CellSet,
    DeltaOp_SlotValue,
    IpcValue_Inline,
    NodeState_Payload,
)
from lazily.reliable_sync import Outbox


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance" / "reliable-sync"
_SPEC_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "lazily-spec"
    / "conformance"
    / "reliable-sync"
)


def _load_fixture(name: str) -> dict:
    spec_path = _SPEC_FIXTURES / name
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / name
    return instrument(json.loads(path.read_text()), name=f"reliable-sync/{name}")


def _scenario(fx: dict, scenario_id: str) -> dict:
    """Look one scenario up by id, booking it as replayed (#lzscenariocoverage).

    Booking happens on the match, not on the scan: `next(...)` walks past every
    scenario ahead of the one asked for, and booking those would claim replay for
    scenarios no test here enters — which is exactly the partial replay this rung
    exists to surface.

    Keyed on `id`, never `name` (#recommendedconformanceco): `name` is a prose
    label, so keying on it means a copy-edit upstream silently stops matching.
    """
    index, scenario = next(
        (i, s) for i, s in enumerate(fx["scenarios"]) if s["id"] == scenario_id
    )
    return scenario_view(getattr(fx, "conformance_name", ""), scenario, index)


def _msg(wire: dict) -> IpcMessage:
    return IpcMessage.from_wire(wire)


def _fold(state: dict[str, list[int]], message: IpcMessage) -> None:
    """Apply an accepted frame to a ``{node_id: payload}`` image.

    The fixtures carry ops and node payloads, and the ``state_after`` /
    ``converged_nodes`` expectations are about the folded image — not about the
    cursor. Replaying deltas with their ops stripped (which this runner used to
    do) makes those keys unreachable, and an unreachable assertion key is exactly
    the silent skip #lzassertunknownkeys is about.
    """
    if message.is_delta:
        delta = message.delta
        assert delta is not None
        for op in delta.ops:
            if isinstance(op, DeltaOp_CellSet | DeltaOp_SlotValue):
                payload = op.payload
                assert isinstance(payload, IpcValue_Inline)
                state[str(op.node)] = list(payload.data)
        return
    if message.is_snapshot:
        snapshot = message.snapshot
        assert snapshot is not None
        # A snapshot is a full image: it REPLACES the receiver's state.
        state.clear()
        for node in snapshot.nodes:
            if isinstance(node.state, NodeState_Payload):
                state[str(node.node)] = list(node.state.data)


# ---------------------------------------------------------------------------
# control-frame serde round-trip
# ---------------------------------------------------------------------------


def test_resync_request_round_trips_json() -> None:
    m = IpcMessage.of_resync_request(ResyncRequest(from_epoch=2))
    text = m.encode_json().decode("utf-8")
    assert text == '{"ResyncRequest":{"from_epoch":2}}'
    assert IpcMessage.decode_json(text).to_wire() == m.to_wire()


def test_outbox_ack_round_trips_json() -> None:
    m = IpcMessage.of_outbox_ack(OutboxAck(through_epoch=41))
    text = m.encode_json().decode("utf-8")
    assert text == '{"OutboxAck":{"through_epoch":41}}'
    assert IpcMessage.decode_json(text).to_wire() == m.to_wire()


def test_control_frame_ffi_kinds() -> None:
    from lazily import LazilyFfiMessageKind, kind_of

    assert kind_of(IpcMessage.of_resync_request(ResyncRequest(from_epoch=2))) is (
        LazilyFfiMessageKind.ResyncRequest
    )
    assert kind_of(IpcMessage.of_outbox_ack(OutboxAck(through_epoch=1))) is (
        LazilyFfiMessageKind.OutboxAck
    )
    assert LazilyFfiMessageKind.ResyncRequest.value == 4
    assert LazilyFfiMessageKind.OutboxAck.value == 5


# ---------------------------------------------------------------------------
# multi_epoch_delta.json
# ---------------------------------------------------------------------------


def _action(result: object) -> str:
    """The fixture's ``action`` tag for a coordinator ingest result."""
    if result.is_apply:  # type: ignore[attr-defined]
        return "Apply"
    if result.is_request_snapshot:  # type: ignore[attr-defined]
        return "RequestSnapshot"
    return "Ignore"


def test_multi_epoch_delta() -> None:
    fx = _load_fixture("multi_epoch_delta.json")
    assert fx["kind"] == "ReliableSync"
    a = fx["assertions"]

    sc = _scenario(fx, "span_3_applies_equal_to_unit_fold")
    wire = sc["delta"]

    # Decode FIRST, and assert the fixture-level block against the DECODED
    # frame. These five keys used to be read straight out of `sc["delta"]` —
    # `assertions.epoch` compared to `scenarios[...].delta.epoch`, the fixture
    # against itself — so a runner that never built an `IpcMessage` satisfied
    # all of them, which is the self-comparison shape `#lznullformblind` sweeps
    # for.
    delta = _msg({"Delta": wire})
    body = delta.delta
    assert body is not None, "the fixture declares the Delta variant"
    base = body.base_epoch
    epoch = body.epoch
    assert_key(a, "base_epoch", base)
    assert_key(a, "epoch", epoch)
    assert_key(a, "span", epoch - base)
    assert_key(a, "is_multi_epoch", epoch > base + 1)
    assert_key(a, "op_count", len(body.ops))

    coord = ResyncCoordinator(sc["receiver_last_epoch"])
    state: dict[str, list[int]] = {}
    result = coord.ingest(delta)
    expect = sc["expect"]
    assert_key(expect, "action", _action(result))
    assert_key(expect, "applied", result.is_apply)
    _fold(state, delta)
    assert_key(expect, "receiver_last_epoch_after", coord.last_epoch)

    # `fold_equivalent` / `atomic_advance`: one span-3 delta must leave the same
    # cursor AND the same image as the equivalent run of unit deltas, and the
    # cursor must reach `epoch` in one step rather than passing through 41/42.
    unit = ResyncCoordinator(sc["receiver_last_epoch"])
    unit_state: dict[str, list[int]] = {}
    cursors = []
    for frame in sc["equivalent_unit_fold"]:
        message = _msg({"Delta": frame})
        assert unit.ingest(message).is_apply
        _fold(unit_state, message)
        cursors.append(unit.last_epoch)
    assert_key(
        expect,
        "fold_equivalent",
        unit_state == state and unit.last_epoch == coord.last_epoch,
    )
    assert (cursors == list(range(base + 1, epoch + 1))) and (len(cursors) > 1), (
        "the unit fold is the multi-step control for atomic_advance"
    )
    assert_key(expect, "atomic_advance", coord.last_epoch == epoch)

    gap = _scenario(fx, "gap_rule_unchanged_under_span")
    gc = ResyncCoordinator(gap["receiver_last_epoch"])
    res = gc.ingest_delta(
        Delta.new(gap["delta"]["base_epoch"], gap["delta"]["epoch"], [])
    )
    gap_expect = gap["expect"]
    assert_key(gap_expect, "action", _action(res))
    assert_key(gap_expect, "applied", res.is_apply)
    assert_key(gap_expect, "request_from", res.from_epoch)
    assert gc.last_epoch == gap["receiver_last_epoch"]
    assert_key(gap_expect, "receiver_last_epoch_after", gc.last_epoch)


# ---------------------------------------------------------------------------
# resync_gap_converge.json
# ---------------------------------------------------------------------------


def test_resync_gap_converge() -> None:
    fx = _load_fixture("resync_gap_converge.json")

    sc = _scenario(fx, "drop_suffix_then_resync_converges")
    coord = ResyncCoordinator(sc["start_last_epoch"])
    state: dict[str, list[int]] = {}
    covering_snapshot: IpcMessage | None = None
    requests = 0
    for frame in sc["inbound"]:
        if frame.get("dropped"):
            continue
        message = _msg(frame["frame"])
        res = coord.ingest(message)
        if frame["expect_action"] == "Apply":
            assert res.is_apply
            _fold(state, message)
            if message.is_snapshot:
                covering_snapshot = message
        elif frame["expect_action"] == "RequestSnapshot":
            requests += 1
            assert res.is_request_snapshot
            assert res.from_epoch == frame["request_from"]
        elif frame["expect_action"] == "Ignore":
            assert res.is_ignore
        else:
            # The final arm used to be a bare `else: assert res.is_ignore`, which
            # ASSUMED the remaining variant instead of naming it: any
            # `expect_action` the corpus grows would have been silently checked
            # against `Ignore` and reported green (#lzscenariobodyskip).
            raise AssertionError(
                f"unknown resync expect_action {frame['expect_action']!r}"
            )
        assert coord.last_epoch == frame["last_epoch_after"]
    expect = sc["expect"]
    assert_key(expect, "final_last_epoch", coord.last_epoch)
    assert_key(expect, "resync_requests_emitted", requests)
    assert_key_with(
        expect,
        "converged_nodes",
        lambda want: state == {k: list(v) for k, v in want.items()},
    )

    # `equals_no_drop_receiver`: the fixture does not carry the dropped delta's
    # ops, so the no-drop receiver is reconstructed from the covering snapshot —
    # which is by definition the full image a receiver that missed nothing holds
    # at that epoch. Gap recovery is state-equivalent, not lossy.
    assert covering_snapshot is not None, "the scenario never applied a snapshot"
    no_drop: dict[str, list[int]] = {}
    _fold(no_drop, covering_snapshot)
    assert_key(expect, "equals_no_drop_receiver", no_drop == state)

    single = _scenario(fx, "single_request_per_gap")
    c2 = ResyncCoordinator(single["start_last_epoch"])
    req2 = 0
    for frame in single["inbound"]:
        if c2.ingest(_msg(frame["frame"])).is_request_snapshot:
            req2 += 1
    single_expect = single["expect"]
    assert_key(single_expect, "resync_requests_emitted", req2)
    assert_key(single_expect, "final_last_epoch", c2.last_epoch)


# ---------------------------------------------------------------------------
# idempotent_redelivery.json
# ---------------------------------------------------------------------------


def test_idempotent_redelivery() -> None:
    fx = _load_fixture("idempotent_redelivery.json")
    for name in ("replayed_delta_is_ignored", "duplicate_current_head_is_ignored"):
        sc = _scenario(fx, name)
        coord = ResyncCoordinator(sc["start_last_epoch"])
        before = {k: list(v) for k, v in sc["state_before"].items()}
        state = {k: list(v) for k, v in before.items()}
        for frame in sc["inbound"]:
            result = coord.ingest(_msg(frame["frame"]))
            assert result.is_ignore, name
            if result.is_apply:
                _fold(state, _msg(frame["frame"]))
            assert coord.last_epoch == frame["last_epoch_after"]
        expect = sc["expect"]
        assert_key(expect, "final_last_epoch", coord.last_epoch, where=name)
        # At-least-once delivery, exactly-once effect: the re-delivered frame
        # carries ops that WOULD change the image, so an image compare is the
        # only thing that separates "ignored" from "applied twice, same result".
        assert_key_with(
            expect,
            "state_after",
            lambda want, state=state: state == {k: list(v) for k, v in want.items()},
            where=name,
        )
        assert_key(expect, "net_effect_unchanged", state == before, where=name)


def _frames_of(sc: dict, key: str) -> list[tuple[int, IpcMessage]]:
    return [(e["epoch"], IpcMessage.from_wire(e["frame"])) for e in sc[key]]


# ---------------------------------------------------------------------------
# outbox_replay_after_crash.json
# ---------------------------------------------------------------------------


def test_outbox_replay_after_crash(tmp_path: Path) -> None:
    fx = _load_fixture("outbox_replay_after_crash.json")
    sc = _scenario(fx, "crash_between_append_and_ack_replays_on_reconnect")
    appended = _frames_of(sc, "appended")
    ack = sc["ack_through"]
    cursor = sc["reconnect_cursor"]

    path = tmp_path / "outbox.sqlite3"

    mem = InMemoryOutbox()
    durable = SqliteOutbox(path, "doc")
    for e, m in appended:
        mem.append(e, m)
        durable.append(e, m)
    mem.ack_through(ack)
    durable.ack_through(ack)

    expect = sc["expect"]
    assert_key(expect, "retained_after_ack", mem.retained_epochs())
    assert durable.retained_epochs() == mem.retained_epochs()
    durable.close()

    # "crash": reopen the durable SQLite outbox from disk.
    durable = SqliteOutbox(path, "doc")
    replay = durable.replay_from(cursor)
    replayed = [e for (e, _) in replay]
    assert_key(expect, "replayed_from_cursor", replayed)
    # Replay is ordered, not merely a set: a reconnect that resends the suffix
    # out of order would still satisfy `replayed_from_cursor` as a membership
    # check, so the order is its own assertion.
    assert_key(expect, "replay_order", replayed)
    assert replayed == sorted(replayed)

    coord = ResyncCoordinator(cursor)
    applied: list[int] = []
    for _e, m in replay:
        if coord.ingest(m).is_apply:
            applied.append(coord.last_epoch)
    assert_key(expect, "receiver_applies", applied)
    assert_key(expect, "receiver_last_epoch_after", coord.last_epoch)
    durable.close()

    # At-least-once delivery, exactly-once effect: nothing in the unacked
    # suffix went missing and nothing was applied twice.
    unacked = [e for (e, _) in appended if e > ack]
    lost = [e for e in unacked if e not in applied]
    doubled = [e for e in set(applied) if applied.count(e) > 1]
    assert_key(expect, "ops_lost", len(lost))
    assert_key(expect, "ops_doubled", len(doubled))
    assert_key(expect, "exactly_once_effect", not lost and not doubled)

    # send_failure_retains_frame_for_next_tick
    sc2 = _scenario(fx, "send_failure_retains_frame_for_next_tick")
    expect2 = sc2["expect"]
    mem2 = InMemoryOutbox()
    appended2 = _frames_of(sc2, "appended")
    for e, m in appended2:
        mem2.append(e, m)
    # The send fails, so nothing is acked; the frame stays in the outbox.
    assert sc2["ack_through"] is None
    retained2 = mem2.retained_epochs()
    assert_key(expect2, "retained", retained2)
    assert_key(expect2, "frame_retained_after_failed_send", bool(retained2))
    resent = [e for (e, _) in mem2.replay_from(retained2[0] - 1)]
    assert resent == retained2
    assert_key(expect2, "resent_on_next_tick", resent)
    # A failed send is a delay, not a hole: every appended epoch is resent.
    assert_key(
        expect2,
        "permanent_gap",
        [e for (e, _) in appended2 if e not in resent] != [],
    )


def test_outbox_store_protocol(tmp_path: Path) -> None:
    fixture = _load_fixture("outbox_store_protocol.json")
    ordered = _scenario(fixture, "unordered_puts_replay_in_epoch_order")
    store = InMemoryStore()
    for epoch in ordered["put_epochs"]:
        store.put(epoch, str(epoch).encode())
    assert_key(
        ordered["expect"],
        "epochs",
        [e for e, _ in store.scan_after(ordered["scan_after"])],
    )

    monotone = _scenario(fixture, "ack_cursor_is_monotone_and_prune_safe")
    outbox = Outbox(InMemoryStore())
    for epoch in monotone["put_epochs"]:
        outbox.append(epoch, IpcMessage.of_delta(Delta.new(epoch - 1, epoch, [])))
    for epoch in monotone["ack_through"]:
        outbox.ack_through(epoch)
    monotone_expect = monotone["expect"]
    assert_key(monotone_expect, "cursor", outbox.acked_through)
    assert_key(monotone_expect, "retained", outbox.retained_epochs())
    assert_key(
        monotone_expect, "replay_from_zero", [e for e, _ in outbox.replay_from(0)]
    )

    restart = _scenario(fixture, "restart_reloads_cursor_and_unacked_suffix")
    path = tmp_path / "protocol.sqlite3"
    first = SqliteOutbox(path, "doc")
    for epoch in restart["put_epochs"]:
        first.append(epoch, IpcMessage.of_delta(Delta.new(epoch - 1, epoch, [])))
    for epoch in restart["ack_through"]:
        first.ack_through(epoch)
    first.close()

    reopened = SqliteOutbox(path, "doc")
    restart_expect = restart["expect"]
    assert_key(restart_expect, "loaded_cursor", reopened.acked_through)
    assert_key(restart_expect, "retained", reopened.retained_epochs())
    assert_key(restart_expect, "replay", [e for e, _ in reopened.replay_from(0)])
    reopened.close()


def test_sqlite_cursor_update_is_serialized_monotone(tmp_path: Path) -> None:
    """A stale writer cannot overwrite a newer cursor persisted by another handle."""
    fixture = _load_fixture("outbox_store_protocol.json")
    scenario = _scenario(fixture, "stale_handle_cannot_regress_cursor")
    path = tmp_path / "cursor.sqlite3"
    handles = {
        "stale": Outbox(SqliteStore(path, "doc")),
        "current": Outbox(SqliteStore(path, "doc")),
    }
    for save in scenario["save_cursor"]:
        handles[save["handle"]].ack_through(save["epoch"])
    stale_cursor = handles["stale"].acked_through
    assert_key(scenario["expect"], "loaded_cursor", stale_cursor)
    for outbox in handles.values():
        outbox.store.close()

    reopened = SqliteStore(path, "doc")
    assert reopened.load_cursor() == stale_cursor
    reopened.close()


# ---------------------------------------------------------------------------
# liveness_orset_lww.json
# ---------------------------------------------------------------------------


def _stamp(o: dict) -> WireStamp:
    return WireStamp(wall_time=o["wall_time"], logical=o["logical"], peer=o["peer"])


def _replay_orset(ops: list[dict]) -> OrSet:
    st = OrSet()
    for op in ops:
        if op["op"] == "add":
            st.add(op["tag"])
        elif op["op"] == "remove":
            st.remove_observed(op["observed_tags"])
        else:
            # No final arm meant an unimplemented op name left the OrSet
            # untouched and the convergence expectations still passed
            # (#lzscenariobodyskip).
            raise AssertionError(f"unknown orset op {op['op']!r}")
    return st


def _live_docs(
    open_set: dict[str, OrSet], alive: dict[int, WireLwwRegister[bool]]
) -> list[str]:
    """Docs whose open entry is present AND whose owning pid is still alive."""
    live: set[str] = set()
    for key, entry in open_set.items():
        doc, pid = key.split("/")
        owner = int(pid.replace("pid", ""))
        register = alive.get(owner)
        if entry.present() and register is not None and register.value is True:
            live.add(doc)
    return sorted(live)


def test_liveness_orset_lww() -> None:
    fx = _load_fixture("liveness_orset_lww.json")

    add = _scenario(fx, "open_set_add_wins_over_stale_remove")
    add_expect = add["expect"]
    st = _replay_orset(add["ops"])
    assert_key(add_expect, "present", st.present())
    # An OrSet is a semilattice: the same ops in reverse order converge, and a
    # re-delivered op adds nothing.
    reversed_st = _replay_orset(list(reversed(add["ops"])))
    assert_key(add_expect, "order_independent", reversed_st.present() == st.present())
    redelivered = _replay_orset([*add["ops"], *add["ops"]])
    assert redelivered.present() == st.present()
    # Count the redeliveries that MOVED the set, rather than checking the
    # fixture's count against the literal 0 (#lzconsumednotasserted): OrSet
    # equality compares both tag sets, so a redelivery that added a tag shows up
    # even when presence did not flip.
    assert_key(add_expect, "redeliver_applied_count", int(redelivered != st))

    lww = _scenario(fx, "lww_alive_highest_stamp_wins")
    lww_expect = lww["expect"]

    def _fold_lww(ops: list[dict]) -> WireLwwRegister[bool]:
        reg: WireLwwRegister[bool] = WireLwwRegister(
            _stamp(ops[0]["stamp"]), ops[0]["value"]
        )
        for op in ops[1:]:
            reg.set(_stamp(op["stamp"]), op["value"])
        return reg

    reg = _fold_lww(lww["ops"])
    assert_key(lww_expect, "value", reg.value)

    def _resolution(want: str) -> None:
        # The fixture's spelling selects the rule to enforce, fail-closed. A bare
        # `== "max_stamp"` asserted only that the fixture equalled itself.
        assert want == "max_stamp", f"unimplemented resolution rule {want!r}"
        winner = max(lww["ops"], key=lambda op: op["stamp"]["wall_time"])
        assert reg.value == winner["value"], (
            "the highest stamp wins, not the last write"
        )

    assert_key_with(lww_expect, "resolution", _resolution)
    assert_key(
        lww_expect,
        "order_independent",
        _fold_lww(list(reversed(lww["ops"]))).value == reg.value,
    )

    death = _scenario(fx, "whole_editor_death_cascades")
    death_expect = death["expect"]
    open_set: dict[str, OrSet] = {}
    for entry in death["open_set"]:
        entry_set = OrSet()
        if entry["present"]:
            entry_set.add(entry["key"])
        open_set[entry["key"]] = entry_set
    alive: dict[int, WireLwwRegister[bool]] = {
        int(pid_str): WireLwwRegister(WireStamp(1, 0, 1), value)
        for pid_str, value in death["alive_before"].items()
    }
    live_before = _live_docs(open_set, alive)
    assert_key_with(
        death_expect, "live_docs_before", lambda want: live_before == sorted(want)
    )

    op = death["op"]
    pid = int(op["key"].replace("alive/pid", ""))
    alive[pid].set(_stamp(op["stamp"]), op["value"])
    live_after = _live_docs(open_set, alive)
    assert_key_with(
        death_expect, "live_docs_after", lambda want: live_after == sorted(want)
    )
    # One `alive` write drops every doc that pid held open — that is the cascade.
    assert_key(death_expect, "cascade", len(live_after) < len(live_before))


def test_liveness_derived_aggregate_converges_under_retry() -> None:
    """The derived per-doc live aggregate is order- and redelivery-independent.

    This scenario was in the fixture and unreplayed: the runner stopped at three
    of the four scenarios, so `converged_live_docs`, `per_doc_isolation` and the
    retry count asserted nothing at all (#lzassertunknownkeys).
    """
    fx = _load_fixture("liveness_orset_lww.json")
    sc = _scenario(fx, "derived_live_doc_aggregate_converges_under_retry")
    expect = sc["expect"]

    def replay(ops: list[dict]) -> tuple[dict[str, OrSet], dict[int, WireLwwRegister]]:
        open_set: dict[str, OrSet] = {}
        alive: dict[int, WireLwwRegister[bool]] = {}
        for op in ops:
            if op["register_kind"] == "orset":
                open_set.setdefault(op["key"], OrSet()).add(op["tag"])
            elif op["register_kind"] == "lww":
                pid = int(op["key"].replace("alive/pid", ""))
                stamp = _stamp(op["stamp"])
                if pid in alive:
                    alive[pid].set(stamp, op["value"])
                else:
                    alive[pid] = WireLwwRegister(stamp, op["value"])
            else:
                # The `lww` arm used to be a bare `else`, so any register kind
                # the corpus grows would have been replayed as an LWW register
                # and reported green (#lzscenariobodyskip).
                raise AssertionError(
                    f"unknown liveness register_kind {op['register_kind']!r}"
                )
        return open_set, alive

    r1_open, r1_alive = replay(sc["ops"])
    live = _live_docs(r1_open, r1_alive)
    assert_key_with(expect, "converged_live_docs", lambda want: live == sorted(want))

    assert sc["reverse_order_equivalent"]
    r2_open, r2_alive = replay(list(reversed(sc["ops"])))
    assert_key(expect, "order_independent", _live_docs(r2_open, r2_alive) == live)

    assert sc["redeliver"]
    retry_open, retry_alive = replay([*sc["ops"], *sc["ops"]])
    assert _live_docs(retry_open, retry_alive) == live
    applied_again = sum(
        1
        for key, entry in retry_open.items()
        if entry.present() != r1_open[key].present()
    )
    assert_key(expect, "redeliver_applied_count", applied_again)

    # Per-doc isolation: each doc's open entry is its own OrSet, so one doc's
    # ops never move another's.
    docs = {key.split("/")[0] for key in r1_open}
    assert_key(expect, "per_doc_isolation", len(docs) == len(r1_open))


# ---------------------------------------------------------------------------
# ResyncCoordinator unit tests (mirror lazily-rs)
# ---------------------------------------------------------------------------


def test_coordinator_applies_contiguous_and_advances() -> None:
    c = ResyncCoordinator.with_epoch(40)
    assert c.ingest_delta(Delta.new(40, 41, [])).is_apply
    assert c.last_epoch == 41
    assert c.ingest_delta(Delta.new(41, 44, [])).is_apply
    assert c.last_epoch == 44


def test_coordinator_ignores_empty_backward_delta() -> None:
    c = ResyncCoordinator.with_epoch(40)
    assert c.ingest_delta(Delta.new(40, 40, [])).is_ignore
    assert c.last_epoch == 40


def test_coordinator_gap_requests_once_then_ignores() -> None:
    c = ResyncCoordinator.with_epoch(2)
    res = c.ingest_delta(Delta.new(3, 4, []))
    assert res.is_request_snapshot and res.from_epoch == 2
    assert c.is_resyncing
    assert c.ingest_delta(Delta.new(4, 5, [])).is_ignore
    assert c.ingest_snapshot(5).is_apply
    assert not c.is_resyncing
    assert c.last_epoch == 5


def test_ack_carries_last_epoch() -> None:
    c = ResyncCoordinator.with_epoch(7)
    assert c.ack() == IpcMessage.of_outbox_ack(OutboxAck(through_epoch=7))


def test_outbox_retains_unacked_and_replays_from_cursor() -> None:
    o = InMemoryOutbox()
    for e in range(41, 44):
        o.append(e, IpcMessage.of_delta(Delta.new(e - 1, e, [])))
    o.ack_through(41)
    assert o.retained_epochs() == [42, 43]
    assert [e for (e, _) in o.replay_from(41)] == [42, 43]


def test_orset_join_is_commutative_and_add_wins() -> None:
    a = OrSet()
    a.add("t1")
    b = OrSet()
    b.remove_observed(["t1"])
    b.add("t3")
    ab = OrSet()
    ab.join(a)
    ab.join(b)
    ba = OrSet()
    ba.join(b)
    ba.join(a)
    assert ab == ba, "join is commutative"
    assert ab.present(), "add tag t3 not shadowed → present"


def test_lww_join_keeps_higher_stamp() -> None:
    a: WireLwwRegister[bool] = WireLwwRegister(WireStamp(10, 0, 1), True)
    b: WireLwwRegister[bool] = WireLwwRegister(WireStamp(20, 0, 1), False)
    a.join(b)
    assert a.value is False
    a.join(WireLwwRegister(WireStamp(5, 0, 1), True))
    assert a.value is False


# ---------------------------------------------------------------------------
# SyncDriver: loop-shape mechanism over a scripted transport (mirror lazily-js)
# ---------------------------------------------------------------------------


class Wire:
    def __init__(self) -> None:
        self.sent: list[IpcMessage] = []
        self.inbound: deque[IpcMessage] = deque()
        self.up = True
        self.source_err = False


class _Sink:
    def __init__(self, wire: Wire) -> None:
        self._wire = wire

    def send(self, message: IpcMessage) -> bool:
        if not self._wire.up:
            return False
        self._wire.sent.append(message)
        return True


class _Source:
    def __init__(self, wire: Wire) -> None:
        self._wire = wire

    def recv(self) -> IpcMessage | None:
        if self._wire.source_err:
            self._wire.source_err = False
            raise RuntimeError("scripted source read failure")
        return self._wire.inbound.popleft() if self._wire.inbound else None


class _SnapAhead:
    """Provider answering ``ResyncRequest{from}`` with a snapshot at ``from + 5``."""

    def snapshot(self, from_epoch: int) -> IpcMessage:
        return IpcMessage.of_snapshot(Snapshot(epoch=from_epoch + 5))


class _ZeroClock:
    def now_millis(self) -> int:
        return 0


def _driver_at(wire: Wire, last_epoch: int) -> SyncDriver:
    return SyncDriver(
        _Sink(wire),
        _Source(wire),
        InMemoryOutbox(),
        _ZeroClock(),
        _SnapAhead(),
        last_epoch,
    )


def _dframe(base: int, epoch: int) -> IpcMessage:
    return IpcMessage.of_delta(Delta.new(base, epoch, []))


def test_driver_drains_append_before_send_and_retains_until_acked() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    d.enqueue(1, _dframe(0, 1))
    d.enqueue(2, _dframe(1, 2))
    p = d.tick()
    assert isinstance(p, Progress)
    assert p.sent == 2, "both fresh frames pushed to the sink"
    assert len(wire.sent) == 2
    assert p.retained == 2, "appended-before-send, retained until acked"
    assert not d.is_stalled()

    wire.inbound.append(IpcMessage.of_outbox_ack(OutboxAck(through_epoch=2)))
    p = d.tick()
    assert p.peer_acked_through == 2
    assert p.retained == 0, "acked frames pruned"


def test_driver_retains_on_send_failure_and_replays_on_reconnect() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.up = False
    d.enqueue(1, _dframe(0, 1))
    p = d.tick()
    assert p.sent == 0
    assert d.is_stalled(), "a failed send stalls the driver"
    assert p.retained == 1, "frame retained in the outbox despite the failure"
    assert wire.sent == []
    assert d.stalled_for(250) == 250, "stall duration is a host backoff signal"

    wire.up = True
    d.on_reconnect()
    p = d.tick()
    assert not d.is_stalled()
    assert p.sent == 1, "the retained frame is replayed"
    assert any(
        m.is_delta and m.delta is not None and m.delta.epoch == 1 for m in wire.sent
    ), "the replayed delta reached the sink"


def test_driver_applies_delta_and_advertises_receiver_cursor() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.inbound.append(_dframe(0, 1))
    p = d.tick()
    assert len(p.applied) == 1, "the applied frame is handed to the host"
    assert d.last_epoch() == 1
    assert any(
        m.is_outbox_ack and m.outbox_ack is not None and m.outbox_ack.through_epoch == 1
        for m in wire.sent
    ), "an OutboxAck advertising the new cursor was sent"


def test_driver_redelivery_is_idempotent_no_op() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.inbound.append(_dframe(0, 1))
    assert len(d.tick().applied) == 1
    wire.inbound.append(_dframe(0, 1))
    p = d.tick()
    assert len(p.applied) == 0, "already-applied re-delivery is ignored"
    assert d.last_epoch() == 1, "cursor does not double-advance"


def test_driver_requests_snapshot_on_inbound_gap() -> None:
    wire = Wire()
    d = _driver_at(wire, 2)
    wire.inbound.append(_dframe(3, 4))
    p = d.tick()
    assert p.resync_requested
    assert p.applied == [], "the gapped delta is not applied"
    assert any(
        m.is_resync_request
        and m.resync_request is not None
        and m.resync_request.from_epoch == 2
        for m in wire.sent
    ), "a ResyncRequest at the current cursor was emitted"


def test_driver_answers_resync_request_with_provider_snapshot() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.inbound.append(IpcMessage.of_resync_request(ResyncRequest(from_epoch=2)))
    p = d.tick()
    assert p.snapshots_served == 1
    assert any(
        m.is_snapshot and m.snapshot is not None and m.snapshot.epoch == 7
        for m in wire.sent
    ), "a covering snapshot (from_epoch + 5) was sent"


def test_driver_surfaces_source_read_error() -> None:
    wire = Wire()
    d = _driver_at(wire, 0)
    wire.source_err = True
    try:
        d.tick()
    except DriverError as e:
        assert e.kind == "Source"
    else:
        raise AssertionError("expected a DriverError(Source)")


def test_driver_gap_then_snapshot_converges() -> None:
    wire = Wire()
    d = _driver_at(wire, 2)
    wire.inbound.append(_dframe(4, 5))
    d.tick()
    assert d.last_epoch() == 2, "still stuck at the pre-gap cursor"
    wire.inbound.append(IpcMessage.of_snapshot(Snapshot(epoch=5)))
    p = d.tick()
    assert len(p.applied) == 1
    assert d.last_epoch() == 5, "snapshot restored convergence"
