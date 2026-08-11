from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

import pytest
from conformance_assert import corpus_subdir, instrument
from conformance_assert import scenarios as replay_scenarios

from lazily.stdlib import (
    MAX_U64,
    RevisionBarrier,
    Timeout,
    TimeoutOperation,
    Timer,
    TimerError,
)


SPEC = corpus_subdir("stdlib")

#: The three stdlib fixtures, in the order the canonical runner replays them.
FIXTURES = ("timer.json", "timeout.json", "revision_barrier.json")


def clean(value: object) -> dict[str, Any]:
    return {key: item for key, item in asdict(value).items() if item is not None}


def load(name: str) -> dict[str, Any]:
    return instrument(json.loads((SPEC / name).read_text()), name=f"stdlib/{name}")


def load_plain(name: str) -> dict[str, Any]:
    """The same bytes, WITHOUT the assertion-key tracker.

    The independent interpreter (below) replays every scenario several times —
    once unperturbed and once per declared operator — and most of those replays
    are expected to DIVERGE from the fixture. Feeding those comparisons through
    the tracker would book keys as asserted on the strength of a run whose whole
    point is that it does not conform. The tracked reading of these fixtures is
    `test_stdlib_canonical_corpus`, which replays the production implementation.
    """
    return json.loads((SPEC / name).read_text())


# Assertions actually performed against the corpus, counted at the point of
# comparison so `assertion_floor` measures the RUN rather than the file
# (#lzpystdlibfloorsunread).
_ASSERTIONS_MADE = 0


def _assert_expect(actual: Any, step: dict[str, Any]) -> None:
    """Compare a replayed step against its declared `expect`, and count it."""
    global _ASSERTIONS_MADE
    expect = step["expect"]
    # Count KEYS compared, not steps. `isinstance(expect, dict)` is wrong here:
    # `scenarios()` hands out tracked views, so an `expect` block is a Mapping
    # but not a `dict`, and the naive check silently counted 1 per step (14 for
    # timer.json instead of 29) — which is itself the shape this item is about.
    _ASSERTIONS_MADE += len(expect) if hasattr(expect, "keys") else 1
    assert actual == expect


def replay_timer(steps: list[dict[str, Any]]) -> None:
    timer: Timer | None = None
    last: dict[str, Any]
    for step in steps:
        if step["op"] == "start":
            try:
                timer = Timer(step["now"], step["duration"])
                last = {"outcome": "pending", "deadline": timer.deadline}
            except TimerError as error:
                last = {"outcome": "unavailable", "reason": error.reason}
        elif step["op"] == "observe":
            assert timer is not None
            last = clean(timer.observe(step["now"]))
        else:
            # `observe` used to be the unnamed `else`, so any op the corpus grows
            # would have been replayed as an observe (#lzscenariobodyskip).
            raise AssertionError(f"unknown timer op {step['op']!r}")
        _assert_expect(last, step)


def replay_timeout(steps: list[dict[str, Any]]) -> None:
    timeout: Timeout[str] | None = None
    for step in steps:
        if step["op"] == "start":
            try:
                timeout = Timeout(step["now"], step["duration"])
                actual = {"outcome": "pending", "deadline": timeout.deadline}
            except TimerError as error:
                actual = {"outcome": "unavailable", "reason": error.reason}
        elif step["op"] == "poll":
            assert timeout is not None
            operation_calls = 0
            cancellation_calls = 0
            operation_state = step["operation"]
            operation_value = step.get("value")
            cancellation_state = step["cancellation"]

            def operation(
                operation_state: str = operation_state,
                operation_value: object = operation_value,
            ) -> TimeoutOperation[str]:
                nonlocal operation_calls
                operation_calls += 1
                if operation_state == "completed":
                    assert isinstance(operation_value, str)
                    return TimeoutOperation.completed(operation_value)
                if operation_state == "unavailable":
                    return TimeoutOperation.unavailable()
                if operation_state == "pending":
                    return TimeoutOperation.pending()
                # `pending` used to be the unnamed fallthrough, so a fixture
                # naming any other operation state was replayed as a pending
                # poll and reported green (#lzscenariobodyskip).
                raise AssertionError(
                    f"unknown timeout operation state {operation_state!r}"
                )

            def cancellation(cancellation_state: str = cancellation_state) -> str:
                nonlocal cancellation_calls
                cancellation_calls += 1
                return cancellation_state

            actual = clean(timeout.poll(step["now"], operation, cancellation))
            actual.update(
                operation_calls=operation_calls,
                cancellation_calls=cancellation_calls,
            )
        else:
            # `poll` used to be the unnamed `else` (#lzscenariobodyskip).
            raise AssertionError(f"unknown timeout op {step['op']!r}")
        _assert_expect(actual, step)


