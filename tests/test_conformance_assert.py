"""Tests for the conformance assertion ledger itself.

Two rungs live here (#lzassertunknownkeys, #lzconsumednotasserted). The ledger is
the thing that decides whether a conformance failure is reported at all, so it
needs its own coverage: a guard that quietly stops reporting is the same silent
skip one level up. And the second rung exists precisely because *reading* a key
looked identical to *checking* it — so these tests assert that a read which never
reaches a comparison is reported, and that an excuse which has stopped hiding
anything is reported too.

Each test leaves the session ledger clean — it either satisfies every key it
declares or drops its own fixture with ``reset(fixture=...)`` — because
``conftest.pytest_sessionfinish`` fails the run on anything left behind, and a
self-test must not fail the suite it is checking.
"""

from __future__ import annotations

import pytest
from conformance_assert import (
    TrackedBlock,
    assert_key,
    assert_key_with,
    consumption_failures,
    excuse_key,
    instrument,
    reset,
    tracked,
)


def _report(fixture: str) -> list[str]:
    return [line for line in consumption_failures() if line.startswith(fixture)]


# ---------------------------------------------------------------------------
# Rung 2: a key no runner read
# ---------------------------------------------------------------------------


def test_unconsumed_key_is_reported_and_named() -> None:
    block = tracked(
        {"epoch": 9, "first_op_payload_backend": "arrow"},
        fixture="selftest/unconsumed.json",
    )
    assert_key(block, "epoch", 9)

    assert block.unconsumed() == ["first_op_payload_backend"]
    reported = _report("selftest/unconsumed.json")
    assert reported, "an unconsumed key produced no report line"
    assert "first_op_payload_backend" in reported[0]
    assert "never consumed" in reported[0]

    # Asserting the remaining key clears it: the report tracks what the runner
    # really took out of the block, not a static declaration.
    assert_key(block, "first_op_payload_backend", "arrow")
    assert block.unconsumed() == []
    assert not _report("selftest/unconsumed.json")


def test_whole_block_equality_consumes_and_asserts_every_key() -> None:
    """A full-dict compare is the strongest check: every value reaches it."""
    block = tracked({"outcome": "pending", "deadline": 10}, fixture="selftest/eq.json")
    assert block == {"outcome": "pending", "deadline": 10}
    assert block.unconsumed() == []
    assert not _report("selftest/eq.json")


def test_prose_keys_are_declared_consumed() -> None:
    """Narration inside an assertion block is exempt from all three verdicts."""
    block = tracked(
        {"note": "docA drops because pid100 died", "cascade": True},
        fixture="selftest/prose.json",
        prose=("note",),
    )
    assert block.unconsumed() == ["cascade"]
    assert_key(block, "cascade", True)
    assert block.unconsumed() == []
    assert not _report("selftest/prose.json")


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


# ---------------------------------------------------------------------------
# Rung 3: a key the runner read and then discarded
# ---------------------------------------------------------------------------


def test_read_without_a_comparison_is_reported() -> None:
    """The exact hole this rung closes: consumed, green, and never checked."""
    block = tracked({"a": 1, "b": 2}, fixture="selftest/access.json")
    assert block.get("a") == 1  # a hand comparison the ledger cannot see
    assert "b" in block  # a membership probe that checks nothing

    # Rung 2 is satisfied — both keys were read — and the run is still wrong.
    assert block.unconsumed() == []
    reported = _report("selftest/access.json")
    assert reported, "a read-then-discarded key produced no report line"
    assert "'a', 'b'" in reported[0]
    assert "never compared against the fixture's value" in reported[0]

    reset(fixture="selftest/access.json")


def test_assert_key_marks_the_key_asserted() -> None:
    block = tracked({"epoch": 4}, fixture="selftest/assert.json")
    assert assert_key(block, "epoch", 4) == 4
    assert not _report("selftest/assert.json")


def test_assert_key_fails_on_a_mismatch_naming_the_fixture() -> None:
    block = tracked({"epoch": 4}, fixture="selftest/mismatch.json")
    with pytest.raises(AssertionError, match=r"selftest/mismatch\.json"):
        assert_key(block, "epoch", 5)
    assert not _report("selftest/mismatch.json")


def test_comparing_against_a_literal_never_marks_the_key() -> None:
    """Shape 3: the fixture gates a branch instead of reaching the comparison."""
    block = tracked({"reran": False}, fixture="selftest/literal.json")
    if block["reran"] is False:  # the fixture picks the branch...
        assert True  # ...and a constant is what gets checked
    reported = _report("selftest/literal.json")
    assert reported and "never compared" in reported[0]

    reset(fixture="selftest/literal.json")


def test_assert_key_with_hands_the_fixture_value_to_the_predicate() -> None:
    block = tracked({"similarity_min": 0.8}, fixture="selftest/with.json")
    seen: list[float] = []
    assert_key_with(block, "similarity_min", lambda want: seen.append(want) or True)
    assert seen == [0.8]
    assert not _report("selftest/with.json")

    other = tracked({"len": 3}, fixture="selftest/with2.json")
    with pytest.raises(AssertionError, match="predicate rejected"):
        assert_key_with(other, "len", lambda want: want == 4)
    assert not _report("selftest/with2.json")


# ---------------------------------------------------------------------------
# Declared exceptions, both directions
# ---------------------------------------------------------------------------


def test_excuse_key_satisfies_the_key() -> None:
    block = tracked({"handle_stable": True}, fixture="selftest/excused.json")
    excuse_key(block, "handle_stable", "proven by test_handle_identity_survives_move")
    assert not _report("selftest/excused.json")


def test_excuse_key_requires_a_reason() -> None:
    block = tracked({"x": 1}, fixture="selftest/noreason.json")
    with pytest.raises(AssertionError, match="non-empty reason"):
        excuse_key(block, "x", "   ")
    reset(fixture="selftest/noreason.json")


def test_excuse_key_rejects_a_key_the_fixture_does_not_carry() -> None:
    block = tracked({"x": 1}, fixture="selftest/absent.json")
    with pytest.raises(AssertionError, match="the excuse has rotted"):
        excuse_key(block, "y", "nothing to compare")
    reset(fixture="selftest/absent.json")


def test_a_stale_excuse_is_reported() -> None:
    """Both directions: an excuse for a key the same run asserts hides nothing."""
    block = tracked({"cursor": 7}, fixture="selftest/stale.json")
    excuse_key(block, "cursor", "checked by the outbox store protocol runner")
    assert_key(block, "cursor", 7)

    reported = _report("selftest/stale.json")
    assert reported, "a stale excuse produced no report line"
    assert "is stale" in reported[0]
    assert "outbox store protocol runner" in reported[0]

    reset(fixture="selftest/stale.json")
