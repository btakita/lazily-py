"""Conformance tests for the signaling plane.

Replays ``conformance/signaling/anti_spoof_session.json`` through
:class:`lazily.signaling.RoomCore` and asserts the exact frame transcript the
anti-spoof invariant requires: a directed frame's ``from`` is the sender's
server-registered peer id (never client-supplied), the ``welcome`` roster
excludes the joining peer, and ``to``/``from`` are never both present.

Also round-trips every frame in ``conformance/signaling/frames.json``: each
frame's canonical JSON survives :meth:`SignalingFrame.to_wire` /
:meth:`SignalingFrame.from_wire` unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance_assert import assert_key, corpus_fixture, instrument

from lazily.signaling import (
    PermissionMode,
    RoomCore,
    SignalingFrame,
)


_LOCAL = Path(__file__).resolve().parent / "conformance"


def _load(rel: str) -> dict:
    path = corpus_fixture(rel, _LOCAL / rel)
    return instrument(json.loads(path.read_text()), name=rel)


def test_signaling_frames_round_trip() -> None:
    fix = _load("signaling/frames.json")
    assert fix["kind"] == "SignalingFrames"
    for frame in fix["frames"]:
        wire = frame["wire"]
        label = frame["label"]
        decoded = SignalingFrame.from_wire(wire, direction=frame["direction"])
        assert decoded.to_wire() == wire, f"round-trip mismatch: {label}"
        # Client-directed frames carry `to`; server-forwarded carry `from`.
        if frame["direction"] == "client":
            for variant in ("offer", "answer", "ice", "relay"):
                if frame["variant"] == variant:
                    assert decoded.to is not None
                    assert decoded.frm is None
        elif frame["direction"] == "server":
            for variant in ("offer", "answer", "ice", "relay"):
                if frame["variant"] == variant:
                    assert decoded.frm is not None
                    assert decoded.to is None
        else:
            # `server` used to be the unnamed `else`, so a frame carrying any
            # other direction was checked against the server-forwarded rule
            # (#lzscenariobodyskip).
            raise AssertionError(
                f"{label}: unknown frame direction {frame['direction']!r}"
            )
        # to/from never both present.
        assert not (decoded.to is not None and decoded.frm is not None)

        # Per-frame field assertions. Decoding a frame is not testing it: every
        # key the fixture names has to reach a real attribute of the decoded
        # frame (#lzassertunknownkeys).
        a = frame["assertions"]
        observed = {
            "peer": decoded.peer,
            "to": decoded.to,
            "from": decoded.frm,
            "code": decoded.code,
            "peers": decoded.peers,
            "capabilities": decoded.capabilities,
            "has_capabilities": decoded.capabilities is not None,
            "roster_excludes_self": decoded.peer not in (decoded.peers or []),
            # A server-forwarded frame carries `from` and never `to`; the value
            # is the sender's registered peer id, which the session fixture
            # below proves end-to-end.
            "server_stamped_from": (
                frame["direction"] == "server"
                and decoded.frm is not None
                and decoded.to is None
            ),
        }
        for key, got in observed.items():
            if key in a:
                assert_key(a, key, got, where=label)


def test_signaling_anti_spoof_session() -> None:
    fix = _load("signaling/anti_spoof_session.json")
    assert fix["kind"] == "SignalingSession"
    a = fix["assertions"]
    room = RoomCore(mode=PermissionMode(fix["mode"]))

    # The three session-wide invariants the fixture names, each accumulated over
    # the transcript and asserted against the fixture's own claim below.
    registered: dict[str, int] = {}
    rosters_exclude_self = True
    rosters_sorted = True
    forwarded_from_is_registered = True
    saw_welcome = False
    saw_forward = False

    for step in fix["steps"]:
        inp = step["input"]
        frame = SignalingFrame.from_wire(inp["recv"], direction="client")
        if frame.type == "join":
            registered[inp["conn"]] = frame.peer  # type: ignore[assignment]
        emits = room.handle(inp["conn"], frame)
        assert len(emits) == len(step["expect"]), (
            f"step {inp}: emit count {len(emits)} != {len(step['expect'])}"
        )
        for (target_conn, emitted), want in zip(emits, step["expect"], strict=True):
            assert target_conn == want["to"], f"step {inp}: target conn mismatch"
            wire = emitted.to_wire()
            assert wire == want["frame"], (
                f"step {inp}: emitted {wire} != {want['frame']}"
            )
            # Anti-spoof: forwarded frames carry a server-stamped `from`,
            # never a client-supplied value; `to`/`from` never both present.
            assert not (emitted.to is not None and emitted.frm is not None)
            if emitted.type == "welcome":
                saw_welcome = True
                roster = emitted.peers or []
                rosters_exclude_self &= emitted.peer not in roster
                rosters_sorted &= roster == sorted(roster)
            if emitted.frm is not None:
                saw_forward = True
                # The stamped `from` is the SENDER's registered peer id — the
                # sender being the conn that produced this step, not the target.
                forwarded_from_is_registered &= emitted.frm == registered[inp["conn"]]

    assert saw_welcome, "transcript proves nothing about rosters without a welcome"
    assert saw_forward, "transcript proves nothing about anti-spoof without a forward"
    assert_key(a, "roster_excludes_self", rosters_exclude_self)
    assert_key(a, "roster_sorted_ascending", rosters_sorted)
    assert_key(a, "forwarded_from_is_server_registered", forwarded_from_is_registered)

    for reject in fix["rejects"]:
        try:
            SignalingFrame.from_wire(reject["input"]["recv"], direction="client")
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted rejected frame: {reject['label']}")


def test_signaling_negative_frames_are_rejected() -> None:
    fix = _load("signaling/frames.json")
    for reject in fix["rejects"]:
        try:
            SignalingFrame.from_wire(
                reject["wire"],
                direction=reject["direction"],
            )
        except (KeyError, ValueError):
            pass
        else:
            raise AssertionError(f"accepted rejected frame: {reject['label']}")


def test_signaling_welcome_roster_excludes_self() -> None:
    room = RoomCore()
    # First join: empty roster.
    emits = room.handle("a", SignalingFrame.from_wire({"type": "join", "peer": 1}))
    welcome = emits[0][1]
    assert welcome.peers == []
    # Second join: roster has peer 1, excludes self (2); peer 1 is told about 2.
    emits = room.handle("b", SignalingFrame.from_wire({"type": "join", "peer": 2}))
    assert emits[0][0] == "b"
    assert emits[0][1].peers == [1]  # roster excludes self
    assert emits[1][0] == "a"
    assert emits[1][1].type == "peer-joined"
    assert emits[1][1].peer == 2


def test_signaling_unknown_target_errors() -> None:
    room = RoomCore()
    room.handle("a", SignalingFrame.from_wire({"type": "join", "peer": 1}))
    emits = room.handle(
        "a", SignalingFrame.from_wire({"type": "offer", "to": 99, "sdp": "x"})
    )
    assert len(emits) == 1
    err = emits[0][1]
    assert err.type == "error"
    assert err.code == "unknown_target"


def test_signaling_allowlist_denies_without_grant() -> None:
    room = RoomCore(mode=PermissionMode.ALLOWLIST)
    room.handle("a", SignalingFrame.from_wire({"type": "join", "peer": 1}))
    room.handle("b", SignalingFrame.from_wire({"type": "join", "peer": 2}))
    emits = room.handle(
        "a", SignalingFrame.from_wire({"type": "offer", "to": 2, "sdp": "x"})
    )
    assert emits[0][1].code == "permission_denied"
    # After granting, the same frame forwards.
    room.allow(1, 2)
    emits = room.handle(
        "a", SignalingFrame.from_wire({"type": "offer", "to": 2, "sdp": "x"})
    )
    assert emits[0][0] == "b"
    assert emits[0][1].type == "offer"
    assert emits[0][1].frm == 1


def test_signaling_leave_announces_peer_left() -> None:
    room = RoomCore()
    room.handle("a", SignalingFrame.from_wire({"type": "join", "peer": 1}))
    room.handle("b", SignalingFrame.from_wire({"type": "join", "peer": 2}))
    emits = room.handle("b", SignalingFrame.from_wire({"type": "leave"}))
    assert emits[0][0] == "a"
    assert emits[0][1].type == "peer-left"
    assert emits[0][1].peer == 2
