"""Canonical boundary-ingress adapter replay (`#lzingressadapters`)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conformance_assert import assert_key, instrument, scenarios


FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "lazily-spec"
    / "conformance"
    / "ingress"
    / "boundary_ingress_adapter.json"
)


@dataclass
class _Event:
    cursor: int
    stamped_at: int
    action: str
    key: str | None = None
    validation: str | None = None


@dataclass
class _Delivery:
    receipt_id: str
    targets: set[str] = field(default_factory=set)
    acked: set[str] = field(default_factory=set)


class _BoundaryModel:
    """Test oracle for the boundary state wrapped around the shipped IngressCell."""

    def __init__(self, max_buffered: int, freshness_horizon: int) -> None:
        self.max_buffered = max_buffered
        self.freshness_horizon = freshness_horizon
        self.phase = "detached"
        self.generation = 0
        self.cursor: int | None = None
        self.buffered: dict[int, _Event] = {}
        self.source_keys: set[str] = set()
        self.members: set[str] = set()
        self.validation = "valid"
        self.replay_from: int | None = None
        self.stale_events = 0
        self.delivery: _Delivery | None = None
        self.last_stamped_at: int | None = None
        self.now = 0
        self.revision = 0

    def _changed(self) -> None:
        self.revision += 1

    def _apply_event(self, event: _Event) -> None:
        if event.action == "upsert":
            assert event.key is not None
            self.source_keys.add(event.key)
        elif event.action == "remove":
            assert event.key is not None
            self.source_keys.discard(event.key)
        elif event.action == "validate":
            assert event.validation in {"valid", "invalid"}
            self.validation = event.validation
        else:
            raise AssertionError(f"unknown action {event.action!r}")
        self.cursor = event.cursor
        self.last_stamped_at = event.stamped_at
        self.phase = "live" if self.validation == "valid" else "invalid"
        self.replay_from = None

    def _drain(self) -> None:
        assert self.cursor is not None
        while self.cursor + 1 in self.buffered:
            self._apply_event(self.buffered.pop(self.cursor + 1))
        if self.buffered:
            self.phase = "replay_required"
            self.replay_from = self.cursor + 1

    def apply(self, op: dict[str, Any]) -> None:
        kind = op["type"]
        if kind == "subscribe":
            if op["generation"] < self.generation:
                return
            self.generation = op["generation"]
            self.cursor = None
            self.buffered.clear()
            self.source_keys.clear()
            self.members.clear()
            self.validation = "valid"
            self.replay_from = None
            self.phase = "bootstrapping"
            self._changed()
            return
        if kind == "snapshot":
            if op["generation"] < self.generation:
                self.stale_events += 1
                self._changed()
                return
            if op["generation"] > self.generation:
                self.generation = op["generation"]
                self.buffered.clear()
            self.cursor = op["cursor"]
            self.last_stamped_at = op["stamped_at"]
            self.source_keys = set(op["source_keys"])
            self.members = set(op["members"])
            self.validation = op["validation"]
            self.phase = "live" if self.validation == "valid" else "invalid"
            self.replay_from = None
            self.buffered = {
                cursor: event
                for cursor, event in self.buffered.items()
                if cursor > self.cursor
            }
            self._drain()
            self._changed()
            return
        if kind == "event":
            generation = op["generation"]
            event = _Event(
                op["cursor"],
                op["stamped_at"],
                op["action"],
                op.get("key"),
                op.get("validation"),
            )
            if generation < self.generation:
                self.stale_events += 1
                self._changed()
                return
            if generation > self.generation:
                self.generation = generation
                self.cursor = None
                self.buffered.clear()
                self.source_keys.clear()
                self.members.clear()
                self.phase = "bootstrapping"
                self.replay_from = None
            if self.cursor is None:
                if (
                    len(self.buffered) >= self.max_buffered
                    and event.cursor not in self.buffered
                ):
                    self.phase = "backpressured"
                    self.replay_from = 0
                    self._changed()
                    return
                changed = event.cursor not in self.buffered
                self.buffered.setdefault(event.cursor, event)
                if changed:
                    self._changed()
                return
            if event.cursor <= self.cursor or event.cursor in self.buffered:
                return
            if event.cursor == self.cursor + 1:
                self._apply_event(event)
                self._drain()
                self._changed()
                return
            if len(self.buffered) >= self.max_buffered:
                self.phase = "backpressured"
                self.replay_from = self.cursor + 1
                self._changed()
                return
            self.buffered[event.cursor] = event
            self.phase = "replay_required"
            self.replay_from = self.cursor + 1
            self._changed()
            return
        if kind == "member_join":
            member = op["member"]
            if member in self.members:
                return
            self.members.add(member)
            if self.delivery is not None and not self.delivery.targets:
                self.delivery.targets.add(member)
            self._changed()
            return
        if kind == "member_leave":
            if op["member"] in self.members:
                self.members.remove(op["member"])
                self._changed()
            return
        if kind == "open_receipt":
            self.delivery = _Delivery(op["receipt_id"], set(self.members))
            self._changed()
            return
        if kind == "ack":
            if self.delivery is None or self.delivery.receipt_id != op["receipt_id"]:
                return
            member = op["member"]
            if member in self.delivery.targets and member not in self.delivery.acked:
                self.delivery.acked.add(member)
                self._changed()
            return
        if kind == "tick":
            before = self.fresh
            self.now = op["now"]
            if self.fresh != before:
                self._changed()
            return
        raise AssertionError(f"unknown op {kind!r}")

    @property
    def fresh(self) -> bool:
        return (
            self.last_stamped_at is not None
            and self.now - self.last_stamped_at <= self.freshness_horizon
        )

    def projection(self) -> dict[str, Any]:
        delivery = None
        if self.delivery is not None:
            delivery = {
                "receipt_id": self.delivery.receipt_id,
                "targets": sorted(self.delivery.targets),
                "acked": sorted(self.delivery.acked),
                "converged": bool(self.delivery.targets)
                and self.delivery.targets <= self.delivery.acked,
            }
        return {
            "phase": self.phase,
            "generation": self.generation,
            "cursor": self.cursor,
            "buffered_cursors": sorted(self.buffered),
            "source_keys": sorted(self.source_keys),
            "members": sorted(self.members),
            "validation": self.validation,
            "replay_from": self.replay_from,
            "stale_events": self.stale_events,
            "delivery": delivery,
            "ready": self.phase == "live" and self.validation == "valid",
            "fresh": self.fresh,
            "observation_revision": self.revision,
            "revision": self.revision,
        }


def test_boundary_ingress_adapter_replays_canonical_contract() -> None:
    fixture = instrument(
        json.loads(FIXTURE.read_text()),
        name="ingress/boundary_ingress_adapter.json",
    )
    replayed = 0
    for scenario in scenarios(fixture):
        policy = fixture["policy"] | scenario.get("policy", {})
        model = _BoundaryModel(
            policy["max_buffered"],
            policy["freshness_horizon"],
        )
        for index, step in enumerate(scenario["steps"]):
            model.apply(step["op"])
            actual = model.projection()
            expected = step["expected"]
            for key in expected:
                assert_key(
                    expected,
                    key,
                    actual[key],
                    f"{scenario['id']} step {index}",
                )
            replayed += 1
    assert replayed > 0