def replay_barrier(steps: list[dict[str, Any]]) -> None:
    barrier: RevisionBarrier | None = None
    for step in steps:
        calls = 0
        if step["op"] == "start":
            barrier = RevisionBarrier(
                step["revision"], step["required_revision"], step["deadline"]
            )
            value = barrier.receipt("")
        else:
            assert barrier is not None
            if step["op"] == "observe":
                cancellation_state = step["cancellation"]

                def cancellation(cancellation_state: str = cancellation_state) -> str:
                    nonlocal calls
                    calls += 1
                    return cancellation_state

                value = barrier.observe(step["now"], step["predicate"], cancellation)
            elif step["op"] == "register_recheck":
                value = barrier.register_recheck(
                    step["now"], step["observed_revision"], step["predicate"]
                )
            elif step["op"] == "advance":
                value = barrier.advance(step["revision"], step["predicate"])
            elif step["op"] == "dispose":
                value = barrier.dispose()
            elif step["op"] == "receipt":
                value = barrier.receipt(step["key"])
            else:
                # `receipt` used to be the unnamed `else`: any barrier op the
                # corpus grows was replayed as a receipt read against
                # `step["key"]` and its expectation checked against that
                # unrelated call (#lzscenariobodyskip).
                raise AssertionError(f"unknown revision-barrier op {step['op']!r}")
        actual = clean(value)
        if step["op"] == "observe":
            actual["cancellation_calls"] = calls
        _assert_expect(actual, step)


def test_stdlib_canonical_corpus() -> None:
    runners = {
        "stdlib_timer_v1": replay_timer,
        "stdlib_timeout_v1": replay_timeout,
        "stdlib_revision_barrier_v1": replay_barrier,
    }
    global _ASSERTIONS_MADE
    for name in FIXTURES:
        fixture = load(name)
        scenarios = {scenario["id"] for scenario in fixture["scenarios"]}

        _ASSERTIONS_MADE = 0
        replayed = 0
        for scenario in replay_scenarios(fixture):
            runners[fixture["feature"]](scenario["steps"])
            replayed += 1

        for mutation in fixture["mutations"]:
            assert mutation["must_fail"]
            assert set(mutation["must_fail"]) <= scenarios

        # The three floors are REQUIRED by schemas/stdlib-fixture.schema.json and
        # were read by nothing here — a repo-wide grep found zero references, so
        # `scenario_floor: 99` against a six-scenario fixture was green
        # (#lzpystdlibfloorsunread). They are the corpus's own anti-vacuity
        # budget: the one area whose fixtures carry an explicit "prove you did N
        # things" contract was the one area where nothing checked it.
        #
        # Each is compared against what this RUN did, not against the file:
        # `replayed` counts scenarios the ledger actually yielded, and
        # `_ASSERTIONS_MADE` is incremented inside `_assert_expect`, at the
        # comparison itself. A floor computed from the fixture would be a
        # tautology.
        assert replayed >= fixture["scenario_floor"], (
            f"{name}: replayed {replayed} scenarios, below the declared "
            f"scenario_floor {fixture['scenario_floor']}"
        )
        assert fixture["assertion_floor"] <= _ASSERTIONS_MADE, (
            f"{name}: made {_ASSERTIONS_MADE} assertions, below the declared "
            f"assertion_floor {fixture['assertion_floor']}"
        )
        # `mutation_floor` bounds the ledger's SIZE, and that is all it can do.
        # Whether each entry's central claim — "mutating the implementation this
        # way breaks exactly these scenarios" — actually holds is decided by
        # `test_every_declared_mutation_is_observed_by_the_independent_interpreter`
        # below, which applies every operator (#lzpystdlibmutants).
        assert len(fixture["mutations"]) >= fixture["mutation_floor"], (
            f"{name}: carries {len(fixture['mutations'])} mutations, below the "
            f"declared mutation_floor {fixture['mutation_floor']}"
        )


