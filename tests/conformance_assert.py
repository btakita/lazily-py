"""Unconsumed-assertion-key guard for the conformance runners (#lzassertunknownkeys).

The coverage guard in ``scripts/check-conformance-coverage.sh`` proves a canonical
fixture's bytes were *read*. That is one level too shallow. A runner can read the
fixture, round-trip its ``wire`` frame, report the fixture as replayed — and never
look at the one assertion key the fixture exists for. Every named key the runner
does not recognise falls through silently; the suite is green and the assertion
proved nothing.

The concrete instance: ``delta_zero_copy_arrow.json`` carries a
``first_op_payload_backend`` discriminator. A runner that reads ``epoch`` and
``op_count`` and ignores the rest passes while never testing the backend at all.
Adding the missing check fixes one fixture. It does not fix the property, and the
property is what bites — a key no binding implements is invisibly skipped in all
nine at once.

This module makes the *unconsumed* key an error. Wrap an assertion/expectation
block with :func:`tracked`; every read through ``[]``, ``.get()``, ``in``, or a
generic iteration marks the key consumed. At the end of the session
``conftest.pytest_sessionfinish`` fails the run when a key present in a fixture was
never consumed by any runner, naming the fixture, the block, and the key.

Consumption is aggregated per ``(fixture, block)`` across the whole session, not
per test. Loading a fixture twice — a dedicated assertion test plus a parametrized
round-trip sweep — is normal, and the question worth answering is the session-wide
one: did *anything* in this suite check that key.

Python makes the silent path especially easy to reach: ``block.get("key")`` on a
key the fixture never had returns ``None`` and asserts nothing, and ``**block``
splats whatever is there into a callee that ignores extras. The tracker is the
same instrument for both — it reports on what was *taken out* of the block, not on
what the runner claims to look for.

Read is not asserted (#lzconsumednotasserted)
---------------------------------------------
Proving a key was *read* is still one rung short. A runner can read a key and then
do nothing with it — three shapes seen in the wild:

1. a named skip inside a loop that iterates the block (the iteration marks every
   key consumed, then the body ``continue``s past the interesting one);
2. a value bound to a local that no later comparison uses;
3. a comparison against a *literal* rather than against the fixture's own value,
   so the fixture field gates a branch instead of being checked.

So the ledger records a second set: which keys reached a comparison against the
fixture's value. The only ways in are :func:`assert_key` (equality against the
fixture value), :func:`assert_key_with` (any predicate, handed the fixture value),
and whole-block equality — an arm that compares against a hand-written constant
never marks the key, because the fixture's value never reached the comparison.

Where a key genuinely cannot be checked at the call site — the binding proves the
fact elsewhere, or the field is a discriminator selecting a code path rather than a
value — :func:`excuse_key` records the key and a non-empty reason. Excuses run in
**both** directions, exactly like the ``KNOWN_UNCOVERED`` coverage allowlist:
excusing a key the same session also asserts is a failure, because the excuse has
gone stale and is now hiding nothing.

Prose keys
----------
A few canonical fixtures carry human-readable narration inside an assertion block
(``note``, ``reason``, and similar). Those are not assertions and cannot be
"implemented". Declare them per call site with ``prose=(...)`` so the exemption is
visible next to the runner that takes it, rather than buried in a global
allowlist. Prose is exempt from all three verdicts. Everything else must be
consumed *and* asserted (or excused) or the guard fails.

Declared prose keys are DISCHARGED, not excused (#lzprosekeyconvention)
-----------------------------------------------------------------------
The paragraph above describes narration a runner may ignore. A *declared* prose
key is the opposite: it states an OBLIGATION in English, and the corpus now says
which keys those are, in the block's own ``assertions.prose`` array. lazily-spec
``docs/conformance.md`` § Prose assertion keys is normative; this module is the
lazily-py half of it.

Replaying ``blob_backend_discriminator.json`` v2 produced four different
treatments of the same four paragraphs across the nine bindings. lazily-py's was
the second-best of the four — it excused each one with an individually-worded
reason naming the assertion that discharges it, which is falsifiable in
principle and was checked by nothing. The convention makes exactly that claim
checkable, and deletes the free-text form so there are not two paths to satisfy
one key.

A declared prose key is satisfied by :func:`prose_key`, which names the
executable assertion keys that carry its obligation, and by nothing else. The
tracker fails the run when:

1. a declared prose key is ASSERTED — comparing a paragraph, or a tally derived
   from one, to an English string pins wording, not behaviour;
2. a declared prose key is EXCUSED with free text — the unfalsifiable form;
3. a key *not* declared prose is discharged;
4. the discharged set differs from ``assertions.prose`` — the comparison that
   consumes ``prose`` itself, and what makes a forgotten key fail rather than
   vanish;
5. a discharge names no keys;
6. a discharge names a key this fixture's run did not assert — the rule that
   makes the excuse falsifiable at all;
7. a discharge names a key that is itself prose.

The ledger is FIXTURE-scoped, not block-scoped: ``epoch_disambiguation`` sits in
``assertions`` and is discharged by ``expect.frame_epoch`` / ``expect.blob_epoch``,
asserted long after the ``assertions`` block is finished. So a named key matches
by NAME in any block of that fixture, and the verdict lands when the fixture's
replay is done — :func:`verify_prose`. A run that never verifies fails from
``conftest.pytest_sessionfinish``: an unverified discharge claim is exactly as
unchecked as an unconsumed key.

``note`` / ``description`` / ``reason`` inside a per-step or per-scenario block
stay exempt BY NAME (:data:`PROSE_KEYS`) — the ~97 step notes in the
reactive-graph corpus are annotations, not obligations. That exemption stops at
the block's own declaration: a key listed in ``assertions.prose`` is NOT
satisfied by the blanket, even when it is spelled ``note``, which is how
``frame_roundtrip_json.json``'s top-level ``note`` — a real obligation about
``role`` vs ``byte_canonical`` — gets discharged rather than waved through.

Scenario was never replayed (#lzscenariocoverage)
-------------------------------------------------
The rungs above all reason about the scenarios a runner *reached*. A fixture that
carries several named scenarios can be PARTIALLY replayed and none of them
notices: the coverage guard only asks whether the file was opened (one scenario is
enough), and the key trackers only bind blocks a runner actually walks, so a
scenario nobody enters contributes no unconsumed key and no unasserted key. It is
invisible by construction.

So there is a fourth ledger here, recording which scenario ids the run really
replayed. Its shape mirrors the runtime fixture manifest deliberately: a
hand-authored list of "scenarios this binding covers" is a claim, and a claim
rots.

The recording happens at the point of replay, and *yielding is not replaying*: a
generator cannot tell a loop body that ran from one that ``continue``d past the
scenario, so booking at the yield would call a skipped scenario covered — the very
defect. :func:`scenarios` therefore hands out a view that books on the first read
of the scenario's PAYLOAD (``steps``, ``ops``, ``frames``, ``expect``, …) and
stays silent for :data:`SCENARIO_LABEL_KEYS` (``id``, ``name``, …), which is what
a skip reads on its way past. Runners that pick a scenario out by name wrap it the
same way through :func:`scenario_view`.

Ids resolve in one fixed order shared by every binding: ``id``, else ``name``.
There is no third option (``#lzspecscenarioids``): a positional ``#<n>`` id
silently rebinds to a different scenario when the corpus array is reordered, so
an unidentified scenario is a FAILURE rather than a note. lazily-spec's
``scenario-identity-check`` keeps the corpus side of that invariant.

Verification runs in both directions at session end, like ``KNOWN_UNCOVERED`` and
like :func:`excuse_key`: a scenario on disk that the run never replayed fails
unless :func:`excuse_scenario` names it with a reason; an excuse for a scenario the
same run DID replay fails as stale; and an excuse naming an id the fixture does not
carry fails as rotted.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any


__all__ = [
    "BLOCK_KEYS",
    "PROSE_DECLARATION_KEY",
    "PROSE_KEYS",
    "SCENARIO_EXCUSES",
    "SCENARIO_LABEL_KEYS",
    "TrackedBlock",
    "assert_invalidates",
    "assert_key",
    "assert_key_with",
    "consumption_failures",
    "corpus_dir",
    "discharged_prose",
    "excuse_key",
    "excuse_scenario",
    "instrument",
    "prose_failures",
    "prose_key",
    "record_scenario",
    "replayed_scenarios",
    "reset",
    "scenario_failures",
    "scenario_id",
    "scenario_view",
    "scenarios",
    "tracked",
    "verify_prose",
]

#: Fixture keys whose dict value is an expectation block. These are the names the
#: canonical corpus uses; a runner reads the block and every key it does not
#: recognise is the silent skip this module exists to catch.
BLOCK_KEYS = frozenset(
    {"assertions", "expect", "expect_after", "expect_initial", "expected"}
)

#: Key names the canonical corpus uses for human narration inside an assertion
#: block. Narration is not an assertion and cannot be "implemented", so these are
#: exempt from all three verdicts wherever they appear. Anything outside this list
#: needs a per-call-site ``prose=(...)`` declaration to claim the same exemption.
PROSE_KEYS = frozenset({"comment", "description", "note", "notes", "reason", "why"})

#: The key an assertion block uses to DECLARE which of its siblings are prose
#: (``#lzprosekeyconvention``). The corpus decides; a binding must not. Because
#: the declaration is itself a key of the block, the consumption guard above sees
#: it: a runner that ignores it fails with an unconsumed key, which is what makes
#: the rollout self-enforcing.
PROSE_DECLARATION_KEY = "prose"


class _Ledger:
    """Session-wide consumption record for one ``(fixture, block)`` pair."""

    __slots__ = (
        "asserted",
        "block",
        "declared_prose",
        "discharged",
        "excused",
        "fixture",
        "keys",
        "prose",
        "seen",
    )

    def __init__(self, fixture: str, block: str) -> None:
        self.fixture = fixture
        self.block = block
        self.keys: set[str] = set()
        self.seen: set[str] = set()
        self.asserted: set[str] = set()
        self.excused: dict[str, str] = {}
        self.prose: set[str] = set()
        #: Keys this block's own ``prose`` array declares as obligations.
        self.declared_prose: set[str] = set()
        #: Declared prose key -> the executable keys named as discharging it.
        self.discharged: dict[str, tuple[str, ...]] = {}

    def blanket_exempt(self) -> set[str]:
        """Names waved through as annotation, minus anything the block DECLARED.

        The by-name exemption is only safe while the key annotates. A key the
        corpus lists in ``assertions.prose`` states an obligation, so the blanket
        must not reach it even when it is spelled ``note`` — otherwise the
        reserved name becomes a place no runner can be made to discharge
        anything, which is the hazard the convention names explicitly.
        """
        return self.prose - self.declared_prose

    def unconsumed(self) -> list[str]:
        return sorted(self.keys - self.seen - self.blanket_exempt())

    def read_not_asserted(self) -> list[str]:
        """Keys some runner read but no runner compared against the fixture value."""
        satisfied = (
            self.asserted
            | set(self.excused)
            | self.blanket_exempt()
            | set(self.discharged)
        )
        return sorted((self.keys & self.seen) - satisfied)

    def stale_excuses(self) -> list[str]:
        """Excuses for keys the same session also asserts — hiding nothing."""
        return sorted(set(self.excused) & self.asserted)


_LEDGERS: dict[tuple[str, str], _Ledger] = {}


class TrackedBlock(Mapping[str, Any]):
    """A read-through view of a fixture assertion block that records key reads.

    Behaves like the underlying mapping for every access a runner performs, and
    additionally remembers which keys were touched, so an unrecognised key can be
    reported instead of silently skipped.
    """

    def __init__(
        self,
        data: Mapping[str, Any],
        *,
        fixture: str,
        block: str,
        prose: tuple[str, ...] = (),
    ) -> None:
        self._data = dict(data)
        self.fixture = fixture
        self.block = block
        ledger = _LEDGERS.setdefault((fixture, block), _Ledger(fixture, block))
        ledger.keys.update(self._data)
        # Prose keys count as consumed AND as satisfied: the declaration at the
        # call site IS the act of looking at them, and there is nothing to check.
        ledger.prose.update(prose)
        ledger.prose.update(PROSE_KEYS)
        # The CORPUS's own declaration (#lzprosekeyconvention) is not an
        # exemption — it is the list of keys that must be DISCHARGED. Registering
        # it here rather than at the call site is deliberate: a binding must not
        # decide for itself which keys are prose, and reading the block is the
        # only moment the corpus's answer is in hand.
        declared = self._data.get(PROSE_DECLARATION_KEY)
        if isinstance(declared, (list, tuple)):
            names = [name for name in declared if isinstance(name, str)]
            ledger.declared_prose.update(names)
            if names:
                _PROSE_VERIFIED.setdefault(fixture, False)
        self._ledger = ledger

    # -- Mapping protocol; every path marks consumption ---------------------

    def __getitem__(self, key: str) -> Any:
        self._ledger.seen.add(key)
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        self._ledger.seen.add(key)
        return self._data.get(key, default)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self._ledger.seen.add(key)
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        # A runner that walks the whole block is consuming the whole block.
        self._ledger.seen.update(self._data)
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def keys(self):  # type: ignore[override]
        self._ledger.seen.update(self._data)
        return self._data.keys()

    def items(self):  # type: ignore[override]
        self._ledger.seen.update(self._data)
        return self._data.items()

    def values(self):  # type: ignore[override]
        self._ledger.seen.update(self._data)
        return self._data.values()

    def __eq__(self, other: object) -> bool:
        # Whole-block equality is the strongest form of consumption there is: it
        # checks every key AND rejects keys the runner did not expect. Every
        # fixture value reaches the comparison, so it also counts as asserting
        # every key (#lzconsumednotasserted) — including, therefore, any declared
        # prose key, which is rule 1 and refused as such.
        self._refuse_asserting_prose(sorted(self._data), "whole-block equality")
        self._ledger.seen.update(self._data)
        self._ledger.asserted.update(self._data)
        if isinstance(other, TrackedBlock):
            return self._data == other._data
        return self._data == other

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"TrackedBlock({self.fixture}:{self.block}, {self._data!r})"

    def unconsumed(self) -> list[str]:
        return sorted(
            set(self._data) - self._ledger.seen - self._ledger.blanket_exempt()
        )

    # -- Assertion ledger (#lzconsumednotasserted) --------------------------

    def _refuse_asserting_prose(self, keys: Iterable[str], how: str) -> None:
        """Rule 1: a declared prose key must never reach a comparison."""
        offending = sorted(self._ledger.declared_prose.intersection(keys))
        if not offending:
            return
        raise AssertionError(
            f"{self.fixture} [{self.block}]: {how} would ASSERT prose key(s) "
            f"{offending}. `assertions.prose` declares them as English "
            f"obligations, and comparing a paragraph — or a tally derived from "
            f"one — to a literal pins WORDING, not behaviour: a copy-edit "
            f"reddens the run and a library regression does not. Discharge them "
            f"with prose_key(block, key, discharged_by=[...]) instead "
            f"(#lzprosekeyconvention)"
        )

    def mark_asserted(self, key: str) -> Any:
        """Record that ``key``'s fixture value reached a comparison; return it."""
        self._refuse_asserting_prose((key,), f"assert_key({key!r})")
        value = self[key]  # marks read
        self._ledger.asserted.add(key)
        return value

    def mark_excused(self, key: str, reason: str) -> None:
        if key in self._ledger.declared_prose:
            raise AssertionError(
                f"{self.fixture} [{self.block}]: excuse_key({key!r}) is a "
                f"free-text excuse for a key `assertions.prose` DECLARES "
                f"(#lzprosekeyconvention). An unfalsifiable reason is "
                f"indistinguishable from the undocumented default this clause "
                f"exists to remove. Name the executable keys that carry the "
                f"obligation instead: "
                f"prose_key(block, {key!r}, discharged_by=[...])"
            )
        if not reason or not reason.strip():
            raise AssertionError(
                f"{self.fixture} [{self.block}]: excuse_key({key!r}) needs a "
                f"non-empty reason naming where the fact is proven instead, or "
                f"why it cannot be proven here"
            )
        if key not in self._data:
            raise AssertionError(
                f"{self.fixture} [{self.block}]: excuse_key({key!r}) names a key "
                f"the fixture does not carry; the excuse has rotted"
            )
        self._ledger.seen.add(key)
        self._ledger.excused[key] = reason.strip()


