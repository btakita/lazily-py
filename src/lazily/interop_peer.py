"""NDJSON peer adapter for the cross-binding Lazily interoperability suite.

This is test infrastructure, not a production daemon.  The adapter deliberately
keeps no CRDT implementation of its own: operations, wire values, frames, and
merge decisions all pass through :mod:`lazily.ipc` and
:class:`lazily.crdt_plane.CrdtPlaneRuntime`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any

from .crdt_plane import CrdtPlaneRuntime
from .ipc import CrdtOp, CrdtSync, IpcMessage
from .stdlib import (
    RevisionBarrier,
    RevisionBarrierObservation,
    Timeout,
    TimeoutObservation,
    TimeoutOperation,
    Timer,
    TimerError,
    TimerObservation,
)


PROTOCOL_VERSION = 1


class InteropPeer:
    """State for one orchestrator-assigned peer process."""

    def __init__(self) -> None:
        self._peer_id: int | None = None
        self._logical = 0
        self._runtime: CrdtPlaneRuntime | None = None
        self._stdlib: dict[str, dict[str, Any]] = {}

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
        if command == "feature_reset":
            return self._feature_reset(request)
        if command == "feature_step":
            return self._feature_step(request)
        if command == "feature_observe":
            return self._feature_observe(request)
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
        self._stdlib = {}
        return {
            "ok": True,
            "binding": "lazily-py",
            "version": "0.37.1",
            "protocol_version": PROTOCOL_VERSION,
            "features": [
                "distributed_crdt",
                "stdlib_timer_v1",
                "stdlib_timeout_v1",
                "stdlib_revision_barrier_v1",
            ],
            # `msgpack` left the carve-out list when lazily-py grew the wire the
            # token actually names (#lzmsgpackseven): externally tagged envelope
            # over named-field maps, replayed against
            # conformance/codec/frame_roundtrip_msgpack.json. Declaring a codec
            # here is a promise a peer may act on, so it moves only when the
            # round-trip fixture proves it.
            "codecs": ["json", "msgpack"],
            "channels": [],
            "channel_variants": {},
            "platform_profile": "portable",
            "carve_outs": ["transport_links"],
        }

    @staticmethod
    def _supported_feature(feature: object) -> bool:
        return feature in {
            "stdlib_timer_v1",
            "stdlib_timeout_v1",
            "stdlib_revision_barrier_v1",
        }

    def _feature_reset(self, request: dict[str, Any]) -> dict[str, Any]:
        feature = request.get("feature")
        if not self._supported_feature(feature):
            return {
                "ok": False,
                "error": f"unsupported feature {feature}",
                "unsupported": True,
            }
        assert isinstance(feature, str)
        self._stdlib[feature] = {"last": None}
        return {"ok": True, "feature": feature}

    def _feature_step(self, request: dict[str, Any]) -> dict[str, Any]:
        feature = request.get("feature")
        state = self._stdlib.get(feature) if isinstance(feature, str) else None
        step = request.get("step")
        if state is None:
            raise ValueError("feature_reset must run first")
        if not isinstance(step, dict):
            raise ValueError("feature_step requires object step")
        if feature == "stdlib_timer_v1":
            observation = self._timer_step(state, step)
        elif feature == "stdlib_timeout_v1":
            observation = self._timeout_step(state, step)
        elif feature == "stdlib_revision_barrier_v1":
            observation = self._barrier_step(state, step)
        else:
            # The old `else` ran the revision-barrier arm for every feature
            # token that was not one of the first two, so a peer stepping a
            # feature this build does not implement got barrier observations
            # back under that feature's name and read as conforming.
            raise ValueError(f"unsupported feature: {feature!r}")
        state["last"] = observation
        return {"ok": True, "feature": feature, "observation": observation}

    def _feature_observe(self, request: dict[str, Any]) -> dict[str, Any]:
        feature = request.get("feature")
        state = self._stdlib.get(feature) if isinstance(feature, str) else None
        if state is None or state["last"] is None:
            raise ValueError("feature has no observation")
        return {"ok": True, "feature": feature, "observation": state["last"]}

    @staticmethod
    def _clean(
        value: TimerObservation | TimeoutObservation[Any] | RevisionBarrierObservation,
    ) -> dict[str, Any]:
        return {key: item for key, item in asdict(value).items() if item is not None}

    @staticmethod
    def _wire_u64(value: Any) -> Any:
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        return value

    def _timer_step(
        self, state: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any]:
        if step.get("op") == "start":
            try:
                timer = Timer(
                    self._wire_u64(step["now"]),
                    self._wire_u64(step["duration"]),
                )
            except TimerError as error:
                state["timer"] = None
                return {"outcome": "unavailable", "reason": error.reason}
            state["timer"] = timer
            return {"outcome": "pending", "deadline": timer.deadline}
        timer = state.get("timer")
        if not isinstance(timer, Timer):
            raise ValueError("timer start must succeed before observe")
        return self._clean(timer.observe(self._wire_u64(step["now"])))

    def _timeout_step(
        self, state: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any]:
        if step.get("op") == "start":
            try:
                timeout: Timeout[str] = Timeout(
                    self._wire_u64(step["now"]),
                    self._wire_u64(step["duration"]),
                )
            except TimerError as error:
                state["timeout"] = None
                return {"outcome": "unavailable", "reason": error.reason}
            state["timeout"] = timeout
            return {"outcome": "pending", "deadline": timeout.deadline}
        timeout = state.get("timeout")
        if not isinstance(timeout, Timeout):
            raise ValueError("timeout start must succeed before poll")
        operation_calls = 0
        cancellation_calls = 0

        def operation() -> TimeoutOperation[str]:
            nonlocal operation_calls
            operation_calls += 1
            outcome = step["operation"]
            if outcome == "completed":
                return TimeoutOperation.completed(step.get("value", ""))
            if outcome == "unavailable":
                return TimeoutOperation.unavailable()
            if outcome == "pending":
                return TimeoutOperation.pending()
            # `operation` is a three-value fixture discriminator. The fallthrough
            # used to *be* the `pending` arm, so a fixture typo or a fourth
            # outcome this build does not know produced a green "pending" run
            # against an assertion nobody wrote.
            raise ValueError(f"unknown timeout operation outcome: {outcome!r}")

        def cancellation() -> str:
            nonlocal cancellation_calls
            cancellation_calls += 1
            return step["cancellation"]

        result = self._clean(
            timeout.poll(self._wire_u64(step["now"]), operation, cancellation)
        )
        result.update(
            operation_calls=operation_calls,
            cancellation_calls=cancellation_calls,
        )
        return result

    def _barrier_step(
        self, state: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any]:
        operation = step.get("op")
        cancellation_calls = 0
        if operation == "start":
            barrier = RevisionBarrier(
                self._wire_u64(step["revision"]),
                self._wire_u64(step["required_revision"]),
                self._wire_u64(step.get("deadline")),
            )
            state["barrier"] = barrier
            value = barrier.receipt("")
        else:
            barrier = state.get("barrier")
            if not isinstance(barrier, RevisionBarrier):
                raise ValueError("barrier start must run first")
            if operation == "observe":

                def cancellation() -> str:
                    nonlocal cancellation_calls
                    cancellation_calls += 1
                    return step["cancellation"]

                value = barrier.observe(
                    self._wire_u64(step["now"]), step["predicate"], cancellation
                )
            elif operation == "register_recheck":
                value = barrier.register_recheck(
                    self._wire_u64(step["now"]),
                    self._wire_u64(step["observed_revision"]),
                    step["predicate"],
                )
            elif operation == "advance":
                value = barrier.advance(
                    self._wire_u64(step["revision"]), step["predicate"]
                )
            elif operation == "dispose":
                value = barrier.dispose()
            elif operation == "receipt":
                value = barrier.receipt(step["key"])
            else:
                raise ValueError(f"unsupported barrier op {operation}")
        result = self._clean(value)
        if operation == "observe":
            result["cancellation_calls"] = cancellation_calls
        return result

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
    for feature, steps in (
        (
            "stdlib_timer_v1",
            [
                {"op": "start", "now": 0, "duration": 1},
                {"op": "observe", "now": 1},
            ],
        ),
        (
            "stdlib_timeout_v1",
            [
                {"op": "start", "now": 0, "duration": 1},
                {
                    "op": "poll",
                    "now": 1,
                    "operation": "pending",
                    "cancellation": "pending",
                },
            ],
        ),
        (
            "stdlib_revision_barrier_v1",
            [
                {
                    "op": "start",
                    "revision": 0,
                    "required_revision": 1,
                    "deadline": None,
                },
                {"op": "advance", "revision": 1, "predicate": True},
            ],
        ),
    ):
        assert peer.handle({"cmd": "feature_reset", "feature": feature})["ok"]
        for step in steps:
            assert peer.handle(
                {"cmd": "feature_step", "feature": feature, "step": step}
            )["ok"]


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