# ---------------------------------------------------------------------------
# The independent interpreter (#lzpystdlibmutants)
#
# Each fixture declares a `mutations` ledger: "mutate the implementation THIS
# named way and exactly these scenarios must fail". Nothing here applied any of
# it — the ledger was checked against the fixture's own scenario ids and against
# its own `mutation_floor`, which is a claim satisfied by its own bookkeeping
# (feedback_conformance_tests_drive_real_behavior_not_runner_bookkeeping). An
# operator rebound to a scenario it does not break stayed green.
#
# The reference shape is lazily-rs `tests/stdlib_conformance.rs`
# (`independent_failures(path, &fixture, Some(operator))`). The design point
# worth restating: the operator perturbs an INDEPENDENT model of the feature,
# never the shipped `lazily.stdlib` implementation. Mutating production code to
# test the corpus would test the mutation harness, would need the library to
# carry seams that exist only for tests, and would say nothing about whether the
# corpus can TELL a correct implementation from a wrong one — which is the only
# thing the ledger claims.
# ---------------------------------------------------------------------------


class Mutation:
    """The operator under test, consulted BY NAME at every perturbable branch.

    `consulted` is what makes an unimplemented operator loud rather than silent.
    A registry of "operators this file implements" would be one more piece of
    bookkeeping to drift; this set is produced by the branches the replay really
    evaluated, so an operator no arm knows about ends the run naming itself.
    """

    __slots__ = ("consulted", "operator")

    def __init__(self, operator: str | None) -> None:
        self.operator = operator
        self.consulted: set[str] = set()

    def __call__(self, name: str) -> bool:
        self.consulted.add(name)
        return self.operator == name


def _model_op(step: dict[str, Any], known: tuple[str, ...]) -> str:
    op = step.get("op")
    if op not in known:
        raise AssertionError(f"unknown model op {op!r} (known: {known}) in {step}")
    return str(op)


def _terminal(state: dict[str, Any], *, adapter_counts: bool) -> dict[str, Any]:
    """The latched observation: whatever this feature carries, plus no calls."""
    result: dict[str, Any] = {"outcome": state["status"]}
    for key in ("fired_at", "value", "reason"):
        if key in state:
            result[key] = state[key]
    if adapter_counts:
        result["operation_calls"] = 0
        result["cancellation_calls"] = 0
    return result


def _model_timer(
    state: dict[str, Any], step: dict[str, Any], mutated: Mutation
) -> dict[str, Any]:
    if _model_op(step, ("start", "observe")) == "start":
        deadline = step["now"] + step["duration"]
        if deadline > MAX_U64:
            state["status"] = "unavailable"
            state["reason"] = "deadline_overflow"
            return _terminal(state, adapter_counts=False)
        state.update(status="pending", deadline=deadline, last_now=step["now"])
        return {"outcome": "pending", "deadline": deadline}
    if mutated("fixture_bookkeeping"):
        return {"outcome": "pending", "deadline": state.get("deadline")}
    latched = mutated("terminal_not_latched")
    if state["status"] != "pending" and not latched:
        return _terminal(state, adapter_counts=False)
    if latched:
        state["status"] = "pending"
    now = step["now"]
    if now < state["last_now"]:
        return {
            "outcome": "unavailable",
            "reason": "clock_regression",
            "deadline": state["deadline"],
        }
    state["last_now"] = now
    deadline = state["deadline"]
    reached = now > deadline if mutated("deadline_strict_greater") else now >= deadline
    if not reached:
        return {"outcome": "pending", "deadline": deadline}
    state["status"] = "fired"
    state["fired_at"] = now
    return _terminal(state, adapter_counts=False)


