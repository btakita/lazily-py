"""Tests for the conformance assertion ledger itself.

Three rungs live here (#lzassertunknownkeys, #lzconsumednotasserted,
#lzscenariocoverage). The ledger is
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

import json
from typing import TYPE_CHECKING

import pytest
from conformance_assert import (
    TrackedBlock,
    assert_key,
    assert_key_with,
    consumption_failures,
    excuse_key,
    excuse_scenario,
    instrument,
    prose_failures,
    prose_key,
    record_scenario,
    replayed_scenarios,
    reset,
    scenario_failures,
    scenario_id,
    scenarios,
    tracked,
    verify_prose,
)


if TYPE_CHECKING:
    from pathlib import Path


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


# ---------------------------------------------------------------------------
# Declared prose keys: discharged, never asserted, never excused
# (#lzprosekeyconvention)
#
# One test per failure mode the convention names. These are the rules that make
# a discharge a CLAIM rather than a form of words, so a rule that quietly stops
# firing puts lazily-py back where the nine bindings started: four defensible
# treatments of one key, indistinguishable from four accidents.
# ---------------------------------------------------------------------------

_PROSE_FIXTURE = "selftest/prose_convention.json"


def _prose_block(**extra: object) -> TrackedBlock:
    """A block declaring one prose key plus an executable sibling."""
    data: dict[str, object] = {
        "prose": ["clause"],
        "clause": "a decoder MUST reject rather than round",
        "node_id_decimal": "9007199254740993",
    }
    data.update(extra)
    return tracked(data, fixture=_PROSE_FIXTURE)


def test_prose_key_discharged_by_an_asserted_key_verifies() -> None:
    """The happy path: the naming is checked, and `prose` itself is consumed."""
    block = _prose_block()
    prose_key(block, "clause", discharged_by=["node_id_decimal"])
    assert_key(block, "node_id_decimal", "9007199254740993")
    verify_prose(_PROSE_FIXTURE)

    assert block.unconsumed() == []
    assert not _report(_PROSE_FIXTURE)
    assert not [line for line in prose_failures() if line.startswith(_PROSE_FIXTURE)]

    reset(fixture=_PROSE_FIXTURE)


def test_rule_1_asserting_a_declared_prose_key_fails() -> None:
    """Comparing a paragraph to a literal pins WORDING, not behaviour."""
    block = _prose_block()
    with pytest.raises(AssertionError, match="would ASSERT prose key"):
        assert_key(block, "clause", "a decoder MUST reject rather than round")

    # Same verdict through whole-block equality, which asserts every key at once
    # and would otherwise be the way around rule 1.
    with pytest.raises(AssertionError, match="would ASSERT prose key"):
        assert block == {}

    reset(fixture=_PROSE_FIXTURE)


def test_rule_2_excusing_a_declared_prose_key_with_free_text_fails() -> None:
    """The form lazily-py used to write: falsifiable in principle, checked by
    nothing."""
    block = _prose_block()
    with pytest.raises(AssertionError, match="free-text excuse for a key"):
        excuse_key(
            block,
            "clause",
            "prose: the behaviour it describes is asserted by the decode below",
        )

    reset(fixture=_PROSE_FIXTURE)


def test_rule_3_discharging_an_undeclared_key_fails() -> None:
    """The corpus decides which keys are paragraphs; a binding must not."""
    block = _prose_block()
    with pytest.raises(AssertionError, match="does NOT declare"):
        prose_key(block, "node_id_decimal", discharged_by=["clause"])

    reset(fixture=_PROSE_FIXTURE)


def test_rule_3_discharging_a_key_the_fixture_lacks_has_rotted() -> None:
    block = _prose_block()
    with pytest.raises(AssertionError, match="the discharge has rotted"):
        prose_key(block, "renamed_upstream", discharged_by=["node_id_decimal"])

    reset(fixture=_PROSE_FIXTURE)


def test_rule_4_a_forgotten_prose_key_fails_verification() -> None:
    """The comparison that consumes `prose` — and makes a forgotten key fail
    rather than vanish."""
    block = tracked(
        {
            "prose": ["clause", "anti_vacuity"],
            "clause": "a decoder MUST reject rather than round",
            "anti_vacuity": "the two exact scenarios are the control",
            "node_id_decimal": "9007199254740993",
        },
        fixture=_PROSE_FIXTURE,
    )
    prose_key(block, "clause", discharged_by=["node_id_decimal"])
    assert_key(block, "node_id_decimal", "9007199254740993")

    with pytest.raises(AssertionError, match=r"\['anti_vacuity'\].*never discharged"):
        verify_prose(_PROSE_FIXTURE)

    reset(fixture=_PROSE_FIXTURE)


def test_rule_5_a_discharge_naming_nothing_fails() -> None:
    """A discharge that names nothing is the free-text excuse with the text
    removed."""
    block = _prose_block()
    with pytest.raises(AssertionError, match="names NO"):
        prose_key(block, "clause", discharged_by=[])

    reset(fixture=_PROSE_FIXTURE)


def test_rule_6_a_discharge_naming_a_never_asserted_key_fails() -> None:
    """The whole convention: the excuse becomes falsifiable, so falsify it."""
    block = _prose_block()
    prose_key(block, "clause", discharged_by=["node_id_decimal"])
    # ...and then never assert it.

    with pytest.raises(AssertionError, match="never ASSERTED"):
        verify_prose(_PROSE_FIXTURE)

    reset(fixture=_PROSE_FIXTURE)


def test_rule_6_matches_by_key_name_in_any_block_of_the_fixture() -> None:
    """Fixture-scoped, not block-scoped: `epoch_disambiguation` sits in
    `assertions` and is discharged by `expect.frame_epoch`, asserted long after
    that block is finished."""
    fixture = instrument(
        {
            "assertions": {
                "prose": ["epoch_disambiguation"],
                "epoch_disambiguation": "frame_epoch and blob_epoch are DIFFERENT",
                "scenario_count": 1,
            },
            "scenarios": [
                {"id": "only", "expect": {"frame_epoch": 9, "blob_epoch": 5}}
            ],
        },
        name=_PROSE_FIXTURE,
    )
    block = fixture["assertions"]
    prose_key(
        block, "epoch_disambiguation", discharged_by=["frame_epoch", "blob_epoch"]
    )
    assert_key(block, "scenario_count", 1)

    expect = fixture["scenarios"][0]["expect"]
    assert_key(expect, "frame_epoch", 9)
    assert_key(expect, "blob_epoch", 5)

    verify_prose(fixture)
    assert not _report(_PROSE_FIXTURE)

    reset(fixture=_PROSE_FIXTURE)


def test_rule_7_a_discharge_naming_another_prose_key_fails() -> None:
    """A paragraph cannot carry another paragraph's obligation."""
    block = tracked(
        {
            "prose": ["clause", "theorem"],
            "clause": "a decoder MUST reject rather than round",
            "theorem": "resolve_wrong_backend — receivers route by kind",
            "node_id_decimal": "9007199254740993",
        },
        fixture=_PROSE_FIXTURE,
    )
    with pytest.raises(AssertionError, match="is itself prose"):
        prose_key(block, "clause", discharged_by=["theorem"])
    # Self-naming is the same failure, and is caught by the same rule.
    with pytest.raises(AssertionError, match="is itself prose"):
        prose_key(block, "clause", discharged_by=["clause"])

    reset(fixture=_PROSE_FIXTURE)


