"""Cross-language state-chart conformance tests for the lazily Harel/SCXML
interpreter.

Each test loads a canonical JSON fixture from ``lazily-spec/conformance/
statechart`` and replays it, asserting ``initial_active`` (and
``initial_actions``), per-step ``accepted`` / ``active`` / ``matches`` /
``actions``. This is the cross-language behavior contract fixed by
``lazily-spec/docs/state-charts.md`` and the Lean ``StateChart`` formal model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance_assert import instrument

from lazily import ChartDef, Slot, StateChart


_SPEC_FIXTURES = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "statechart"
)

FIXTURES = [
    "flat_cycle.json",
    "hierarchical_player.json",
    "guarded_door.json",
    "parallel_regions.json",
    "history_shallow.json",
    "history_deep.json",
    "entry_exit_actions.json",
]


def load_fixture(name: str) -> dict:
    path = _SPEC_FIXTURES / name
    assert path.exists(), f"statechart fixture {name} missing from lazily-spec"
    fixture = instrument(json.loads(path.read_text()), name=f"statechart/{name}")
    assert fixture["kind"] == "StateChart"
    return fixture


def _sorted_active(value: str | list[str]) -> list[str]:
    return sorted(value) if isinstance(value, list) else [value]


@pytest.mark.parametrize("name", FIXTURES)
def test_statechart_conformance(name: str) -> None:
    fixture = load_fixture(name)
    chart = StateChart({}, ChartDef.from_chart(fixture["chart"]))

    assert chart.active_leaves() == _sorted_active(fixture["initial_active"])
    if "initial_actions" in fixture:
        assert chart.last_actions() == fixture["initial_actions"]

    for index, step in enumerate(fixture["steps"]):
        label = f"{name} step {index} ({step['event']})"
        accepted = chart.send(step["event"], step.get("guards", {}))

        assert accepted is step["accepted"], f"{label}: accepted"
        assert chart.active_leaves() == _sorted_active(step["active"]), (
            f"{label}: active"
        )
        for sid, expected in step.get("matches", {}).items():
            assert chart.matches(sid) is expected, f"{label}: matches({sid})"
        if "actions" in step:
            assert chart.last_actions() == step["actions"], f"{label}: actions"


def test_flat_chart_walks_up_and_resolves_lca() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "green",
                "states": {
                    "root": {"initial": "green"},
                    "red": {"parent": "root", "on": {"TICK": "green"}},
                    "green": {"parent": "root", "on": {"TICK": "yellow"}},
                    "yellow": {"parent": "root", "on": {"TICK": "red"}},
                },
            }
        ),
    )
    assert chart.active_leaves() == ["green"]
    assert chart.send("TICK") is True
    assert chart.active_leaves() == ["yellow"]
    assert chart.send("UNKNOWN") is False
    assert chart.active_leaves() == ["yellow"]
    assert "root" in chart.configuration()
    assert chart.matches("green") is False


def test_named_guards_fail_closed_when_absent() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "closed",
                "states": {
                    "root": {"initial": "closed"},
                    "closed": {
                        "parent": "root",
                        "on": {"OPEN": {"target": "open", "guard": "allowed"}},
                    },
                    "open": {"parent": "root", "on": {"CLOSE": "closed"}},
                },
            }
        ),
    )
    assert chart.send("OPEN") is False
    assert chart.active_leaves() == ["closed"]
    assert chart.send("OPEN", {"allowed": True}) is True
    assert chart.active_leaves() == ["open"]


def test_unsupported_features_rejected_explicitly() -> None:
    with pytest.raises(TypeError, match="run"):
        ChartDef.from_chart(
            {
                "initial": "a",
                "states": {
                    "root": {"initial": "a"},
                    "a": {"parent": "root", "run": ["x"]},
                },
            }
        )
    with pytest.raises(TypeError, match="expr"):
        ChartDef.from_chart(
            {
                "initial": "a",
                "states": {
                    "root": {"initial": "a"},
                    "a": {
                        "parent": "root",
                        "on": {"GO": {"target": "a", "guard": {"expr": "x"}}},
                    },
                },
            }
        )


def test_rejected_event_leaves_configuration_unchanged() -> None:
    chart = StateChart(
        {},
        ChartDef.from_chart(
            {
                "initial": "a1",
                "states": {
                    "root": {"initial": "a"},
                    "a": {"parent": "root", "initial": "a1", "entry": ["enterA"]},
                    "a1": {"parent": "a", "on": {"SWAP": "a2"}},
                    "a2": {"parent": "a"},
                },
            }
        ),
    )
    assert chart.last_actions() == ["enterA"]
    assert chart.send("NOPE") is False
    assert chart.last_actions() == []
    assert chart.active_leaves() == ["a1"]


def test_reactive_invalidation_on_real_transition() -> None:
    """A slot reading the configuration recomputes on a real transition, and a
    no-op self-transition is suppressed by the Cell's != guard (spec rule)."""
    ctx: dict = {}
    chart = StateChart(
        ctx,
        ChartDef.from_chart(
            {
                "initial": "a",
                "states": {
                    "root": {"initial": "a"},
                    "a": {"parent": "root", "on": {"TO_B": "b", "SELF": "a"}},
                    "b": {"parent": "root", "on": {"TO_A": "a"}},
                },
            }
        ),
    )
    computed: list[int] = []

    @Slot
    def leaf_count(ctx: dict) -> int:
        n = len(chart.active_leaves(ctx))
        computed.append(n)
        return n

    assert leaf_count(ctx) == 1
    assert computed == [1]

    # Real transition a -> b: the slot recomputes.
    chart.send("TO_B")
    assert leaf_count(ctx) == 1
    assert computed == [1, 1]

    # Real transition b -> a: recomputes again.
    chart.send("TO_A")
    assert leaf_count(ctx) == 1
    assert computed == [1, 1, 1]

    # No-op self-transition a -> a: accepted but the resulting configuration is
    # equal, so the Cell's != guard suppresses propagation (no recompute).
    assert chart.send("SELF") is True
    assert leaf_count(ctx) == 1
    assert computed == [1, 1, 1]