def _model_timeout(
    state: dict[str, Any], step: dict[str, Any], mutated: Mutation
) -> dict[str, Any]:
    if _model_op(step, ("start", "poll")) == "start":
        deadline = step["now"] + step["duration"]
        if deadline > MAX_U64:
            state["status"] = "unavailable"
            state["reason"] = "deadline_overflow"
            return _terminal(state, adapter_counts=False)
        state.update(status="pending", deadline=deadline, last_now=step["now"])
        return {"outcome": "pending", "deadline": deadline}
    if mutated("fixture_bookkeeping"):
        return {
            "outcome": "pending",
            "deadline": state.get("deadline"),
            "operation_calls": 0,
            "cancellation_calls": 0,
        }
    latched = mutated("terminal_not_latched")
    if state["status"] != "pending" and not latched:
        return _terminal(state, adapter_counts=True)
    if latched:
        state["status"] = "pending"
    now = step["now"]
    deadline = state["deadline"]
    if now < state["last_now"]:
        state["status"] = "unavailable"
        state["reason"] = "clock_regression"
        return {
            "outcome": "unavailable",
            "reason": "clock_regression",
            "operation_calls": 0,
            "cancellation_calls": 0,
        }
    state["last_now"] = now
    reached = now > deadline if mutated("deadline_strict_greater") else now >= deadline
    if reached:
        state["status"] = "timed_out"
        return {
            "outcome": "timed_out",
            "operation_calls": 0,
            "cancellation_calls": 0,
        }
    # Both drive if-chains whose tail ASSUMES `pending`; validate the spelling so
    # an unknown one names itself instead of quietly meaning "pending"
    # (#lzscenariobodyskip).
    operation = step["operation"]
    if operation not in ("completed", "pending", "unavailable"):
        raise AssertionError(f"unknown operation {operation!r} in {step}")
    cancellation = step["cancellation"]
    if cancellation not in ("cancelled", "pending", "unavailable"):
        raise AssertionError(f"unknown cancellation {cancellation!r} in {step}")
    if mutated("cancellation_before_completion") and cancellation == "cancelled":
        state["status"] = "cancelled"
        return {"outcome": "cancelled", "operation_calls": 1, "cancellation_calls": 1}
    if operation == "completed":
        state["status"] = "completed"
        state["value"] = step["value"]
        return {
            "outcome": "completed",
            "value": step["value"],
            "operation_calls": 1,
            "cancellation_calls": 1,
        }
    if operation == "unavailable":
        state["status"] = "unavailable"
        state["reason"] = "operation_unavailable"
        return {
            "outcome": "unavailable",
            "reason": "operation_unavailable",
            "operation_calls": 1,
            "cancellation_calls": 1,
        }
    if cancellation == "cancelled":
        state["status"] = "cancelled"
        return {"outcome": "cancelled", "operation_calls": 1, "cancellation_calls": 1}
    if cancellation == "unavailable":
        state["status"] = "unavailable"
        state["reason"] = "cancellation_unavailable"
        return {
            "outcome": "unavailable",
            "reason": "cancellation_unavailable",
            "operation_calls": 1,
            "cancellation_calls": 1,
        }
    return {
        "outcome": "pending",
        "deadline": deadline,
        "operation_calls": 1,
        "cancellation_calls": 1,
    }


def _barrier_observation(state: dict[str, Any]) -> dict[str, Any]:
    result = {
        "outcome": state["status"],
        "revision": state["revision"],
        "generation": state["generation"],
    }
    if "reason" in state:
        result["reason"] = state["reason"]
    return result


