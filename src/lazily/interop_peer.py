"""NDJSON peer adapter for the cross-binding Lazily interoperability suite.

This is test infrastructure, not a production daemon.  The adapter deliberately
keeps no CRDT implementation of its own: operations, wire values, frames, and
merge decisions all pass through :mod:`lazily.ipc` and
:class:`lazily.crdt_plane.CrdtPlaneRuntime`.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .crdt_plane import CrdtPlaneRuntime
from .ipc import CrdtOp, CrdtSync, IpcMessage


PROTOCOL_VERSION = 1


class InteropPeer:
    """State for one orchestrator-assigned peer process."""

    def __init__(self) -> None:
        self._peer_id: int | None = None
        self._logical = 0
        self._runtime: CrdtPlaneRuntime | None = None

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle one control request and return one schema-shaped response."""
        command = request.get("cmd")
        if command == "hello":
            return self._hello(request)
        if command == "local_set":
            return self._local_set(request)
        if command == "deliver":
            return self._deliver(request)
        if command == "snapshot":
            return self._snapshot()
        if command == "bye":
            return {"ok": True}
        if isinstance(command, str) and command.startswith("link_"):
            return {
                "ok": False,
                "error": "unsupported channel",
                "unsupported": True,
            }
        return {"ok": False, "error": "unknown command"}

    def _hello(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("protocol_version") != PROTOCOL_VERSION:
            return {"ok": False, "error": "unsupported protocol_version"}
        peer_id = request.get("peer")
        if not isinstance(peer_id, int):
            return {"ok": False, "error": "hello requires integer peer"}
        self._peer_id = peer_id
        self._logical = 0
        self._runtime = CrdtPlaneRuntime(peer_id)
        return {
            "ok": True,
            "binding": "lazily-py",
            "version": "0.37.1",
            "protocol_version": PROTOCOL_VERSION,
            "features": ["distributed_crdt"],
            "codecs": ["json"],
            "channels": [],
            "channel_variants": {},
            "platform_profile": "portable",
            "carve_outs": ["msgpack", "transport_links"],
        }

    def _local_set(self, request: dict[str, Any]) -> dict[str, Any]:
        runtime, peer_id = self._ready()
        node = request.get("node")
        at = request.get("at")
        key = request.get("key")
        state = request.get("state")
        if not isinstance(node, int) or not isinstance(at, int):
            raise ValueError("local_set requires integer node and at")
        if key is not None and not isinstance(key, str):
            raise ValueError("local_set key must be a string or null")
        self._logical += 1
        op = CrdtOp.from_wire(
            {
                "node": node,
                "key": None if key is None else {"path": key},
                "stamp": {
                    "wall_time": at,
                    "logical": self._logical,
                    "peer": peer_id,
                },
                "state": state,
            }
        )
        if not runtime.apply(op):
            raise ValueError("production runtime rejected fresh local op")
        message = IpcMessage.of_crdt_sync(CrdtSync.new(runtime.frontier(), [op]))
        # Exercise the production JSON encoder before returning the frame.
        frame = json.loads(message.encode_json())
        return {"ok": True, "frame": frame}

    def _deliver(self, request: dict[str, Any]) -> dict[str, Any]:
        runtime, _ = self._ready()
        # Exercise the production JSON decoder on cross-language input.
        message = IpcMessage.decode_json(
            json.dumps(request.get("frame"), separators=(",", ":"))
        )
        if message.crdt_sync is None:
            raise ValueError("deliver requires CrdtSync")
        return {"ok": True, "applied": runtime.apply_frame(message.crdt_sync)}

    def _snapshot(self) -> dict[str, Any]:
        runtime, _ = self._ready()
        cells = [
            {
                "node": entry.node,
                "key": entry.key.path if entry.key is not None else None,
                "state": {"Inline": list(entry.state)},
            }
            for entry in runtime.converged()
        ]
        cells.sort(key=lambda cell: (cell["node"], cell["key"] or ""))
        return {"ok": True, "cells": cells}

    def _ready(self) -> tuple[CrdtPlaneRuntime, int]:
        if self._runtime is None or self._peer_id is None:
            raise ValueError("hello must run first")
        return self._runtime, self._peer_id


def self_check() -> None:
    """Run a minimal local transcript through production CRDT/IPC surfaces."""
    peer = InteropPeer()
    assert peer.handle(
        {"cmd": "hello", "peer": 1, "protocol_version": PROTOCOL_VERSION}
    )["ok"]
    local = peer.handle(
        {
            "cmd": "local_set",
            "node": 7,
            "key": None,
            "state": {"Inline": [65]},
            "at": 10,
        }
    )
    assert local["frame"]["CrdtSync"]["ops"][0]["key"] is None
    assert (
        peer.handle({"cmd": "deliver", "frame": local["frame"], "at": 11})["applied"]
        == 0
    )
    assert peer.handle({"cmd": "snapshot"})["cells"][0]["state"] == {"Inline": [65]}


def main() -> int:
    if "--self-check" in sys.argv[1:]:
        self_check()
        print("lazily-py interop peer self-check: ok", file=sys.stderr)
        return 0

    peer = InteropPeer()
    for line in sys.stdin:
        request: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("control request must be an object")
            response = peer.handle(request)
        except Exception as error:  # Keep the NDJSON protocol alive for diagnosis.
            response = {"ok": False, "error": str(error)}
        print(json.dumps(response, separators=(",", ":")), flush=True)
        if isinstance(request, dict) and request.get("cmd") == "bye":
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