def assert_key(
    block: TrackedBlock,
    key: str,
    actual: Any,
    where: str = "",
) -> Any:
    """Assert ``actual`` equals the fixture's value for ``key``, marking it asserted.

    This is the one equality entry point. A runner that reads ``block[key]`` and
    compares it against a hand-written constant marks the key *read* but never
    *asserted*, which is exactly the hole this closes: the fixture's own value has
    to reach the comparison for the key to count.
    """
    want = block.mark_asserted(key)
    site = f" ({where})" if where else ""
    assert actual == want, (
        f"{block.fixture} [{block.block}] {key}{site}: expected {want!r}, got {actual!r}"
    )
    return want


def assert_key_with(
    block: TrackedBlock,
    key: str,
    check: Callable[[Any], Any] | None = None,
    where: str = "",
) -> Any:
    """Hand the fixture's value for ``key`` to ``check``, marking the key asserted.

    For comparisons that are not equality — a tolerance, a set containment, a
    regex, a structural walk. ``check`` may assert internally (returning ``None``)
    or return a truthy/falsy verdict, which is then asserted. Called without a
    ``check`` it simply returns the value and books the assertion, for the arms
    whose comparison is too tangled to express as a lambda; the fixture value
    still reaches the caller's own check.
    """
    want = block.mark_asserted(key)
    if check is None:
        return want
    verdict = check(want)
    if verdict is not None:
        site = f" ({where})" if where else ""
        assert verdict, (
            f"{block.fixture} [{block.block}] {key}{site}: predicate rejected "
            f"fixture value {want!r}"
        )
    return want