def test_a_run_that_never_verifies_is_reported() -> None:
    """An unverified discharge claim is as unchecked as an unconsumed key."""
    block = _prose_block()
    prose_key(block, "clause", discharged_by=["node_id_decimal"])
    assert_key(block, "node_id_decimal", "9007199254740993")

    reported = [line for line in prose_failures() if line.startswith(_PROSE_FIXTURE)]
    assert reported, "an unverified prose discharge produced no report line"
    assert "verify_prose" in reported[0]

    verify_prose(_PROSE_FIXTURE)
    assert not [line for line in prose_failures() if line.startswith(_PROSE_FIXTURE)]

    reset(fixture=_PROSE_FIXTURE)


def test_a_declaring_block_is_reported_when_nothing_discharges_at_all() -> None:
    """A runner that ignores `assertions.prose` entirely: `prose` is an
    unconsumed key AND the fixture is unverified. Self-enforcing rollout."""
    block = _prose_block()
    assert_key(block, "node_id_decimal", "9007199254740993")

    assert block.unconsumed() == ["clause", "prose"]
    reported = _report(_PROSE_FIXTURE)
    assert reported and "'clause', 'prose'" in reported[0]
    assert [line for line in prose_failures() if line.startswith(_PROSE_FIXTURE)]

    reset(fixture=_PROSE_FIXTURE)


