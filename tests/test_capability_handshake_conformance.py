"""Canonical capability-handshake negotiation (``#lzhandshakedeadfields``)."""

from __future__ import annotations

import json
from pathlib import Path

from conformance_assert import TrackedBlock, assert_key, instrument, scenarios

from lazily.ipc import CapabilityHandshake


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"
_SPEC_FIXTURES = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"
_FIXTURE = "codec/capability_handshake.json"


def _load() -> dict:
    spec_path = _SPEC_FIXTURES / _FIXTURE
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / _FIXTURE
    fixture = instrument(json.loads(path.read_text()), name=_FIXTURE)
    assert fixture["protocol_version"] == 1
    assert fixture["kind"] == "CapabilityHandshake"
    return fixture


def test_capability_handshake_conformance() -> None:
    fixture = _load()
    replayed = 0

    for scenario in scenarios(fixture):
        replayed += 1
        local = CapabilityHandshake.from_wire(scenario["local"])
        remote = CapabilityHandshake.from_wire(scenario["remote"])
        result = local.negotiate_with(remote)
        expected: TrackedBlock = scenario["expected"]

        assert_key(expected, "compatible", result.compatible)
        if result.compatible:
            assert result.max_frame_size is not None
            assert result.fragmentation_supported is not None
            assert_key(expected, "negotiated_max_frame_size", result.max_frame_size)
            assert_key(
                expected,
                "negotiated_fragmentation_supported",
                result.fragmentation_supported,
            )
        else:
            assert result.field is not None
            assert_key(expected, "field", result.field)

    assert replayed == 5, "the settled handshake fixture has five scenarios"