def _model_barrier(
    state: dict[str, Any], step: dict[str, Any], mutated: Mutation
) -> dict[str, Any]:
    op = _model_op(
        step, ("start", "register_recheck", "advance", "observe", "dispose", "receipt")
    )
    if op == "start":
        state.update(
            status="pending",
            revision=step["revision"],
            generation=0,
            required=step["required_revision"],
            deadline=step["deadline"],
            last_now=None,
        )
        return _barrier_observation(state)
    if mutated("fixture_bookkeeping"):
        state["status"] = "pending"
        return _barrier_observation(state)
    latched = mutated("terminal_not_latched")
    if state["status"] != "pending" and not latched:
        result = _barrier_observation(state)
        if op == "observe":
            result["cancellation_calls"] = 0
        return result
    if latched:
        state["status"] = "pending"
    if op == "dispose":
        state["status"] = "disposed"
        return _barrier_observation(state)
    if op == "receipt":
        # An application-owned effect receipt is NOT barrier authority: it wakes
        # the waiter and changes no revision. The operator makes it authority.
        if mutated("receipt_is_authority"):
            state["revision"] = state["required"]
            state["generation"] += 1
            state["status"] = "satisfied"
        return _barrier_observation(state)
    if op == "advance":
        state["revision"] = max(state["revision"], step["revision"])
        state["generation"] += 1
        if state["revision"] >= state["required"] and step["predicate"] is True:
            state["status"] = "satisfied"
        return _barrier_observation(state)
    now = step["now"]
    regressed = state["last_now"] is not None and now < state["last_now"]
    if regressed and not mutated("barrier_accept_clock_regression"):
        state["status"] = "unavailable"
        state["reason"] = "clock_regression"
        result = _barrier_observation(state)
        if op == "observe":
            result["cancellation_calls"] = 0
        return result
    state["last_now"] = now
    if op == "register_recheck":
        state["generation"] += 1
        if not mutated("barrier_skip_post_registration_recheck"):
            state["revision"] = max(state["revision"], step["observed_revision"])
            if state["revision"] >= state["required"] and step["predicate"] is True:
                state["status"] = "satisfied"
        return _barrier_observation(state)
    deadline = state["deadline"]
    if deadline is None:
        reached = False
    elif mutated("deadline_strict_greater"):
        reached = now > deadline
    else:
        reached = now >= deadline
    if reached:
        state["status"] = "timed_out"
        result = _barrier_observation(state)
        result["cancellation_calls"] = 0
        return result
    if state["revision"] >= state["required"] and step["predicate"] is True:
        state["status"] = "satisfied"
        result = _barrier_observation(state)
        result["cancellation_calls"] = 0
        return result
    # Fail-closed tail (#lzscenariobodyskip): a cancellation spelling this model
    # does not know must not behave like `pending`.
    cancellation = step["cancellation"]
    if cancellation == "cancelled":
        state["status"] = "cancelled"
    elif cancellation == "unavailable":
        state["status"] = "unavailable"
        state["reason"] = "cancellation_unavailable"
    elif cancellation != "pending":
        raise AssertionError(f"unknown cancellation {cancellation!r} in {step}")
    result = _barrier_observation(state)
    result["cancellation_calls"] = 1
    return result


MODELS = {
    "stdlib_timer_v1": _model_timer,
    "stdlib_timeout_v1": _model_timeout,
    "stdlib_revision_barrier_v1": _model_barrier,
}


def independent_failures(
    fixture: dict[str, Any], operator: str | None
) -> tuple[set[str], set[str]]:
    """Replay every scenario through the model, perturbed by `operator`.

    Returns the ids that DIVERGED from their declared `expect`, and the operator
    names the replay's branches consulted.
    """
    model = MODELS[fixture["feature"]]
    mutated = Mutation(operator)
    failed: set[str] = set()
    for scenario in fixture["scenarios"]:
        state: dict[str, Any] = {}
        for step in scenario["steps"]:
            if model(state, step, mutated) != step["expect"]:
                failed.add(scenario["id"])
    return failed, mutated.consulted


def test_independent_model_agrees_with_the_unperturbed_corpus() -> None:
    """The non-vacuity control: unperturbed, the model reproduces every scenario.

    Without this a mutation proves nothing — a scenario that fails whether or not
    the operator is applied is not evidence that the operator broke it.
    """
    for name in FIXTURES:
        fixture = load_plain(name)
        assert fixture["scenarios"], f"{name}: no scenarios to replay"
        failed, _ = independent_failures(fixture, None)
        assert failed == set(), (
            f"stdlib/{name}: the independent model diverged from the canonical "
            f"corpus with NO operator applied, on {sorted(failed)}"
        )