def assert_invalidates(
    block: Mapping[str, Any],
    observed: Mapping[str, bool],
    *,
    key: str = "invalidates",
    where: str = "",
) -> None:
    """Assert a step's per-reader invalidation map against what really happened.

    The corpus spells reader invalidation as a nested ``{"invalidates": {reader:
    bool}}`` map. The tracker only sees the outer key, so the inner map is checked
    here instead: *every* reader the fixture names must appear in ``observed``, or
    the fixture is expecting something of a reader this runner never watched — the
    same silent skip one level down. A step without the key asserts nothing.
    """
    if key not in block:
        return
    want = block.mark_asserted(key) if isinstance(block, TrackedBlock) else block[key]
    site = f" ({where})" if where else ""
    unwatched = sorted(set(want) - set(observed))
    assert not unwatched, (
        f"{site.strip() or 'step'}: fixture expects invalidation of reader(s) "
        f"{unwatched} that this runner never observed"
    )
    for reader, expected in want.items():
        if expected:
            assert observed[reader], f"reader `{reader}`{site} should have invalidated"
        else:
            assert not observed[reader], (
                f"reader `{reader}`{site} should have stayed cached"
            )


def excuse_key(block: TrackedBlock, key: str, reason: str) -> None:
    """Declare that ``key`` cannot be asserted here, and say why.

    Both directions, like the ``KNOWN_UNCOVERED`` coverage allowlist: the excuse
    satisfies the key, and a key the same session *also* asserts fails as a stale
    excuse. Prefer implementing the assertion — excusing is the fallback for when
    there is genuinely nothing to compare at this call site.
    """
    block.mark_excused(key, reason)


