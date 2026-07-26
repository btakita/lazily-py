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

from lazily.signaling import (
    PermissionMode,
    RoomCore,
    SignalingFrame,
)


_LOCAL = Path(__file__).resolve().parent / "conformance"
_SPEC = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"


def _load(rel: str) -> dict:
    path = _SPEC / rel
    if not path.exists():
        path = _LOCAL / rel
    return json.loads(path.read_text())


def test_signaling_frames_round_trip() -> None:
    fix = _load("signaling/frames.json")
    assert fix["kind"] == "SignalingFrames"
    for frame in fix["frames"]:
        wire = frame["wire"]
        decoded = SignalingFrame.from_wire(wire, direction=frame["direction"])
        assert decoded.to_wire() == wire, f"round-trip mismatch: {frame['label']}"
        # Client-directed frames carry `to`; server-forwarded carry `from`.
        if frame["direction"] == "client":
            for variant in ("offer", "answer", "ice", "relay"):
                if frame["variant"] == variant:
                    assert decoded.to is not None
                    assert decoded.frm is None
        else:
            for variant in ("offer", "answer", "ice", "relay"):
                if frame["variant"] == variant:
                    assert decoded.frm is not None
                    assert decoded.to is None
        # to/from never both present.
        assert not (decoded.to is not None and decoded.frm is not None)


def test_signaling_anti_spoof_session() -> None:
    fix = _load("signaling/anti_spoof_session.json")
    assert fix["kind"] == "SignalingSession"
    room = RoomCore(mode=PermissionMode(fix["mode"]))

    for step in fix["steps"]:
        inp = step["input"]
        frame = SignalingFrame.from_wire(inp["recv"], direction="client")
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
