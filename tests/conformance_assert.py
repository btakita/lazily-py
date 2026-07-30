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
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any


__all__ = [
    "BLOCK_KEYS",
    "PROSE_KEYS",
    "TrackedBlock",
    "assert_invalidates",
    "assert_key",
    "assert_key_with",
    "consumption_failures",
    "excuse_key",
    "instrument",
    "reset",
    "tracked",
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


class _Ledger:
    """Session-wide consumption record for one ``(fixture, block)`` pair."""

    __slots__ = ("asserted", "block", "excused", "fixture", "keys", "prose", "seen")

    def __init__(self, fixture: str, block: str) -> None:
        self.fixture = fixture
        self.block = block
        self.keys: set[str] = set()
        self.seen: set[str] = set()
        self.asserted: set[str] = set()
        self.excused: dict[str, str] = {}
        self.prose: set[str] = set()

    def unconsumed(self) -> list[str]:
        return sorted(self.keys - self.seen - self.prose)

    def read_not_asserted(self) -> list[str]:
        """Keys some runner read but no runner compared against the fixture value."""
        satisfied = self.asserted | set(self.excused) | self.prose
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
        # every key (#lzconsumednotasserted).
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
        return sorted(set(self._data) - self._ledger.seen - self._ledger.prose)

    # -- Assertion ledger (#lzconsumednotasserted) --------------------------

    def mark_asserted(self, key: str) -> Any:
        """Record that ``key``'s fixture value reached a comparison; return it."""
        value = self[key]  # marks read
        self._ledger.asserted.add(key)
        return value

    def mark_excused(self, key: str, reason: str) -> None:
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

    return walk(fixture, "")


def reset(fixture: str | None = None) -> None:
    """Drop the session ledger (tests of the guard itself).

    With ``fixture`` set, only that fixture's ledgers are dropped, so a self-test
    can plant a deliberate violation, observe the report, and clean up after
    itself without erasing what the real runners have booked.
    """
    if fixture is None:
        _LEDGERS.clear()
        return
    for key in [key for key in _LEDGERS if key[0] == fixture]:
        del _LEDGERS[key]


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