# ---------------------------------------------------------------------------
# Declared prose keys: discharge, never assert, never excuse
# (#lzprosekeyconvention)
# ---------------------------------------------------------------------------

#: fixture -> whether ``verify_prose`` ran for it. An entry appears as soon as a
#: block of that fixture DECLARES prose, or as soon as a discharge is claimed, so
#: the "never verified" verdict does not depend on the runner remembering to
#: register anything.
_PROSE_VERIFIED: dict[str, bool] = {}


def _fixture_ledgers(fixture: str) -> list[_Ledger]:
    return [ledger for (name, _), ledger in _LEDGERS.items() if name == fixture]


def _fixture_declared_prose(fixture: str) -> set[str]:
    declared: set[str] = set()
    for ledger in _fixture_ledgers(fixture):
        declared |= ledger.declared_prose
    return declared


def _fixture_asserted(fixture: str) -> set[str]:
    """Every key name this fixture's run compared against the fixture's value.

    Fixture-scoped on purpose: ``epoch_disambiguation`` lives in ``assertions``
    and is discharged by ``expect.frame_epoch`` / ``expect.blob_epoch``, which
    are asserted long after that block is finished. Matching is therefore by key
    NAME in any block of the fixture.
    """
    asserted: set[str] = set()
    for ledger in _fixture_ledgers(fixture):
        asserted |= ledger.asserted
    return asserted


