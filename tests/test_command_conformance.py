"""Cross-language conformance for the command / RPC message plane.

Replays the canonical fixtures in
``lazily-spec/conformance/message-passing`` (the same eight files the Rust,
Kotlin, and JS bindings replay) through :class:`lazily.command.CommandProjection`
and the :class:`lazily.command.CommandRpcClient` facade. Each fixture is a
sequence of externally-tagged frames (``CommandSubmit`` / ``CommandCancel`` /
``CommandEvents`` / ``CommandProjection``, plus ``CausalReceipts`` for terminal
authority); the assertions pin the load-bearing rules — terminal authority is
the receipt, generation guards, idempotency, cancel-before-terminal-only,
terminal-conflict-fails-closed, and reconnect equivalence.

A wire-schema compliance test round-trips every ``CommandMessage`` variant
through ``message-passing.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance_assert import instrument

from lazily.command import (
    CallStateKind,
    CommandApplyStatus,
    CommandMessage,
    CommandProjection,
    CommandRpcClient,
    CommandStatus,
)
from lazily.ipc import CausalReceipts


_SPEC_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "lazily-spec"
    / "conformance"
    / "message-passing"
)


def _fixture(name: str) -> dict:
    path = _SPEC_FIXTURES / name
    assert path.exists(), f"missing spec fixture {name}"
    return instrument(json.loads(path.read_text()), name=f"message-passing/{name}")


class _Transport:
    """Capturing transport double for the RPC facade."""

    def __init__(self) -> None:
        self.sent: list[CommandMessage] = []

    def send(self, message: CommandMessage) -> None:
        self.sent.append(message)


def _ingest_frame(proj: CommandProjection, frame: dict) -> CommandApplyStatus:
    schema = frame["schema"]
    wire = frame["wire"]
    if schema == "message-passing":
        return proj.apply_message(CommandMessage.from_wire(wire))
    if schema == "receipts":
        result = CommandApplyStatus.UNKNOWN
        for receipt in CausalReceipts.from_wire(wire).receipts:
            result = proj.observe_receipt(receipt)
        return result
    raise ValueError(f"unknown frame schema: {schema!r}")


def _entry_dict(proj: CommandProjection, command_id: str) -> dict:
    entry = proj.entry(command_id)
    assert entry is not None
    return entry.to_wire()


def _run_frames_expect(
    proj: CommandProjection, frames: list[dict], expect: dict
) -> None:
    terminal_cmd = None
    for i, frame in enumerate(frames):
        _ingest_frame(proj, frame)
        if "terminal_after_frame_index" in expect:
            terminal_cmd = terminal_cmd or _first_command_id(frames)
            want_terminal = i >= expect["terminal_after_frame_index"]
            entry = proj.entry(terminal_cmd)
            assert entry is not None
            assert entry.terminal == want_terminal, (
                f"frame {i}: terminal={entry.terminal} want {want_terminal}"
            )
        if "ignored_frame_indices" in expect and i in expect["ignored_frame_indices"]:
            # Ignored frames are stale/late; status stays as it was. The exact
            # status is pinned by the projection expectation below.
            pass

    if "projection" in expect:
        image = proj.to_image()
        assert image.generation == expect["projection"]["generation"]
        expected_cmds = expect["projection"]["commands"]
        assert len(image.commands) == len(expected_cmds)
        for got, exp in zip(image.commands, expected_cmds, strict=True):
            assert got.to_wire() == exp

    if "ignored_frame_indices" in expect:
        # The ignored frames did not mutate the projection; verify by
        # re-asserting the projected status matches the expected commands.
        for exp in expect["projection"]["commands"]:
            assert _entry_dict(proj, exp["command_id"]) == exp


def _first_command_id(frames: list[dict]) -> str:
    for frame in frames:
        wire = frame["wire"]
        if "CommandSubmit" in wire:
            return wire["CommandSubmit"]["command_id"]
    raise AssertionError("no CommandSubmit frame found")


def _run_fixture(name: str) -> None:
    fixture = _fixture(name)
    assert fixture["kind"] == "Command"

    # Fixtures come in two shapes: a top-level `frames` list, or a
    # `scenarios` list where each scenario has its own `frames` and `expect`.
    scenarios = fixture.get("scenarios")
    if scenarios:
        for sc in scenarios:
            proj = CommandProjection()
            _run_frames_expect(proj, sc["frames"], sc["expect"])
    else:
        proj = CommandProjection()
        _run_frames_expect(proj, fixture["frames"], fixture["expect"])


_FIXTURE_NAMES = [
    "accepted_then_applied_receipt.json",
    "rpc_call_waits_for_terminal.json",
    "stale_generation_ignored.json",
    "terminal_conflict_fail_closed.json",
    "cancel_preempts_nonterminal.json",
    "reconnect_command_projection.json",
    "editor_route_submit.json",
    "sync_tmux_layout_submit.json",
]


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_command_conformance(name: str) -> None:
    _run_fixture(name)


# ---------------------------------------------------------------------------
# Terminal-conflict and stale-generation detail assertions
# ---------------------------------------------------------------------------


def test_terminal_conflict_fail_closed_detail() -> None:
    fixture = _fixture("terminal_conflict_fail_closed.json")
    proj = CommandProjection()
    frames = fixture["frames"]
    expect = fixture["expect"]
    cmd = _first_command_id(frames)
    assert cmd == expect["conflict_command_id"]

    # The conflict lands on the frame the fixture names, and NOT before it: a
    # reducer that flagged a conflict at frame 1 would still end up "in
    # conflict" without failing closed at the right point.
    conflict_at = expect["conflict_after_frame_index"]
    for index, frame in enumerate(frames[:conflict_at]):
        _ingest_frame(proj, frame)
        assert not proj.has_conflict(cmd), f"conflict raised early at frame {index}"
    before = proj.to_image().to_wire()
    assert before == expect["projection_before_conflict"]
    assert proj.entry(cmd).status is CommandStatus.APPLIED

    # Frame 2: conflicting rejected receipt -> fail closed, applied preserved.
    status = _ingest_frame(proj, frames[conflict_at])
    assert status is CommandApplyStatus.TERMINAL_CONFLICT
    assert proj.has_conflict(cmd) is expect["conflict"]
    assert proj.entry(cmd).status is CommandStatus.APPLIED  # unchanged
    assert proj.last_conflict is not None
    assert proj.last_conflict.command_id == cmd
    assert proj.last_conflict.existing is CommandStatus.APPLIED
    assert proj.last_conflict.incoming is CommandStatus.REJECTED


def test_stale_generation_ignored_detail() -> None:
    fixture = _fixture("stale_generation_ignored.json")
    proj = CommandProjection()
    cmd = _first_command_id(fixture["frames"])
    for i, frame in enumerate(fixture["frames"]):
        status = _ingest_frame(proj, frame)
        if i in fixture["expect"]["ignored_frame_indices"]:
            assert status is CommandApplyStatus.STALE_GENERATION
    assert proj.entry(cmd).status is CommandStatus.SUBMITTED
    assert not proj.entry(cmd).terminal


def test_cancel_preempts_then_cancel_after_applied_ignored() -> None:
    fixture = _fixture("cancel_preempts_nonterminal.json")
    scenarios = fixture["scenarios"]

    before = scenarios[0]
    proj = CommandProjection()
    for frame in before["frames"]:
        _ingest_frame(proj, frame)
    cmd = before["expect"]["projection"]["commands"][0]["command_id"]
    assert proj.entry(cmd).status is CommandStatus.CANCELLED
    assert proj.entry(cmd).terminal

    after = scenarios[1]
    proj2 = CommandProjection()
    for _i, frame in enumerate(after["frames"]):
        _ingest_frame(proj2, frame)
    # Frame 2 (the late cancel) is ignored — applied stays.
    assert proj2.entry(cmd).status is CommandStatus.APPLIED
    assert proj2.entry(cmd).terminal_receipt_id == "rcpt-applied"


def test_rpc_call_resolves_only_on_terminal_receipt() -> None:
    fixture = _fixture("rpc_call_waits_for_terminal.json")
    frames = fixture["frames"]
    expect = fixture["expect"]["rpc"]
    cmd = _first_command_id(frames)

    transport = _Transport()
    client = CommandRpcClient(transport)
    # The submit frame seeds the projection via the facade.
    submit_wire = frames[0]["wire"]["CommandSubmit"]
    from lazily.command import CommandSubmit

    client.submit(CommandSubmit.from_wire(submit_wire))
    # Ingest subsequent frames (events + receipt) one at a time.
    for i, frame in enumerate(frames[1:], start=1):
        if i in expect["unresolved_after_frame_indices"]:
            assert client.poll_call(cmd).kind is CallStateKind.PENDING
        _ingest_frame(client.projection, frame)
        if i == expect["resolves_after_frame_index"]:
            state = client.poll_call(cmd)
            assert state.kind is CallStateKind.RESOLVED
            assert state.entry is not None
            assert state.entry.status.value == expect["terminal_status"]


# ---------------------------------------------------------------------------
# Wire schema compliance
# ---------------------------------------------------------------------------

jsonschema = pytest.importorskip("jsonschema")
referencing = pytest.importorskip("referencing")
from referencing import Registry  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402


_SPEC_SCHEMAS = Path(__file__).resolve().parents[2] / "lazily-spec" / "schemas"


def _registry() -> Registry:
    names = ["defs", "message-passing", "receipts"]
    schemas = {
        f"https://lazily.dev/schemas/{n}.json": json.loads(
            (_SPEC_SCHEMAS / f"{n}.json").read_text()
        )
        for n in names
    }
    resources = [
        (uri, DRAFT202012.create_resource(schema)) for uri, schema in schemas.items()
    ]
    return Registry().with_resources(resources)


def test_command_plane_wire_validates_schema() -> None:
    """Round-trip every CommandMessage variant through message-passing.json."""
    validator = jsonschema.Draft202012Validator(
        json.loads((_SPEC_SCHEMAS / "message-passing.json").read_text()),
        registry=_registry(),
    )
    for name in _FIXTURE_NAMES:
        fixture = _fixture(name)
        if "scenarios" in fixture:
            frame_groups = [sc["frames"] for sc in fixture["scenarios"]]
        else:
            frame_groups = [fixture["frames"]]
        for frames in frame_groups:
            for frame in frames:
                if frame["schema"] != "message-passing":
                    continue
                wire = frame["wire"]
                validator.validate(wire)
                # Round-trip: from_wire -> to_wire is byte-identical.
                msg = CommandMessage.from_wire(wire)
                assert msg.to_wire() == wire


def test_command_message_round_trips_inline_and_shared_blob_payloads() -> None:
    fixture = _fixture("sync_tmux_layout_submit.json")
    wire = fixture["frames"][0]["wire"]
    from lazily.command import CommandSubmit

    submit = CommandSubmit.from_wire(wire["CommandSubmit"])
    assert submit.payload.to_wire() == wire["CommandSubmit"]["payload"]
    assert submit.to_wire() == wire["CommandSubmit"]
