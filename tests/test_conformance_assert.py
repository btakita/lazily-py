"""Tests for the unconsumed-assertion-key guard itself (#lzassertunknownkeys).

The guard is the thing that decides whether a conformance failure is reported at
all, so it needs its own coverage: a guard that quietly stops reporting is the
same silent skip one level up.

Each test leaves the session ledger clean — it consumes every key it declares —
because ``conftest.pytest_sessionfinish`` fails the run on anything left behind,
and a self-test must not fail the suite it is checking.
"""

from __future__ import annotations

from conformance_assert import (
    TrackedBlock,
    consumption_failures,
    instrument,
    tracked,
)


def test_unconsumed_key_is_reported_and_named() -> None:
    block = tracked(
        {"epoch": 9, "first_op_payload_backend": "arrow"},
        fixture="selftest/unconsumed.json",
    )
    assert block["epoch"] == 9

    assert block.unconsumed() == ["first_op_payload_backend"]
    reported = [
        line
        for line in consumption_failures()
        if line.startswith("selftest/unconsumed.json")
    ]
    assert reported, "an unconsumed key produced no report line"
    assert "first_op_payload_backend" in reported[0]

    # Reading the remaining key clears it: the report tracks what the runner
    # really took out of the block, not a static declaration.
    assert dict(block) == {"epoch": 9, "first_op_payload_backend": "arrow"}
    assert block.unconsumed() == []


def test_whole_block_equality_consumes_every_key() -> None:
    """A full-dict compare is the strongest consumption: it checks every key."""
    block = tracked({"outcome": "pending", "deadline": 10}, fixture="selftest/eq.json")
    assert block == {"outcome": "pending", "deadline": 10}
    assert block.unconsumed() == []


def test_get_and_contains_count_as_reads() -> None:
    block = tracked({"a": 1, "b": 2}, fixture="selftest/access.json")
    assert block.get("a") == 1
    assert "b" in block
    assert block.unconsumed() == []


def test_prose_keys_are_declared_consumed() -> None:
    """Narration inside an assertion block is exempt only when it is declared."""
    block = tracked(
        {"note": "docA drops because pid100 died", "cascade": True},
        fixture="selftest/prose.json",
        prose=("note",),
    )
    assert block.unconsumed() == ["cascade"]
    assert block["cascade"] is True
    assert block.unconsumed() == []


def test_instrument_wraps_nested_expectation_blocks() -> None:
    fixture = instrument(
        {"steps": [{"name": "first", "expect": {"value": 1}}]},
        name="selftest/instrument.json",
    )
    block = fixture["steps"][0]["expect"]
    assert isinstance(block, TrackedBlock)
    # The block path names the scenario, so the failure points at one place.
    assert block.block == "steps[first].expect"
    assert block == {"value": 1}
