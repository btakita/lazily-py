"""WebSocket signaling protocol — ``signaling``.

The Python counterpart of ``lazily-spec/protocol.md`` § "Signaling Protocol
(WebSocket)". WebSocket signaling frames for lazily peer discovery: one frame =
one JSON object tagged ``type``. Client → server directed frames carry ``to``;
server → client forwarded frames carry a server-stamped ``from`` (never
client-supplied) so a peer cannot impersonate another. Tags are kebab-case;
``peer`` ids are bare JSON numbers ``<= 2**53 - 1``.

This module ships the typed frame envelope (:class:`SignalingFrame`) and a
concrete room state machine (:class:`RoomCore`) that implements the anti-spoof
routing invariant — the load-bearing property pinned by the
``conformance/signaling/anti_spoof_session.json`` fixture.

A concrete WebRTC backend is a platform adapter (the spec explicitly names this
as optional behind a transport seam); this binding ships the portable signaling
stack and an in-process loopback transport so the full signaling plane is
conformance-tested without a native WebRTC dependency.
"""

from __future__ import annotations


__all__ = [
    "PermissionMode",
    "RoomCore",
    "RoomError",
    "SignalingFrame",
    "SignalingFrameKind",
]


from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Frame envelope
# ---------------------------------------------------------------------------


class SignalingFrameKind(Enum):
    """The ``type`` discriminant on a signaling frame.

    Client → server: ``join``, ``offer``, ``answer``, ``ice``, ``relay``,
    ``leave``. Server → client: ``welcome``, ``peer-joined``, ``peer-left``,
    ``offer``, ``answer``, ``ice``, ``relay``, ``error``.
    """

    JOIN = "join"
    WELCOME = "welcome"
    PEER_JOINED = "peer-joined"
    PEER_LEFT = "peer-left"
    OFFER = "offer"
    ANSWER = "answer"
    ICE = "ice"
    RELAY = "relay"
    LEAVE = "leave"
    ERROR = "error"


@dataclass
class SignalingFrame:
    """One signaling frame — a tagged JSON object.

    Client-directed frames carry ``to``; server-forwarded frames carry
    ``from``. The two are never both present on one frame.
    """

    type: str
    peer: int | None = None
    to: int | None = None
    frm: int | None = None
    sdp: str | None = None
    candidate: str | None = None
    payload: Any = None
    capabilities: list[str] | None = None
    peers: list[int] | None = None
    code: str | None = None
    message: str | None = None

    @staticmethod
    def from_wire(d: dict[str, Any], *, direction: str | None = None) -> SignalingFrame:
        """Decode a wire JSON object into a typed frame."""
        t = d["type"]
        try:
            SignalingFrameKind(t)
        except ValueError as exc:
            raise ValueError(f"unknown signaling frame type: {t!r}") from exc
        if "to" in d and "from" in d:
            raise ValueError("a signaling frame cannot carry both `to` and `from`")
        if direction == "client" and "from" in d:
            raise ValueError("a client signaling frame cannot carry `from`")
        if direction == "server" and "to" in d:
            raise ValueError("a server signaling frame cannot carry `to`")
        if t == "welcome" and d.get("peer") in d.get("peers", []):
            raise ValueError("a welcome roster cannot contain the joining peer")
        return SignalingFrame(
            type=t,
            peer=d.get("peer"),
            to=d.get("to"),
            frm=d.get("from"),
            sdp=d.get("sdp"),
            candidate=d.get("candidate"),
            payload=d.get("payload"),
            capabilities=d.get("capabilities"),
            peers=d.get("peers"),
            code=d.get("code"),
            message=d.get("message"),
        )

    def to_wire(self) -> dict[str, Any]:
        """Encode to the canonical JSON object (omits ``None`` fields)."""
        out: dict[str, Any] = {"type": self.type}
        if self.peer is not None:
            out["peer"] = self.peer
        if self.to is not None:
            out["to"] = self.to
        if self.frm is not None:
            out["from"] = self.frm
        if self.sdp is not None:
            out["sdp"] = self.sdp
        if self.candidate is not None:
            out["candidate"] = self.candidate
        if self.payload is not None:
            out["payload"] = self.payload
        if self.capabilities is not None:
            out["capabilities"] = list(self.capabilities)
        if self.peers is not None:
            out["peers"] = list(self.peers)
        if self.code is not None:
            out["code"] = self.code
        if self.message is not None:
            out["message"] = self.message
        return out


# ---------------------------------------------------------------------------
# Room state machine (anti-spoof routing)
# ---------------------------------------------------------------------------


class PermissionMode(Enum):
    """Room permission mode (``protocol.md § Permission modes``)."""

    OPEN = "open"
    ALLOWLIST = "allowlist"


class RoomError(Exception):
    """Raised when a room rejects a directed frame (unknown target / denied)."""


@dataclass
class _PeerConn:
    """A peer's connection identity: ``conn`` id → registered ``peer`` id."""

    peer: int
    capabilities: list[str] = field(default_factory=list)


