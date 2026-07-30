"""The transport-agnostic ingress contract replayed against every Python flavor.

``#designimplementtransport``. lazily-py ships all three shells — ``IngressCell``
/ ``ThreadSafeIngressCell`` / ``AsyncIngressCell`` — matching the three
``coverage.json`` ingress rows and the contract
``lazily-spec/docs/transport-ingress.md`` declares REQUIRED of every binding in
every flavor.

The flavor axis lives in the **runner**, not the corpus: the fixtures carry a
``model`` naming the primitive and no execution-model field, and one replay
function drives the same JSON against each shell. Nothing below is
async-coloured, which is the finding rather than an oversight — an admission
decision is a function of the fence, the watermark, the reorder buffer, and the
observed clock, so there is nothing to await and no ``settle`` step anywhere.

Three things keep this suite from reporting green while testing nothing, each one
a failure mode this family of suites has actually shipped:

* :func:`test_unshipped_flavors_are_really_absent` greps ``src/lazily`` for each
  flavor's class definition, in **both** directions. A ledger row marked shipped
  whose class does not exist fails; a class that exists while its row says
  unshipped fails and names the runner to extend. The ledger cannot rot, because
  the filesystem enforces it.
* Every replay returns its step count, and every flavor asserts that count is
  non-zero and equal to the corpus total. An absence guard proves the fixtures
  exist on disk; only a positive count proves this process opened them.
* ``invalidates`` is asserted in **both** directions through a cache-validity
  probe per reader kind. A step expecting ``False`` fails if the shell
  invalidated anyway, so over-invalidation is as visible as under-. Receipts are
  asserted **per channel** and never by receipt *count*: a stale cache recomputes
  to the right count, so a count-only gate reports green.

Mutation-check record (each defect introduced, the gate run, the defect reverted
with an mtime bump — a restore that preserves mtime lets a build system reuse the
mutated artifact and report a false green). All seven were killed:

* fence checked after dedupe → ``ingress_generation_handoff`` reports
  ``duplicate_sequence`` where the corpus expects ``stale_generation``.
* handoff keeps the superseded window → ``ingress_generation_handoff`` fails on
  the window value.
* ``Buffered`` marks every reader dirty → every ``invalidates: false`` step in
  ``ingress_reorder_and_duplication`` fails, in all three flavors.
* ``tick`` marks readiness unconditionally → ``ingress_freshness_and_retry``
  fails on the in-horizon tick.
* ``BLOCK`` advances the watermark → ``ingress_backpressure``'s final step
  reports a duplicate instead of an accept.
* the thread-safe shell clears outside ``batch()`` → the frontier-walk gate
  (:func:`test_one_admission_is_one_frontier_walk`) sees two effect runs for one
  admission.
* the error-receipt channel is never cleared → the replay fails on
  ``invalidates.receipts.error``. This one is why ``invalidates`` is asserted per
  channel rather than by receipt COUNT.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import lazily


_SPEC = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "ingress"
_SRC = Path(__file__).resolve().parents[1] / "src" / "lazily"

#: Every fixture the ingress corpus ships. Named explicitly rather than globbed:
#: a fixture added to the corpus and not to this list is a *missing replay*, and
#: the conformance-coverage guard is what should notice, not a silently shorter
#: run.
FIXTURES = (
    "ingress_ordered_delivery.json",
    "ingress_reorder_and_duplication.json",
    "ingress_reorder_window_overflow.json",
    "ingress_disconnect_replay.json",
    "ingress_backpressure.json",
    "ingress_generation_handoff.json",
    "ingress_freshness_and_retry.json",
)

READER_KINDS = ("value", "readiness", "authority", "retry")
RECEIPT_CHANNELS = ("accepted", "dropped", "error")


class _Flavor:
    name: str
    cls: type[Any]


class SyncFlavor(_Flavor):
    name = "sync"
    cls = lazily.IngressCell


class ThreadSafeFlavor(_Flavor):
    name = "thread-safe"
    cls = lazily.ThreadSafeIngressCell


class AsyncFlavor(_Flavor):
    name = "async"
    cls = lazily.AsyncIngressCell


FLAVORS = (SyncFlavor, ThreadSafeFlavor, AsyncFlavor)

#: One ledger row per (primitive, flavor) pair this binding claims:
#: (primitive, flavor, source marker, shipped).
LEDGER = (
    ("ingress", "sync", "class IngressCell", True),
    ("ingress", "thread-safe", "class ThreadSafeIngressCell", True),
    ("ingress", "async", "class AsyncIngressCell", True),
)

MERGE_POLICIES = {
    "sum": lazily.Sum,
    "keep_latest": lazily.KeepLatest,
    "max": lazily.Max,
    "set_union": lazily.SetUnion,
    "raw_fifo": lazily.RawFifo,
}


# ---------------------------------------------------------------------------
# Fixture decoding
# ---------------------------------------------------------------------------


def _corpus_present() -> bool:
    return _SPEC.is_dir()


def _load(name: str) -> dict[str, Any]:
    if not _corpus_present():
        pytest.skip(f"canonical ingress corpus not found at {_SPEC}")
    path = _SPEC / name
    # A missing fixture inside a corpus that DOES exist is drift, not an absent
    # sibling checkout, so it fails rather than skips.
    assert path.exists(), f"canonical ingress fixture missing: {name}"
    return json.loads(path.read_text())


def _policy_of(raw: dict[str, Any]) -> lazily.IngressPolicy:
    return lazily.IngressPolicy(
        reorder_window=raw["reorder_window"],
        freshness_horizon=raw["freshness_horizon"],
        high_water=raw["high_water"],
        overflow=lazily.Overflow[raw["overflow"].upper()],
        receipt_capacity=raw["receipt_capacity"],
        retry_base=raw["retry_base"],
        retry_ceiling=raw["retry_ceiling"],
    )


def _merge_of(name: str) -> Any:
    policy = MERGE_POLICIES.get(name)
    assert policy is not None, f"unknown merge policy {name!r}"
    return policy


def _keys_of(fixture: dict[str, Any]) -> list[str]:
    """Every key the fixture ever mentions.

    A reader must exist (and be probed) from the first step: an absent reader
    would silently pass a ``False`` invalidation expectation.
    """
    keys: list[str] = []
    for step in fixture["steps"]:
        candidates = [step["op"].get("key")]
        candidates.extend(step.get("expected", {}).get("scopes", {}))
        for key in candidates:
            if isinstance(key, str) and key not in keys:
                keys.append(key)
    return keys


def _replay_of(raw: Any) -> lazily.ReplayRequest | None:
    if raw is None:
        return None
    return lazily.ReplayRequest(raw["generation"], raw["from_sequence"])


def _admission_summary(admission: lazily.IngressAdmission) -> dict[str, Any]:
    """Project an admission onto the fixture's ``returns`` shape.

    Comparing projections rather than whole records keeps the corpus free of
    fields it deliberately does not pin (a handoff's watermark, for instance).
    """
    kind = admission.kind
    if kind in {
        lazily.IngressAdmissionKind.ACCEPTED,
        lazily.IngressAdmissionKind.CONFLATED,
    }:
        return {
            "admission": str(kind),
            "delivered_through": admission.delivered_through,
        }
    if kind == lazily.IngressAdmissionKind.BUFFERED:
        return {"admission": "buffered", "gap_from": admission.gap_from}
    if kind == lazily.IngressAdmissionKind.GENERATION_HANDOFF:
        return {
            "admission": "generation_handoff",
            "from": admission.from_generation,
            "to": admission.to_generation,
        }
    if kind == lazily.IngressAdmissionKind.DROPPED:
        return {"admission": "dropped", "reason": str(admission.reason)}
    return {"admission": "blocked"}


# ---------------------------------------------------------------------------
# The cache-validity probe
# ---------------------------------------------------------------------------


def _snapshot(cell: Any, keys: list[str]) -> dict[str, bool]:
    """Cache-validity of every reader kind the fixture can speak about."""
    snapshot: dict[str, bool] = {}
    for key in keys:
        snapshot[f"{key}.value"] = cell.value_is_valid(key)
        snapshot[f"{key}.readiness"] = cell.readiness_is_valid(key)
        snapshot[f"{key}.authority"] = cell.authority_is_valid(key)
        snapshot[f"{key}.retry"] = cell.retry_is_valid(key)
    snapshot["receipts.accepted"] = cell.accepted_is_valid()
    snapshot["receipts.dropped"] = cell.dropped_is_valid()
    snapshot["receipts.error"] = cell.errors_is_valid()
    return snapshot


def _materialize(cell: Any, keys: list[str]) -> None:
    """Read every reader kind, so the caches are warm and the next step's probe
    measures *that step's* invalidation and nothing else."""
    for key in keys:
        cell.value(key)
        cell.readiness(key)
        cell.authority(key)
        cell.retry(key)
    cell.accepted()
    cell.dropped()
    cell.errors()
    cell.schedule()


def _assert_invalidation(
    where: str,
    want: dict[str, Any],
    before: dict[str, bool],
    after: dict[str, bool],
) -> None:
    """Assert ``invalidates`` in both directions.

    ``True`` means the reader's cache went from valid to invalid across the op;
    ``False`` means it stayed valid.
    """
    for key, want_scope in want["scopes"].items():
        for kind in READER_KINDS:
            probe = f"{key}.{kind}"
            expected = want_scope[kind]
            invalidated = before[probe] and not after[probe]
            assert invalidated is expected, (
                f"{where}: {probe} invalidation is {invalidated}, expected "
                f"{expected} (was valid={before[probe]}, now valid={after[probe]})"
            )
    for channel in RECEIPT_CHANNELS:
        probe = f"receipts.{channel}"
        expected = want["receipts"][channel]
        invalidated = before[probe] and not after[probe]
        assert invalidated is expected, (
            f"{where}: {probe} invalidation is {invalidated}, expected {expected}"
        )


# ---------------------------------------------------------------------------
# State assertions
# ---------------------------------------------------------------------------


def _assert_scope_state(where: str, cell: Any, key: str, want: dict[str, Any]) -> None:
    view = cell.view(key)
    assert view is not None, f"{where}: scope {key!r} absent"
    assert view.lifecycle == want["lifecycle"], f"{where}: {key} lifecycle"
    assert view.generation == want["generation"], f"{where}: {key} generation"
    assert view.delivered_through == want["delivered_through"], (
        f"{where}: {key} watermark"
    )
    assert view.buffered == want["buffered"], f"{where}: {key} buffered"
    assert view.consecutive_errors == want["consecutive_errors"], (
        f"{where}: {key} consecutive errors"
    )
    assert cell.value(key) == want["window"], f"{where}: {key} window"
    assert cell.readiness(key) == want["readiness"], f"{where}: {key} readiness"

    want_authority = want["authority"]
    authority = cell.authority(key)
    if want_authority is None:
        assert authority is None, f"{where}: {key} authority"
    else:
        assert authority == lazily.IngressAuthority(
            want_authority["generation"],
            want_authority["delivered_through"],
            want_authority["stamped_at"],
        ), f"{where}: {key} authority"

    want_retry = want["retry"]
    retry = cell.retry(key)
    if want_retry is None:
        assert retry is None, f"{where}: {key} retry"
    else:
        assert retry == lazily.IngressRetry(
            want_retry["attempt"], want_retry["backoff"], want_retry["resume_from"]
        ), f"{where}: {key} retry"


def _assert_state(where: str, cell: Any, expected: dict[str, Any]) -> None:
    for key, want in expected["scopes"].items():
        _assert_scope_state(where, cell, key, want)
    receipts = expected["receipts"]
    assert len(cell.accepted()) == receipts["accepted"], f"{where}: accepted receipts"
    assert len(cell.dropped()) == receipts["dropped"], f"{where}: dropped receipts"
    assert len(cell.errors()) == receipts["error"], f"{where}: error receipts"


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


def _apply_op(where: str, cell: Any, op: dict[str, Any]) -> dict[str, Any] | None:
    kind = op["type"]
    if kind == "admit":
        admission = cell.admit(
            lazily.IngressEnvelope(
                op["key"],
                op["generation"],
                op["sequence"],
                op["stamped_at"],
                op["payload"],
            )
        )
        return _admission_summary(admission)
    if kind == "open":
        cell.open(op["key"], op["generation"])
        return None
    if kind == "drain":
        return {"drained": cell.drain(op["key"])}
    if kind == "suspend":
        request = cell.suspend(op["key"])
        return {"replay": request}
    if kind == "reconnect":
        request = cell.reconnect(op["key"], op["generation"])
        return {"replay": request}
    if kind == "close":
        cell.close(op["key"])
        return None
    if kind == "fail":
        cell.fail(op["key"], lazily.IngressError(op["error"]))
        return None
    if kind == "tick":
        cell.tick(op["now"])
        return None
    raise AssertionError(f"{where}: unknown ingress op {kind!r}")


def _assert_returns(where: str, actual: dict[str, Any] | None, want: Any) -> None:
    if "replay" in want:
        assert actual is not None
        assert actual["replay"] == _replay_of(want["replay"]), (
            f"{where}: replay request {actual['replay']!r}"
        )
        return
    assert actual == want, f"{where}: returns {actual!r}, expected {want!r}"


def _replay(flavor: type[_Flavor], fixture_name: str) -> int:
    """Replay one fixture against one flavor.

    Returns the number of steps executed, so a caller can prove this process
    really opened the corpus rather than merely naming it.
    """
    fixture = _load(fixture_name)
    assert fixture["model"] == "IngressCell", f"{fixture_name}: fixture model"
    ctx: dict = {}
    cell = flavor.cls(
        ctx,
        _policy_of(fixture["policy"]),
        _merge_of(fixture["merge"]),
        transport=lazily.IngressTransportKind(fixture["transport"]),
        poll_interval=fixture["poll_interval"],
    )
    keys = _keys_of(fixture)
    assert keys, f"{fixture_name}: fixture names no scope keys"
    _materialize(cell, keys)

    steps = fixture["steps"]
    assert steps, f"{flavor.name} {fixture_name}: fixture has no steps"
    for index, step in enumerate(steps):
        where = f"{flavor.name} {fixture_name} step {index} ({step['op']['type']})"
        expected = step.get("expected")
        assert expected is not None, f"{where}: expected block is missing"
        invalidates = expected.get("invalidates")
        assert invalidates is not None, f"{where}: invalidation matrix is missing"

        before = _snapshot(cell, keys)
        actual = _apply_op(where, cell, step["op"])
        # The probe must be taken BEFORE the state assertions: reading a reader
        # re-warms its cache, which would erase the very invalidation under test.
        after = _snapshot(cell, keys)
        if "returns" in step:
            _assert_returns(where, actual, step["returns"])
        _assert_state(where, cell, expected)
        _assert_invalidation(where, invalidates, before, after)
        _materialize(cell, keys)

    return len(steps)


def _expected_step_total() -> int:
    return sum(len(_load(name)["steps"]) for name in FIXTURES)


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def test_corpus_is_present_and_non_trivial() -> None:
    if not _corpus_present():
        pytest.skip(f"canonical ingress corpus not found at {_SPEC}")
    for name in FIXTURES:
        assert (_SPEC / name).exists(), f"canonical ingress fixture missing: {name}"
    total = _expected_step_total()
    assert total >= 30, (
        f"the ingress corpus replays only {total} steps; "
        "that is not the named schedule set"
    )


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_ingress_fixture_all_flavors(flavor: type[_Flavor], fixture_name: str) -> None:
    steps = _replay(flavor, fixture_name)
    assert steps > 0, f"{flavor.name} {fixture_name}: replayed zero steps"


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_every_flavor_replays_the_whole_corpus(flavor: type[_Flavor]) -> None:
    """The positive count. An absence guard proves the fixtures exist on disk;
    only this proves the flavor was driven through all of them."""
    steps = sum(_replay(flavor, name) for name in FIXTURES)
    assert steps > 0, f"{flavor.name}: replayed zero steps"
    assert steps == _expected_step_total(), (
        f"{flavor.name}: replayed {steps} steps; the corpus has "
        f"{_expected_step_total()}"
    )


def test_unshipped_flavors_are_really_absent() -> None:
    """The ledger cannot rot: the filesystem enforces it, in both directions."""
    sources = "\n".join(path.read_text() for path in sorted(_SRC.rglob("*.py")))
    assert sources, "read no sources from src/lazily; the ledger check is vacuous"
    assert len(LEDGER) == 3, "one row per flavor this family defines"
    assert any(shipped for *_, shipped in LEDGER), (
        "a ledger of nothing-shipped is not coverage"
    )
    for primitive, flavor, marker, shipped in LEDGER:
        assert (marker in sources) is shipped, (
            f"{primitive}/{flavor}: ledger says shipped={shipped} but source "
            f"marker {marker!r} present={marker in sources}; fix the ledger or "
            "extend this runner"
        )


def test_ledger_covers_every_flavor_the_runner_drives() -> None:
    """The other direction of the same claim: a flavor in ``FLAVORS`` with no
    ledger row would be replayed but unaccounted for."""
    claimed = {flavor for _, flavor, _, shipped in LEDGER if shipped}
    assert claimed == {flavor.name for flavor in FLAVORS}


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_the_invalidation_probe_discriminates(flavor: type[_Flavor]) -> None:
    """The corpus asserts negative invalidation, so the probe itself must be able
    to fail. Reading warms the cache, an op that dirties the reader clears it, and
    one that does not leaves it warm."""
    ctx: dict = {}
    cell = flavor.cls(ctx, lazily.IngressPolicy(), lazily.Sum)
    key = "alpha"

    assert not cell.value_is_valid(key), "an unread reader has no cached value"
    cell.value(key)
    assert cell.value_is_valid(key), "reading warms the cache"

    cell.admit(lazily.IngressEnvelope(key, 1, 0, 0, 1))
    assert not cell.value_is_valid(key), "a delivery must invalidate the value reader"

    cell.value(key)
    cell.admit(lazily.IngressEnvelope(key, 1, 5, 0, 1))
    assert cell.value_is_valid(key), (
        "a buffered envelope must NOT invalidate the value reader"
    )


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_one_admission_is_one_frontier_walk(flavor: type[_Flavor]) -> None:
    """A generation handoff must never be observable as "new value, old
    authority": every reader the transition dirtied is cleared in ONE coalesced
    wave, so an Effect reading both runs exactly once."""
    ctx: dict = {}
    cell = flavor.cls(ctx, lazily.IngressPolicy(), lazily.Sum)
    cell.admit(lazily.IngressEnvelope("alpha", 1, 0, 0, 5))
    value = cell.value_handle("alpha")
    authority = cell.authority_handle("alpha")
    seen: list[tuple[Any, Any]] = []

    def observe(view: Any) -> None:
        window = value(view)
        claim = authority(view)
        seen.append((window, None if claim is None else claim.generation))

    eff = lazily.Effect(observe)
    eff(ctx)
    assert seen == [(5, 1)]

    cell.admit(lazily.IngressEnvelope("alpha", 2, 0, 0, 9))
    assert seen == [(5, 1), (9, 2)], (
        "value and authority must land together — one frontier walk, one run"
    )
    eff.dispose()


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_scopes_do_not_invalidate_each_other(flavor: type[_Flavor]) -> None:
    ctx: dict = {}
    cell = flavor.cls(ctx, lazily.IngressPolicy(), lazily.Sum)
    cell.admit(lazily.IngressEnvelope("alpha", 1, 0, 0, 1))
    cell.value("alpha")
    cell.admit(lazily.IngressEnvelope("beta", 1, 0, 0, 2))
    cell.close("beta")
    assert cell.value_is_valid("alpha"), "another scope's traffic is not a change here"
    assert cell.value("alpha") == 1


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_schedule_derives_from_the_transport_and_retunes_live(
    flavor: type[_Flavor],
) -> None:
    ctx: dict = {}
    cell = flavor.cls(
        ctx,
        lazily.IngressPolicy(),
        lazily.Sum,
        transport=lazily.IngressTransportKind.EVENT_CHANNEL,
        poll_interval=25,
    )
    assert cell.schedule().poll_interval is None
    cell.set_transport(lazily.IngressTransportKind.BOUNDED_POLLING)
    assert cell.schedule().poll_interval == 25
    cell.set_poll_interval(200)
    assert cell.schedule().poll_interval == 200
    cell.set_poll_interval(0)
    assert cell.schedule().poll_interval == 1, "a zero interval is an unbounded loop"
    cell.set_transport(lazily.IngressTransportKind.RPC_TRIGGERED)
    assert cell.schedule().poll_interval is None


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_pump_admits_a_batch_and_requests_replay_for_a_surviving_gap(
    flavor: type[_Flavor],
) -> None:
    ctx: dict = {}
    cell = flavor.cls(ctx, lazily.IngressPolicy(), lazily.Sum)
    transport: lazily.InProcIngress[str, int] = lazily.InProcIngress(
        lazily.IngressTransportKind.EVENT_CHANNEL
    )
    transport.push(lazily.IngressEnvelope("alpha", 1, 0, 0, 1))
    transport.push(lazily.IngressEnvelope("alpha", 1, 2, 0, 4))

    outcomes = cell.pump(transport)
    assert len(outcomes) == 2
    assert outcomes[0].is_delivered()
    assert outcomes[1] == lazily.IngressAdmission.buffered(1)
    assert transport.replays() == [("alpha", lazily.ReplayRequest(1, 1))]

    transport.push(lazily.IngressEnvelope("alpha", 1, 1, 0, 2))
    cell.pump(transport)
    assert cell.value("alpha") == 7
    assert len(transport.replays()) == 1, "a closed gap asks for nothing more"


@pytest.mark.parametrize("flavor", FLAVORS, ids=lambda flavor: flavor.name)
def test_a_polling_transport_cannot_serve_a_replay(flavor: type[_Flavor]) -> None:
    ctx: dict = {}
    cell = flavor.cls(ctx, lazily.IngressPolicy(), lazily.Sum)
    transport: lazily.InProcIngress[str, int] = lazily.InProcIngress(
        lazily.IngressTransportKind.BOUNDED_POLLING
    )
    transport.push(lazily.IngressEnvelope("alpha", 1, 3, 0, 1))
    cell.pump(transport)
    assert transport.replays() == []


def test_conflate_is_rejected_for_a_non_conflating_algebra() -> None:
    with pytest.raises(lazily.IngressConfigError) as excinfo:
        lazily.IngressCell(
            {},
            lazily.IngressPolicy(overflow=lazily.Overflow.CONFLATE),
            lazily.RawFifo,
        )
    assert str(excinfo.value) == lazily.IngressConfigError.CONFLATE_NOT_BOUNDING


def test_zero_receipt_capacity_is_rejected() -> None:
    with pytest.raises(lazily.IngressConfigError) as excinfo:
        lazily.IngressCell({}, lazily.IngressPolicy(receipt_capacity=0), lazily.Sum)
    assert str(excinfo.value) == lazily.IngressConfigError.ZERO_RECEIPT_CAPACITY


def test_receipts_are_bounded_and_offsets_stay_monotone() -> None:
    ctx: dict = {}
    cell: lazily.IngressCell[str, int] = lazily.IngressCell(
        ctx, lazily.IngressPolicy(receipt_capacity=2), lazily.Sum
    )
    for sequence in range(4):
        cell.admit(lazily.IngressEnvelope("alpha", 1, sequence, 0, 1))
    accepted = cell.accepted()
    assert len(accepted) == 2, "the log is bounded"
    assert [receipt.offset for receipt in accepted] == [2, 3], (
        "offsets survive eviction, so a consumer can tell 'seen everything' from "
        "'the log wrapped'"
    )


def test_a_drain_is_an_egress_not_an_ack() -> None:
    ctx: dict = {}
    cell: lazily.IngressCell[str, int] = lazily.IngressCell(
        ctx, lazily.IngressPolicy(), lazily.Sum
    )
    cell.admit(lazily.IngressEnvelope("alpha", 1, 0, 0, 3))
    assert cell.drain("alpha") == 3
    view = cell.view("alpha")
    assert view is not None
    assert view.delivered_through == 0, "a drain never moves the watermark"
    assert cell.suspend("alpha") == lazily.ReplayRequest(1, 1)


def test_out_of_order_arrival_converges_to_the_in_order_fold() -> None:
    """The reordering tax is paid by the buffer, not by the algebra: for any
    arrival permutation of a contiguous run, the drained window equals the
    in-order fold. ``Sum`` is merely associative here."""
    permutations = (
        (0, 1, 2, 3),
        (3, 2, 1, 0),
        (1, 0, 3, 2),
        (2, 0, 1, 3),
        (0, 3, 1, 2),
    )
    for order in permutations:
        ctx: dict = {}
        cell: lazily.IngressCell[str, int] = lazily.IngressCell(
            ctx, lazily.IngressPolicy(), lazily.Sum
        )
        for sequence in order:
            cell.admit(lazily.IngressEnvelope("alpha", 1, sequence, 0, 1 << sequence))
        assert cell.value("alpha") == 1 + 2 + 4 + 8, f"order {order}"
        view = cell.view("alpha")
        assert view is not None and view.delivered_through == 3, f"order {order}"


def test_thread_safe_flavor_serializes_concurrent_admissions() -> None:
    """The thread-safe row is a claim about concurrency, so exercise it as one:
    128 envelopes admitted from 8 threads must all land, with the coalesced window
    equal to the in-order fold."""
    from concurrent.futures import ThreadPoolExecutor

    ctx: dict = {}
    cell: lazily.ThreadSafeIngressCell[str, int] = lazily.ThreadSafeIngressCell(
        ctx, lazily.IngressPolicy(reorder_window=256), lazily.Sum
    )

    def admit(sequence: int) -> lazily.IngressAdmission:
        return cell.admit(lazily.IngressEnvelope("alpha", 1, sequence, 0, 1))

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(admit, range(128)))
    assert all(
        outcome.is_delivered() or outcome.kind == lazily.IngressAdmissionKind.BUFFERED
        for outcome in outcomes
    )
    view = cell.view("alpha")
    assert view is not None
    assert view.delivered_through == 127, "every sequence landed in order"
    assert view.buffered == 0
    assert cell.value("alpha") == 128