def prose_key(
    block: TrackedBlock,
    key: str,
    discharged_by: Iterable[str],
) -> tuple[str, ...]:
    """Discharge a declared prose key by naming the assertions that carry it.

    The replacement for the free-text ``excuse_key`` reasons this binding used
    to write for these paragraphs — not a sibling of them. Two paths to satisfy
    one key is the ambiguity ``#lzprosekeyconvention`` removes, so a declared
    prose key reaches :func:`excuse_key` and :func:`assert_key` as an error.

    Refused at the call site: a key the corpus does not declare prose (rule 3),
    a discharge naming nothing (rule 5), and a discharge naming another prose
    key (rule 7). The remaining two — the discharged SET matching
    ``assertions.prose`` (rule 4) and every named key really having been
    asserted (rule 6) — need the whole fixture's replay, so they land in
    :func:`verify_prose`.
    """
    if not isinstance(block, TrackedBlock):
        raise AssertionError(
            "prose_key needs a tracked block; wrap the fixture through "
            "instrument()/tracked() so the discharge is recorded against a ledger"
        )
    # Private access is deliberate: the ledger is this module's own state.
    ledger = block._ledger
    if key not in block._data:
        raise AssertionError(
            f"{block.fixture} [{block.block}]: prose_key({key!r}) names a key the "
            f"fixture does not carry; the discharge has rotted"
        )
    if key not in ledger.declared_prose:
        raise AssertionError(
            f"{block.fixture} [{block.block}]: prose_key({key!r}) discharges a key "
            f"`assertions.prose` does NOT declare. The corpus decides which keys "
            f"are paragraphs — a binding that decides for itself is how four "
            f"treatments of one rule went unnoticed. Declared here: "
            f"{sorted(ledger.declared_prose)} (#lzprosekeyconvention)"
        )
    names = tuple(discharged_by)
    if not names:
        raise AssertionError(
            f"{block.fixture} [{block.block}]: prose_key({key!r}) names NO "
            f"discharging keys. A discharge that names nothing is the free-text "
            f"excuse again with the text removed — it makes no claim the tracker "
            f"can check (#lzprosekeyconvention)"
        )
    declared = _fixture_declared_prose(block.fixture)
    prose_named = sorted(
        name for name in names if name in declared or name == PROSE_DECLARATION_KEY
    )
    if prose_named:
        raise AssertionError(
            f"{block.fixture} [{block.block}]: prose_key({key!r}) is discharged by "
            f"{prose_named}, which "
            + ("is itself prose" if len(prose_named) == 1 else "are themselves prose")
            + ". A paragraph cannot carry another paragraph's obligation; name an "
            "EXECUTABLE key (#lzprosekeyconvention)"
        )
    ledger.seen.add(key)
    ledger.discharged[key] = names
    _PROSE_VERIFIED.setdefault(block.fixture, False)
    return names


def verify_prose(fixture: Any) -> None:
    """Check this fixture's discharges once its replay is finished.

    Rule 4 — the discharged set equals ``assertions.prose`` — is the comparison
    that CONSUMES and ASSERTS the ``prose`` key itself, which is what makes a
    forgotten paragraph fail rather than vanish. Rule 6 — every named key was
    really asserted somewhere in this fixture's run — is the whole convention:
    it turns "``epoch_disambiguation`` is prose" (not a claim) into
    "``epoch_disambiguation`` is discharged by ``frame_epoch`` and ``blob_epoch``"
    (a claim about the run, and one the tracker can falsify).

    Call it at the end of the runner that replays the fixture. A fixture that
    declares prose and is never verified is reported by
    ``conftest.pytest_sessionfinish``, exactly as an unconsumed key is.
    """
    name = (
        fixture
        if isinstance(fixture, str)
        else getattr(fixture, "conformance_name", "")
    )
    if not name:
        raise AssertionError(
            "verify_prose needs the corpus-relative fixture path; load the "
            "fixture through instrument(name=...) or pass `name` explicitly"
        )
    asserted = _fixture_asserted(name)
    problems: list[str] = []
    for ledger in _fixture_ledgers(name):
        if not ledger.declared_prose and not ledger.discharged:
            continue
        discharged = set(ledger.discharged)
        # Reading `assertions.prose` for THIS comparison is what consumes it.
        ledger.seen.add(PROSE_DECLARATION_KEY)
        ledger.asserted.add(PROSE_DECLARATION_KEY)
        missing = sorted(ledger.declared_prose - discharged)
        extra = sorted(discharged - ledger.declared_prose)
        if missing:
            problems.append(
                f"[{ledger.block}] prose key(s) {missing} are declared in "
                f"`assertions.prose` but were never discharged"
            )
        if extra:
            problems.append(
                f"[{ledger.block}] key(s) {extra} were discharged but are not "
                f"declared in `assertions.prose`"
            )
        for key, names in sorted(ledger.discharged.items()):
            unasserted = sorted(set(names) - asserted)
            if unasserted:
                problems.append(
                    f"[{ledger.block}] prose_key({key!r}) claims to be discharged "
                    f"by {unasserted}, which this fixture's run never ASSERTED. "
                    f"The claim is the point of the convention — an obligation "
                    f"discharged by an assertion nobody makes is discharged by "
                    f"nothing"
                )
    _PROSE_VERIFIED[name] = True
    if problems:
        raise AssertionError(
            f"{name}: prose discharge verification failed "
            f"(#lzprosekeyconvention)\n  " + "\n  ".join(problems)
        )


def discharged_prose(fixture: str) -> dict[str, dict[str, tuple[str, ...]]]:
    """``{block: {prose key: named keys}}`` recorded for ``fixture`` so far."""
    return {
        ledger.block: dict(ledger.discharged)
        for ledger in _fixture_ledgers(fixture)
        if ledger.discharged
    }


def prose_failures() -> list[str]:
    """Report lines for prose discharges the run never verified.

    An unverified discharge claim is as unchecked as an unconsumed key: nothing
    compared the discharged set against ``assertions.prose``, and nothing checked
    that the named keys were asserted. Reported at session end for the same
    reason the other rungs are — only then is the ledger complete.
    """
    lines: list[str] = []
    for fixture, verified in sorted(_PROSE_VERIFIED.items()):
        if verified:
            continue
        declared = sorted(_fixture_declared_prose(fixture))
        claimed = sorted(
            key for ledger in _fixture_ledgers(fixture) for key in ledger.discharged
        )
        lines.append(
            f"{fixture}: `assertions.prose` declares {declared} and this run "
            f"discharged {claimed}, but verify_prose({fixture!r}) never ran. An "
            f"unverified discharge is an unchecked one — call verify_prose at the "
            f"end of the runner that replays this fixture (#lzprosekeyconvention)"
        )
    return lines