def test_every_declared_mutation_is_observed_by_the_independent_interpreter() -> None:
    pairs = 0
    for name in FIXTURES:
        fixture = load_plain(name)
        baseline, _ = independent_failures(fixture, None)
        assert baseline == set(), f"stdlib/{name}: unperturbed replay already fails"
        assert fixture["mutations"], f"stdlib/{name}: empty mutation ledger"
        for mutation in fixture["mutations"]:
            operator = mutation["operator"]
            must_fail = set(mutation["must_fail"])
            assert must_fail, f"stdlib/{name}: {operator!r} names no scenario"
            failed, consulted = independent_failures(fixture, operator)
            # An operator with no interpreter arm is a HARD failure, never a
            # skip: a silently unimplemented operator is the same vacuity as a
            # ledger checked against itself.
            assert operator in consulted, (
                f"stdlib/{name}: mutation operator {operator!r} is declared by the "
                f"corpus but no arm of the independent interpreter implements it; "
                f"the replay consulted {sorted(consulted)}"
            )
            escaped = must_fail - failed
            assert not escaped, (
                f"stdlib/{name}: mutation {operator!r} did NOT break "
                f"{sorted(escaped)} — the ledger claims those scenarios detect it"
            )
            # Redundant given `baseline == set()`, but it names the PAIR rather
            # than the fixture when it fires.
            still_green = must_fail & baseline
            assert not still_green, (
                f"stdlib/{name}: {operator!r}/{sorted(still_green)} fail with the "
                f"operator applied AND without it, so the mutation proves nothing"
            )
            pairs += len(must_fail)
        # Every entry contributes at least one (operator, scenario) pair, so the
        # corpus's own `mutation_floor` is also a floor on what this run applied.
        assert pairs >= fixture["mutation_floor"]
    # timer 4 + timeout 5 + revision_barrier 6. A floor, not an equality: the
    # corpus may grow pairs, and this run must never apply fewer than it does
    # today (#lzpystdlibmutants).
    assert pairs >= 15, f"applied only {pairs} (operator, scenario) pairs"


def test_the_complement_is_not_asserted_because_the_corpus_does_not_support_it() -> (
    None
):
    """Some operators break scenarios their ledger entry does not name.

    The obvious complement — "a scenario NOT named in `must_fail` survives the
    operator" — is FALSE for this corpus, and asserting it would be inventing a
    claim the fixtures never make. `deadline_strict_greater` on `timer.json`
    also breaks `clock_regression_is_rejected_without_state_change`, whose final
    step observes exactly at the deadline; `must_fail` is a lower bound on
    detection ("these scenarios catch it"), not a partition. lazily-rs makes the
    same choice — `must_fail.is_subset(&failed)`, not equality.

    Recorded as a test rather than a comment so the day the corpus DOES become a
    partition, this stops being true and someone has to decide deliberately
    whether to tighten the assertion above.
    """
    fixture = load_plain("timer.json")
    failed, _ = independent_failures(fixture, "deadline_strict_greater")
    entry = next(
        mutation
        for mutation in fixture["mutations"]
        if mutation["operator"] == "deadline_strict_greater"
    )
    assert failed - set(entry["must_fail"]) == {
        "clock_regression_is_rejected_without_state_change"
    }


def test_async_adapters_are_caller_driven() -> None:
    async def run() -> None:
        timeout: Timeout[str] = Timeout(0, 10)
        calls: list[str] = []

        async def operation() -> TimeoutOperation[str]:
            calls.append("operation")
            return TimeoutOperation.completed("async")

        async def cancellation() -> str:
            calls.append("cancellation")
            return "pending"

        assert clean(await timeout.poll_async(2, operation, cancellation)) == {
            "outcome": "completed",
            "value": "async",
        }
        assert calls == ["operation", "cancellation"]

        barrier = RevisionBarrier(0, 1)

        async def cancelled() -> str:
            return "cancelled"

        assert (await barrier.observe_async(0, False, cancelled)).outcome == "cancelled"

    asyncio.run(run())