class RoomCore:
    """The room routing state machine.

    Replays a sequence of ``(conn, SignalingFrame)`` inputs and produces the
    exact frame transcript the anti-spoof invariant requires: a directed frame's
    ``from`` is the SENDER's server-registered peer id, never client-supplied;
    the ``welcome`` roster excludes the joining peer's own id; ``to``/``from``
    are never both present on one frame.
    """

    __slots__ = ("_by_conn", "_by_peer", "_grants", "mode")

    def __init__(self, mode: PermissionMode = PermissionMode.OPEN) -> None:
        self.mode = mode
        self._by_conn: dict[str, _PeerConn] = {}
        self._by_peer: dict[int, str] = {}
        self._grants: dict[tuple[int, int], bool] = {}

    # -- routing -------------------------------------------------------- #

    def handle(
        self, conn: str, frame: SignalingFrame
    ) -> list[tuple[str, SignalingFrame]]:
        """Apply one inbound ``frame`` from ``conn``; return the emit list.

        Each emit is ``(target_conn, SignalingFrame)``. The anti-spoof
        invariant: a forwarded directed frame carries ``from`` = the sender's
        server-registered peer id (never a client-supplied ``from``); the
        inbound directed frame's client-supplied ``from`` is ignored.
        """
        kind = frame.type
        if kind == SignalingFrameKind.JOIN.value:
            return self._on_join(conn, frame)
        if kind == SignalingFrameKind.LEAVE.value:
            return self._on_leave(conn)
        if kind in (
            SignalingFrameKind.OFFER.value,
            SignalingFrameKind.ANSWER.value,
            SignalingFrameKind.ICE.value,
            SignalingFrameKind.RELAY.value,
        ):
            return self._on_directed(conn, frame)
        # Unknown frame type: reject.
        return [
            (
                conn,
                SignalingFrame(
                    type=SignalingFrameKind.ERROR.value,
                    code="unknown_frame",
                    message=f"unknown frame type {kind!r}",
                ),
            )
        ]

    def _on_join(
        self, conn: str, frame: SignalingFrame
    ) -> list[tuple[str, SignalingFrame]]:
        peer = frame.peer
        if peer is None:
            return [
                (
                    conn,
                    SignalingFrame(
                        type=SignalingFrameKind.ERROR.value,
                        code="malformed_join",
                        message="join requires a peer id",
                    ),
                )
            ]
        # Register the connection. A re-join on the same conn refreshes caps.
        existing = self._by_conn.get(conn)
        if existing is not None and existing.peer != peer:
            # The conn was previously registered to a different peer: detach.
            self._by_peer.pop(existing.peer, None)
        caps = list(frame.capabilities) if frame.capabilities is not None else []
        self._by_conn[conn] = _PeerConn(peer=peer, capabilities=caps)
        self._by_peer[peer] = conn

        # welcome: the roster excludes the joining peer's own id, sorted asc.
        roster = sorted(p for p in self._by_peer if p != peer)
        emits: list[tuple[str, SignalingFrame]] = [
            (
                conn,
                SignalingFrame(
                    type=SignalingFrameKind.WELCOME.value,
                    peer=peer,
                    peers=roster,
                ),
            )
        ]
        # Announce the join to every other peer.
        for other_conn, _pc in self._by_conn.items():
            if other_conn == conn:
                continue
            emits.append(
                (
                    other_conn,
                    SignalingFrame(
                        type=SignalingFrameKind.PEER_JOINED.value, peer=peer
                    ),
                )
            )
        return emits

    def _on_leave(self, conn: str) -> list[tuple[str, SignalingFrame]]:
        pc = self._by_conn.pop(conn, None)
        if pc is None:
            return []
        self._by_peer.pop(pc.peer, None)
        emits: list[tuple[str, SignalingFrame]] = []
        for other_conn in self._by_conn:
            if other_conn == conn:
                continue
            emits.append(
                (
                    other_conn,
                    SignalingFrame(
                        type=SignalingFrameKind.PEER_LEFT.value, peer=pc.peer
                    ),
                )
            )
        return emits

    def _on_directed(
        self, conn: str, frame: SignalingFrame
    ) -> list[tuple[str, SignalingFrame]]:
        sender = self._by_conn.get(conn)
        if sender is None:
            return [
                (
                    conn,
                    SignalingFrame(
                        type=SignalingFrameKind.ERROR.value,
                        code="not_joined",
                        message="connection has not joined the session",
                    ),
                )
            ]
        target_peer = frame.to
        if target_peer is None:
            return [
                (
                    conn,
                    SignalingFrame(
                        type=SignalingFrameKind.ERROR.value,
                        code="missing_target",
                        message="directed frame requires a `to` peer id",
                    ),
                )
            ]
        target_conn = self._by_peer.get(target_peer)
        if target_conn is None or target_conn == conn:
            return [
                (
                    conn,
                    SignalingFrame(
                        type=SignalingFrameKind.ERROR.value,
                        code="unknown_target",
                        message=f"peer {target_peer} is not in this session",
                    ),
                )
            ]
        # Allowlist mode: require an explicit grant for the directed target.
        if self.mode is PermissionMode.ALLOWLIST and not self._is_allowed(
            sender.peer, target_peer
        ):
            return [
                (
                    conn,
                    SignalingFrame(
                        type=SignalingFrameKind.ERROR.value,
                        code="permission_denied",
                        message=(
                            f"peer {sender.peer} is not allowed to signal "
                            f"peer {target_peer}"
                        ),
                    ),
                )
            ]
        # Forward with a server-stamped ``from`` (anti-spoof). The client's
        # own ``from`` (if any) is ignored.
        forwarded = SignalingFrame(
            type=frame.type,
            frm=sender.peer,
            sdp=frame.sdp,
            candidate=frame.candidate,
            payload=frame.payload,
        )
        return [(target_conn, forwarded)]

    # -- allowlist grants ----------------------------------------------- #

    def _is_allowed(self, sender: int, target: int) -> bool:
        return self._grants.get((sender, target), False)

    def allow(self, sender: int, target: int) -> None:
        """Grant ``sender`` permission to direct frames at ``target``
        (allowlist mode only)."""
        self._grants[(sender, target)] = True

    # -- introspection -------------------------------------------------- #

    def roster(self) -> list[int]:
        """The currently-joined peer ids, sorted ascending."""
        return sorted(self._by_peer)