def tracked(
    data: Mapping[str, Any] | None,
    *,
    fixture: str,
    block: str = "assertions",
    prose: tuple[str, ...] = (),
) -> TrackedBlock:
    """Wrap a fixture assertion/expectation block so unread keys become errors.

    ``fixture`` should identify the file (and scenario, when a fixture holds
    several) well enough for the failure message to point at one place. ``data``
    may be ``None`` for an optional block; the result is then an empty tracked
    mapping, which keeps call sites free of ``if block is not None``.
    """
    return TrackedBlock(data or {}, fixture=fixture, block=block, prose=prose)


def instrument(
    fixture: Any,
    *,
    name: str,
    prose: tuple[str, ...] = (),
    block_keys: frozenset[str] = BLOCK_KEYS,
) -> Any:
    """Return ``fixture`` with every expectation block replaced by a tracked view.

    One call at the loader covers a whole runner, which is the point: wrapping
    each access site would only record what each site claims to read, and a
    runner that quietly stops reading a block would keep its wrapper. Wrapping at
    load records what the runner really took out of the fixture.

    ``prose`` names keys that are narration rather than assertions wherever they
    appear in this fixture. Keep the list short and justified at the call site —
    it is the one legitimate way for a key to go unchecked.
    """

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                child = f"{path}.{key}" if path else key
                if key in block_keys and isinstance(value, dict):
                    out[key] = TrackedBlock(
                        value, fixture=name, block=child, prose=prose
                    )
                else:
                    out[key] = walk(value, child)
            return out
        if isinstance(node, list):
            items: list[Any] = []
            for index, value in enumerate(node):
                label = value.get("name") if isinstance(value, dict) else None
                tag = label if isinstance(label, str) else str(index)
                items.append(walk(value, f"{path}[{tag}]"))
            return items
        return node

    walked = walk(fixture, "")
    if isinstance(walked, dict):
        instrumented = _InstrumentedFixture(walked)
        instrumented.conformance_name = name
        return instrumented
    return walked


def reset(fixture: str | None = None) -> None:
    """Drop the session ledger (tests of the guard itself).

    With ``fixture`` set, only that fixture's ledgers are dropped, so a self-test
    can plant a deliberate violation, observe the report, and clean up after
    itself without erasing what the real runners have booked.
    """
    if fixture is None:
        _LEDGERS.clear()
        _REPLAYED.clear()
        _PROSE_VERIFIED.clear()
        _SCENARIO_EXCUSES.clear()
        _seed_scenario_excuses()
        return
    for key in [key for key in _LEDGERS if key[0] == fixture]:
        del _LEDGERS[key]
    _REPLAYED.pop(fixture, None)
    _PROSE_VERIFIED.pop(fixture, None)
    _SCENARIO_EXCUSES.pop(fixture, None)
    if fixture in SCENARIO_EXCUSES:
        for scenario, reason in SCENARIO_EXCUSES[fixture].items():
            excuse_scenario(fixture, scenario, reason)


def consumption_failures() -> list[str]:
    """Report lines for the three verdicts, over every ``(fixture, block)``.

    1. never read — ``#lzassertunknownkeys``, the original rung;
    2. read but never asserted — ``#lzconsumednotasserted``;
    3. a stale excuse: excused *and* asserted in the same session.
    """
    lines: list[str] = []
    for (fixture, block), ledger in sorted(_LEDGERS.items()):
        missed = ledger.unconsumed()
        if missed:
            lines.append(
                f"{fixture} [{block}]: assertion key(s) {missed} present in the "
                f"fixture but never consumed by any runner"
            )
        unasserted = ledger.read_not_asserted()
        if unasserted:
            lines.append(
                f"{fixture} [{block}]: assertion key(s) {unasserted} were read but "
                f"never compared against the fixture's value — route them through "
                f"assert_key/assert_key_with, or declare excuse_key(reason=...)"
            )
        stale = ledger.stale_excuses()
        if stale:
            lines.append(
                f"{fixture} [{block}]: excuse_key({stale}) is stale — the same run "
                f"also asserts "
                + ("that key" if len(stale) == 1 else "those keys")
                + "; reason(s): "
                + "; ".join(ledger.excused[key] for key in stale)
            )
    return lines


# ---------------------------------------------------------------------------
# Rung 4: per-scenario replay accounting (#lzscenariocoverage)
# ---------------------------------------------------------------------------

#: The key under which the canonical corpus spells a fixture's scenario list.
SCENARIOS_KEY = "scenarios"

#: Scenarios this binding does not replay, with the reason it cannot.
#:
#: The companion of ``KNOWN_UNCOVERED`` in ``scripts/check-conformance-coverage.sh``
#: — that list names whole fixtures this binding never opens, this one names
#: scenarios inside a fixture it does open, so the two together are the single
#: reading of what lazily-py does not prove. Keep them in sync: a fixture listed in
#: ``KNOWN_UNCOVERED`` must NOT appear here (it is already excused a level up), and
#: an entry here is a claim someone looked, exactly like an entry there.
#:
#: Prefer implementing the scenario. Both directions are enforced: an entry for a
#: scenario the run DOES replay fails as stale, and an entry naming an id the
#: fixture does not carry fails as rotted.
SCENARIO_EXCUSES: dict[str, dict[str, str]] = {}


class _InstrumentedFixture(dict):
    """A loaded fixture that remembers its corpus-relative name.

    :func:`instrument` already knows the name — every call site passes it — so
    carrying it on the fixture lets :func:`scenarios` record against the right
    fixture without every loop repeating the string. A ``dict`` subclass keeps
    every existing access, ``isinstance`` check, and equality comparison intact.
    """

    conformance_name: str = ""