def test_a_declared_key_is_not_satisfied_by_the_reserved_name_exemption() -> None:
    """`note` is exempt BY NAME as an annotation, and that exemption stops at
    the block's own declaration — otherwise a reserved name is a place no runner
    can be made to discharge anything, which is the hazard the convention calls
    out. ``frame_roundtrip_json.json``'s top-level `note` is a real rule."""
    block = tracked(
        {
            "prose": ["note"],
            "note": "`role` and `byte_canonical` are different senses of canonical",
            "role": "reference",
        },
        fixture=_PROSE_FIXTURE,
        prose=("note",),
    )
    assert_key(block, "role", "reference")
    assert block.unconsumed() == ["note", "prose"]

    prose_key(block, "note", discharged_by=["role"])
    verify_prose(_PROSE_FIXTURE)
    assert block.unconsumed() == []

    reset(fixture=_PROSE_FIXTURE)


def test_an_undeclared_step_note_stays_exempt_by_name() -> None:
    """The ~97 reactive-graph step notes are annotations and must not churn."""
    fixture = instrument(
        {
            "steps": [
                {
                    "name": "first",
                    "expect": {"note": "teardown is idempotent", "value": 1},
                }
            ]
        },
        name="selftest/step_note.json",
    )
    block = fixture["steps"][0]["expect"]
    assert_key(block, "value", 1)
    assert block.unconsumed() == []
    assert not _report("selftest/step_note.json")
    assert not [
        line for line in prose_failures() if line.startswith("selftest/step_note.json")
    ]

    reset(fixture="selftest/step_note.json")


def test_verify_prose_needs_a_named_fixture() -> None:
    with pytest.raises(AssertionError, match="corpus-relative fixture path"):
        verify_prose({"assertions": {}})


# ---------------------------------------------------------------------------
# Rung 4: a scenario no runner replayed (#lzscenariocoverage)
# ---------------------------------------------------------------------------

_SELFTEST = "selftest/scenarios.json"


def _corpus(tmp_path: Path, scenario_list: list[dict]) -> Path:
    """Write a throwaway fixture so the guard has a corpus to compare against.

    Never the shared ``lazily-spec`` corpus: it is read by all nine bindings and a
    probe written into it reddens every one of them.
    """
    path = tmp_path / _SELFTEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"scenarios": scenario_list}), encoding="utf-8")
    return tmp_path


def _scenario_report(tmp_path: Path, scenario_list: list[dict]) -> list[str]:
    # Filtered to this fixture: the guard reports over every excused fixture in
    # the session, and a self-test must read only the violation it planted.
    failures, _ = scenario_failures(
        [_SELFTEST], corpus=_corpus(tmp_path, scenario_list)
    )
    return [line for line in failures if line.startswith(_SELFTEST)]


def test_scenario_id_resolution_order() -> None:
    """``id`` beats ``name``, and there is no third option — in every binding."""
    assert scenario_id({"id": "a", "name": "b"}, 0) == "a"
    assert scenario_id({"name": "b"}, 0) == "b"


def test_scenario_id_refuses_an_unidentified_scenario() -> None:
    """A positional id silently rebinds on a corpus reorder (#lzspecscenarioids).

    The ledger would record "index 2 was replayed" and the guard would compare it
    against whatever now sits at index 2 -- the two agree with each other about
    the wrong scenario, and nothing turns red.
    """
    with pytest.raises(AssertionError, match="carries neither `id` nor `name`"):
        scenario_id({"policy": "Sum"}, 2)


def test_scenario_id_refuses_a_blank_identifier() -> None:
    """A blank id would file every blank-id scenario under one ledger entry."""
    with pytest.raises(AssertionError, match="carries neither `id` nor `name`"):
        scenario_id({"name": "", "id": "  "}, 1)


def test_scenarios_helper_books_a_scenario_the_body_replays() -> None:
    fixture = instrument(
        {
            "scenarios": [
                {"name": "first", "steps": []},
                {"name": "second", "steps": []},
            ]
        },
        name=_SELFTEST,
    )
    for scenario in scenarios(fixture):
        scenario["steps"]
    assert replayed_scenarios()[_SELFTEST] == {"first", "second"}

    reset(fixture=_SELFTEST)


