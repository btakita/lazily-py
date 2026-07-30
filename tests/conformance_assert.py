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

Prose keys
----------
A few canonical fixtures carry human-readable narration inside an assertion block
(``note``, ``reason``, and similar). Those are not assertions and cannot be
"implemented". Declare them per call site with ``prose=(...)`` so the exemption is
visible next to the runner that takes it, rather than buried in a global
allowlist. Everything else must be consumed or the guard fails.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any


__all__ = [
    "BLOCK_KEYS",
    "TrackedBlock",
    "consumption_failures",
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


class _Ledger:
    """Session-wide consumption record for one ``(fixture, block)`` pair."""

    __slots__ = ("block", "fixture", "keys", "seen")

    def __init__(self, fixture: str, block: str) -> None:
        self.fixture = fixture
        self.block = block
        self.keys: set[str] = set()
        self.seen: set[str] = set()

    def unconsumed(self) -> list[str]:
        return sorted(self.keys - self.seen)


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
        # Prose keys count as consumed: the declaration at the call site IS the
        # act of looking at them.
        ledger.seen.update(prose)
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
        # checks every key AND rejects keys the runner did not expect.
        self._ledger.seen.update(self._data)
        if isinstance(other, TrackedBlock):
            return self._data == other._data
        return self._data == other

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"TrackedBlock({self.fixture}:{self.block}, {self._data!r})"

    def unconsumed(self) -> list[str]:
        return sorted(set(self._data) - self._ledger.seen)


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


def reset() -> None:
    """Drop the session ledger (tests of the guard itself)."""
    _LEDGERS.clear()


def consumption_failures() -> list[str]:
    """Report lines for every ``(fixture, block)`` holding an unconsumed key."""
    lines: list[str] = []
    for (fixture, block), ledger in sorted(_LEDGERS.items()):
        missed = ledger.unconsumed()
        if missed:
            lines.append(
                f"{fixture} [{block}]: assertion key(s) {missed} present in the "
                f"fixture but never consumed by any runner"
            )
    return lines