#: fixture -> scenario ids this run actually replayed.
_REPLAYED: dict[str, set[str]] = {}
#: fixture -> {scenario id: reason}, seeded from ``SCENARIO_EXCUSES``.
_SCENARIO_EXCUSES: dict[str, dict[str, str]] = {}


def _seed_scenario_excuses() -> None:
    for fixture, entries in SCENARIO_EXCUSES.items():
        for scenario, reason in entries.items():
            excuse_scenario(fixture, scenario, reason)


def scenario_id(scenario: Any, index: int) -> str:
    """Resolve a scenario's id: ``id``, else ``name``. There is no third option.

    One fixed order, identical in every binding.

    The positional ``#<n>`` fallback is GONE (``#lzspecscenarioids``). It let the
    ledger record a scenario BY POSITION, where inserting one ahead of it silently
    rebinds that entry — and any excuse naming it — to a different scenario, with
    nothing turning red: the guard compares "index 1 was replayed" against
    whatever now sits at index 1 and agrees with itself.

    It was load-bearing for exactly one fixture,
    ``collections/mergecell_algebra.json``, whose scenarios differed only by
    ``policy``. They carry ids now, and lazily-spec's ``scenario-identity-check``
    keeps every scenario identified — so this is a hole with no users, which is one
    waiting to become load-bearing again. A blank identifier is refused for the
    same reason: it would file every blank-id scenario under one ledger entry,
    which reads as "replayed" the moment any one of them runs.
    """
    if isinstance(scenario, Mapping):
        for key in ("id", "name"):
            value = scenario.get(key)
            if isinstance(value, str) and value.strip():
                return value
    raise AssertionError(
        f"scenario at index {index} carries neither `id` nor `name`. The replay "
        f"ledger would have to record it by POSITION, where inserting a scenario "
        f"ahead of it silently rebinds that entry to a different scenario. Give it "
        f"a stable id upstream in lazily-spec (#lzspecscenarioids)."
    )


def record_scenario(fixture: str, scenario: Any, index: int = 0) -> str:
    """Book one scenario as replayed; return the id it was booked under.

    ``scenario`` may be the scenario mapping (the id is resolved from it) or an
    already-resolved id string, for the runners that look a scenario up by name.
    """
    if not fixture:
        raise AssertionError(
            "record_scenario needs the corpus-relative fixture path; an unnamed "
            "fixture cannot be compared against the corpus on disk"
        )
    resolved = scenario if isinstance(scenario, str) else scenario_id(scenario, index)
    _REPLAYED.setdefault(fixture, set()).add(resolved)
    return resolved


#: Keys that identify or narrate a scenario rather than drive one. Reading only
#: these is *looking at the label*, not replaying — a loop body that reads
#: ``scenario["id"]`` and ``continue``s past it has replayed nothing, and booking
#: it there would let the skip this rung exists to catch hide behind the helper.
SCENARIO_LABEL_KEYS = frozenset(
    {"comment", "description", "id", "label", "name", "note", "notes", "reason", "why"}
)