def test_scenarios_helper_does_not_book_a_scenario_the_body_skips() -> None:
    """Yielding is not replaying — the hole a yield-time record would leave open.

    A generator cannot tell a body that ran from one that ``continue``d, so the
    booking rides on the scenario's *payload*: ``id``/``name`` are what a skip
    reads on the way past, ``steps``/``ops``/``expect`` are what a replay reads.
    """
    fixture = instrument(
        {
            "scenarios": [
                {"name": "first", "steps": []},
                {"name": "skipped", "steps": []},
            ]
        },
        name=_SELFTEST,
    )
    for scenario in scenarios(fixture):
        if scenario["name"] == "skipped":
            continue
        scenario["steps"]
    assert replayed_scenarios()[_SELFTEST] == {"first"}

    reset(fixture=_SELFTEST)


def test_scenarios_helper_needs_a_named_fixture() -> None:
    with pytest.raises(AssertionError, match="corpus-relative fixture path"):
        list(scenarios({"scenarios": [{"name": "x"}]}))


def test_a_skipped_scenario_is_reported(tmp_path: Path) -> None:
    """The defect this rung exists for: fixture opened, one scenario never run."""
    record_scenario(_SELFTEST, {"name": "replayed"})
    reported = _scenario_report(tmp_path, [{"name": "replayed"}, {"name": "skipped"}])
    assert reported, "a skipped scenario produced no report line"
    assert "'skipped'" in reported[0]
    assert "never replayed" in reported[0]

    reset(fixture=_SELFTEST)


def test_an_unidentified_corpus_scenario_is_a_failure(tmp_path: Path) -> None:
    """Not a notice (#lzspecscenarioids). It used to be, and that is the bug.

    A scenario the guard books by POSITION makes every ledger entry for that
    fixture order-dependent, so a corpus reorder rebinds them all with nothing
    turning red.
    """
    failures, _ = scenario_failures(
        [_SELFTEST], corpus=_corpus(tmp_path, [{"policy": "Sum"}])
    )
    reported = [line for line in failures if line.startswith(_SELFTEST)]
    assert reported and "carry neither `id` nor `name`" in reported[0]

    reset(fixture=_SELFTEST)


def test_excuse_scenario_satisfies_the_scenario(tmp_path: Path) -> None:
    excuse_scenario(_SELFTEST, "skipped", "no lease clock in this binding")
    assert not _scenario_report(tmp_path, [{"name": "skipped"}])

    reset(fixture=_SELFTEST)


def test_excuse_scenario_requires_a_reason() -> None:
    with pytest.raises(AssertionError, match="non-empty reason"):
        excuse_scenario(_SELFTEST, "skipped", "  ")
    reset(fixture=_SELFTEST)


def test_an_excuse_for_a_replayed_scenario_is_stale(tmp_path: Path) -> None:
    """Both directions: an excuse the run disproves hides nothing."""
    record_scenario(_SELFTEST, {"name": "replayed"})
    excuse_scenario(_SELFTEST, "replayed", "believed unexpressible")
    reported = _scenario_report(tmp_path, [{"name": "replayed"}])
    assert reported, "a stale scenario excuse produced no report line"
    assert "is stale" in reported[0]
    assert "believed unexpressible" in reported[0]

    reset(fixture=_SELFTEST)


def test_an_excuse_for_an_absent_scenario_has_rotted(tmp_path: Path) -> None:
    record_scenario(_SELFTEST, {"name": "replayed"})
    excuse_scenario(_SELFTEST, "renamed_upstream", "was unexpressible")
    reported = _scenario_report(tmp_path, [{"name": "replayed"}])
    assert reported, "a rotted scenario excuse produced no report line"
    assert "rotted" in reported[0]
    assert "renamed_upstream" in reported[0]

    reset(fixture=_SELFTEST)


def test_an_excuse_naming_a_missing_fixture_has_rotted(tmp_path: Path) -> None:
    excuse_scenario("selftest/gone.json", "whatever", "the corpus moved")
    failures, _ = scenario_failures([], corpus=tmp_path)
    reported = [line for line in failures if line.startswith("selftest/gone.json")]
    assert reported and "not in the canonical corpus" in reported[0]

    reset(fixture="selftest/gone.json")


def test_a_fixture_the_suite_never_opened_is_not_held_to_scenarios(
    tmp_path: Path,
) -> None:
    """Fixture-level gaps are ``KNOWN_UNCOVERED``'s verdict, not this rung's."""
    failures, _ = scenario_failures([], corpus=_corpus(tmp_path, [{"name": "x"}]))
    assert not [line for line in failures if line.startswith(_SELFTEST)]