def test_public_logical_inputs_are_strict_uint64() -> None:
    with pytest.raises(TypeError):
        Timer(0.5, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Timer(True, 1)
    with pytest.raises(ValueError):
        Timer(-1, 1)

    timer = Timer(0, 1)
    with pytest.raises(ValueError):
        timer.observe(MAX_U64 + 1)

    timeout: Timeout[str] = Timeout(0, 10)
    with pytest.raises(TypeError):
        timeout.poll(  # type: ignore[arg-type]
            1.5,
            TimeoutOperation.pending,
            lambda: "pending",
        )

    with pytest.raises(ValueError):
        RevisionBarrier(MAX_U64 + 1, 1)
    with pytest.raises(TypeError):
        RevisionBarrier(0, 1, False)

    barrier = RevisionBarrier(0, 1)
    with pytest.raises(ValueError):
        barrier.observe(MAX_U64 + 1, False, lambda: "pending")
    with pytest.raises(TypeError):
        barrier.register_recheck(0, 0.5, False)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        barrier.advance(MAX_U64 + 1, False)


def test_barrier_clock_regression_skips_cancellation_and_latches() -> None:
    barrier = RevisionBarrier(0, 2)
    cancellation_calls = 0

    def pending() -> str:
        nonlocal cancellation_calls
        cancellation_calls += 1
        return "pending"

    assert barrier.observe(5, False, pending).outcome == "pending"
    regressed = barrier.observe(4, False, pending)
    assert clean(regressed) == {
        "outcome": "unavailable",
        "revision": 0,
        "generation": 0,
        "reason": "clock_regression",
    }
    assert cancellation_calls == 1

    registration = barrier.register_recheck(4, 2, True)
    assert registration.outcome == "unavailable"
    assert registration.revision == 0
    assert registration.generation == 0

    latched = barrier.advance(2, True)
    assert latched.outcome == "unavailable"
    assert latched.reason == "clock_regression"
    assert latched.revision == 0
    assert latched.generation == 0


def test_sync_adapters_preserve_reentrant_first_terminal_result() -> None:
    timeout: Timeout[str] = Timeout(0, 10)

    def operation() -> TimeoutOperation[str]:
        inner = timeout.poll(
            2,
            lambda: TimeoutOperation.completed("first"),
            lambda: "pending",
        )
        assert inner.outcome == "completed"
        return TimeoutOperation.pending()

    outer = timeout.poll(1, operation, lambda: "cancelled")
    assert outer.outcome == "completed"
    assert outer.value == "first"

    barrier = RevisionBarrier(0, 1)

    def dispose_then_cancel() -> str:
        assert barrier.dispose().outcome == "disposed"
        return "cancelled"

    assert barrier.observe(0, False, dispose_then_cancel).outcome == "disposed"
    assert barrier.receipt("").outcome == "disposed"


def test_async_adapters_start_together_and_preserve_reentrant_terminal() -> None:
    async def run() -> None:
        timeout: Timeout[str] = Timeout(0, 10)
        never = asyncio.Event()
        calls: list[str] = []

        async def operation() -> TimeoutOperation[str]:
            calls.append("operation")
            await never.wait()
            return TimeoutOperation.pending()

        async def cancellation() -> str:
            calls.append("cancellation")
            return "cancelled"

        task = asyncio.create_task(timeout.poll_async(1, operation, cancellation))
        for _ in range(3):
            await asyncio.sleep(0)
            if len(calls) == 2:
                break
        assert calls == ["operation", "cancellation"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        reentrant: Timeout[str] = Timeout(0, 10)

        async def reentrant_operation() -> TimeoutOperation[str]:
            reentrant.poll(
                2,
                lambda: TimeoutOperation.completed("first"),
                lambda: "pending",
            )
            return TimeoutOperation.pending()

        result = await reentrant.poll_async(
            1,
            reentrant_operation,
            cancellation,
        )
        assert result.outcome == "completed"
        assert result.value == "first"

        barrier = RevisionBarrier(0, 1)

        async def dispose_then_cancel() -> str:
            barrier.dispose()
            return "cancelled"

        assert (
            await barrier.observe_async(0, False, dispose_then_cancel)
        ).outcome == "disposed"

    asyncio.run(run())