class _ScenarioView(dict):
    """One scenario, booked as replayed on the first read of its *payload*.

    Yielding is not replaying. A generator cannot tell a loop body that ran from
    one that ``continue``d — both just come back for the next item — so booking at
    the yield would call a skipped scenario covered, which is precisely the defect
    (#lzscenariocoverage). Booking on payload access can tell them apart: the
    steps/ops/frames/expect of a scenario are only read by a runner that is about
    to replay it, while ``id`` and ``name`` are what a skip reads on its way past.

    A ``dict`` subclass rather than a ``Mapping`` wrapper so every runner keeps
    its ``dict`` annotations and its existing access patterns.
    """

    def __init__(self, data: Mapping[str, Any], *, fixture: str, ident: str) -> None:
        super().__init__(data)
        self._fixture = fixture
        self._ident = ident

    def _touch(self, key: object) -> None:
        if key not in SCENARIO_LABEL_KEYS:
            record_scenario(self._fixture, self._ident)

    def __getitem__(self, key: str) -> Any:
        self._touch(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        self._touch(key)
        return super().get(key, default)

    def __iter__(self) -> Iterator[str]:
        record_scenario(self._fixture, self._ident)
        return super().__iter__()

    def keys(self):  # type: ignore[override]
        record_scenario(self._fixture, self._ident)
        return super().keys()

    def items(self):  # type: ignore[override]
        record_scenario(self._fixture, self._ident)
        return super().items()

    def values(self):  # type: ignore[override]
        record_scenario(self._fixture, self._ident)
        return super().values()


def scenario_view(fixture: str, scenario: Mapping[str, Any], index: int = 0) -> Any:
    """Wrap one scenario so reading its payload books it as replayed.

    For the runners that pick a scenario out by name rather than iterating. The
    lookup itself must not book: ``next(s for s in ... if s["name"] == wanted)``
    walks past every scenario ahead of the match, and a scenario handed back but
    never used is not replayed either.
    """
    return _ScenarioView(scenario, fixture=fixture, ident=scenario_id(scenario, index))


def scenarios(
    fixture: Mapping[str, Any],
    name: str | None = None,
    *,
    key: str = SCENARIOS_KEY,
) -> Iterator[Any]:
    """Iterate a fixture's scenarios, booking each as its payload is read.

    This is the shared iteration helper the contract asks for — a runner converted
    to::

        for scenario in scenarios(fixture):
            ...

    cannot forget to book what it replayed, because the booking rides on the
    scenario the loop is handed rather than on a call the loop has to remember.
    And a runner that stops entering a scenario stops booking it, which is the
    whole point: a declaration would keep claiming coverage the run no longer has.

    ``name`` defaults to the corpus-relative name :func:`instrument` recorded on
    the fixture.
    """
    resolved_name = name or getattr(fixture, "conformance_name", "")
    if not resolved_name:
        raise AssertionError(
            "scenarios() needs the corpus-relative fixture path; load the fixture "
            "through instrument(name=...) or pass `name` explicitly"
        )
    for index, scenario in enumerate(fixture[key]):
        yield scenario_view(resolved_name, scenario, index)


def excuse_scenario(fixture: str, scenario: str, reason: str) -> None:
    """Declare that this binding cannot replay ``scenario``, and say why.

    Both directions, like ``KNOWN_UNCOVERED`` and like :func:`excuse_key`: the
    excuse satisfies the scenario, an excuse for a scenario the same run replays
    fails as stale, and an excuse naming an id the fixture does not carry fails as
    rotted. Prefer implementing the scenario — a known-skipped scenario is exactly
    the work this rung exists to force.
    """
    if not reason or not reason.strip():
        raise AssertionError(
            f"{fixture}: excuse_scenario({scenario!r}) needs a non-empty reason "
            f"naming what this binding cannot express"
        )
    _SCENARIO_EXCUSES.setdefault(fixture, {})[scenario] = reason.strip()


def replayed_scenarios() -> dict[str, set[str]]:
    """The scenario ids this run really replayed, per fixture."""
    return {fixture: set(ids) for fixture, ids in _REPLAYED.items()}


def corpus_dir() -> Path:
    """Locate the canonical corpus, honouring ``LAZILY_SPEC_CONFORMANCE_DIR``.

    The same override ``scripts/check-conformance-coverage.sh`` reads, so a
    scratch copy of the corpus (never edit the shared one in place — it is shared
    by all nine bindings) moves both guards together.
    """
    override = os.environ.get("LAZILY_SPEC_CONFORMANCE_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"


def _disk_scenarios(path: Path) -> tuple[list[str], list[str]] | None:
    """``(ids, unidentified)`` for a fixture on disk, or ``None`` if it has none.

    ``unidentified`` names the indices that carry no ``id``/``name``. Those are a
    corpus defect reported as a FAILURE, never an id to invent
    (``#lzspecscenarioids``): booking a scenario by POSITION silently rebinds that
    ledger entry to a different scenario on any corpus reorder.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    listed = raw.get(SCENARIOS_KEY)
    if not isinstance(listed, list) or not listed:
        return None
    ids: list[str] = []
    unidentified: list[str] = []
    for index, scenario in enumerate(listed):
        try:
            ids.append(scenario_id(scenario, index))
        except AssertionError:
            unidentified.append(f"#{index}")
    return ids, unidentified


def scenario_failures(
    opened: Iterable[str],
    corpus: Path | None = None,
) -> tuple[list[str], list[str]]:
    """``(failures, notices)`` for per-scenario replay accounting.

    ``opened`` is the runtime fixture manifest — the corpus-relative paths the
    suite really read. Only those fixtures are checked: a fixture this binding
    does not open at all is already the coverage guard's verdict, and reporting it
    again here would just name the same gap twice under a weaker rung.

    Notices are not failures; failures are what the caller asserts on.
    """
    root = corpus if corpus is not None else corpus_dir()
    failures: list[str] = []
    notices: list[str] = []

    checked = sorted(set(opened) | set(_SCENARIO_EXCUSES))
    for fixture in checked:
        path = root / fixture
        if not path.is_file():
            if fixture in _SCENARIO_EXCUSES:
                failures.append(
                    f"{fixture}: excuse_scenario names a fixture that is not in the "
                    f"canonical corpus; the excuse has rotted"
                )
            continue
        found = _disk_scenarios(path)
        excused = _SCENARIO_EXCUSES.get(fixture, {})
        if found is None:
            if excused:
                failures.append(
                    f"{fixture}: excuse_scenario({sorted(excused)}) names scenario(s) "
                    f"in a fixture that carries no `scenarios` array; the excuse has "
                    f"rotted"
                )
            continue
        ids, unidentified = found
        replayed = _REPLAYED.get(fixture, set())

        rotted = sorted(set(excused) - set(ids))
        if rotted:
            failures.append(
                f"{fixture}: excuse_scenario({rotted}) names scenario id(s) the "
                f"fixture does not carry; the excuse has rotted. Ids on disk: {ids}"
            )
        stale = sorted(set(excused) & replayed)
        if stale:
            failures.append(
                f"{fixture}: excuse_scenario({stale}) is stale — this run DID replay "
                + ("that scenario" if len(stale) == 1 else "those scenarios")
                + "; reason(s): "
                + "; ".join(excused[name] for name in stale)
            )
        skipped = [name for name in ids if name not in replayed and name not in excused]
        if skipped:
            failures.append(
                f"{fixture}: scenario(s) {skipped} are in the fixture but were never "
                f"replayed by this suite. The fixture was opened, so the coverage "
                f"guard is satisfied and the key guards never saw these blocks — "
                f"replay them, or declare excuse_scenario(fixture, id, reason)"
            )
        if unidentified:
            failures.append(
                f"{fixture}: scenario(s) at {unidentified} carry neither `id` nor "
                f"`name`. The ledger would record them by POSITION, which silently "
                f"rebinds on a corpus reorder. Give them stable ids upstream in "
                f"lazily-spec (#lzspecscenarioids)"
            )

    return failures, notices


_seed_scenario_excuses()
