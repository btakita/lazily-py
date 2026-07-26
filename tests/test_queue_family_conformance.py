"""The queue-family flavor ledger — enforced against the source, not a comment.

``test_queue_conformance.py`` replays the canonical ``queuecell_*.json`` corpus
against the single-threaded ``QueueCell``. That is currently the only flavor: no
binding in the family ships a thread-safe or async queue primitive, and
``cell-model.md`` § "Core surface vs. binding extensions (queue family)" now makes
those Core, so their absence is a conformance gap rather than an unfinished
nicety.

A three-flavor replay written today would therefore skip two of three flavors
entirely, and a suite that skips almost everything while reporting green is
exactly the failure this file exists to prevent. So the ledger is wired to the
source: it greps ``src/lazily`` for each unshipped flavor's class name, and the
moment one appears this goes red and names the runner to extend.

Mirrors ``lazily-rs/tests/queue_family_conformance.rs``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


_SPEC = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "collections"
)
_LOCAL = Path(__file__).resolve().parent / "conformance" / "collections"
_SRC = Path(__file__).resolve().parents[1] / "src" / "lazily"

QUEUE_FIXTURES = (
    "queuecell_spsc_push_pop.json",
    "queuecell_popped_head_observation.json",
    "queuecell_mpsc_multi_writer.json",
    "queuecell_bounded_backpressure.json",
    "queuecell_closure_lifecycle.json",
)

# (flavor name, the class name that proves it exists, shipped?)
#
# The marker is grepped rather than imported: importing a class that does not
# exist would fail at collection time, and a ledger you cannot write until the
# work is done is no ledger at all.
LEDGER = (
    ("single-threaded", "class QueueCell", True),
    ("thread-safe", "ThreadSafeQueueCell", False),
    ("async", "AsyncQueueCell", False),
)


def _sources() -> str:
    return "\n".join(p.read_text() for p in _SRC.rglob("*.py"))


def _fixture_dir() -> Path | None:
    if _SPEC.is_dir():
        return _SPEC
    if _LOCAL.is_dir():
        return _LOCAL
    return None


def test_unshipped_flavors_are_really_absent() -> None:
    """The ledger is enforced, not advisory.

    When a ``ThreadSafeQueueCell`` or ``AsyncQueueCell`` lands, this fails and says
    what to do — so a newly-shipped flavor cannot sit silently unreplayed while the
    suite reports green.
    """
    sources = _sources()
    assert sources, "read no sources from src/lazily; the ledger check would be vacuous"

    for name, marker, shipped in LEDGER:
        defined = marker in sources
        if shipped:
            assert defined, (
                f"flavor {name!r} is recorded as shipped but {marker!r} is not defined "
                "in src/lazily — the ledger claims coverage this package does not have"
            )
        else:
            assert not defined, (
                f"flavor {name!r} now EXISTS in src/lazily ({marker!r}) but the "
                "queue-family ledger still records it as unshipped, so the canonical "
                "corpus is not being replayed against it.\n\n"
                f"Fix: flip the {name!r} entry to shipped in LEDGER *and* extend the "
                "replay to drive it, exactly as test_collection_family_conformance.py "
                "drives all three map flavors. Do NOT flip the flag alone — that "
                "restores the false green this test exists to prevent."
            )


def test_ledger_is_not_all_skips() -> None:
    """A runner that skips everything must fail: in a summary line, "skipped" and
    "passed" are indistinguishable."""
    assert sum(1 for *_, shipped in LEDGER if shipped) > 0, (
        "every queue flavor is recorded as unshipped, so this suite would assert "
        "nothing while still reporting success"
    )
    assert len(LEDGER) == 3, (
        "the ledger must cover all three execution flavors; a missing entry is an "
        "unscored gap, not an absent one"
    )


def test_shipped_flavor_replays_the_corpus() -> None:
    """Positive proof this test module read the corpus.

    An absence guard proves the fixtures exist on disk; only a non-zero count
    proves they were opened.
    """
    directory = _fixture_dir()
    if directory is None:
        pytest.skip("canonical collections fixtures not found")

    fixtures_read = steps_seen = matrices_seen = 0
    for name in QUEUE_FIXTURES:
        path = directory / name
        assert path.exists(), f"{name}: declared queue fixture is missing"
        fixture = json.loads(path.read_text())
        fixtures_read += 1

        steps = fixture.get("steps") or []
        assert steps, (
            f"{name}: fixture has no steps - a vacuous replay would report green"
        )
        steps_seen += len(steps)

        for i, step in enumerate(steps):
            # The matrix nests under `expected`, NOT on the step. lazily-rs's MAP
            # runner read it off the step, so it was always absent and the
            # assertion never ran once. Pin the nesting so that cannot recur here.
            assert "invalidates" not in step, (
                f"{name} step {i}: `invalidates` appears at STEP level; the runners "
                "read expected.invalidates, so a step-level copy is silently ignored"
            )
            expected = step.get("expected")
            assert expected is not None, f"{name} step {i}: no expected block"
            if "invalidates" in expected:
                matrices_seen += 1

    assert fixtures_read == len(QUEUE_FIXTURES)
    assert steps_seen > 0, "read the corpus but saw zero steps"
    assert matrices_seen > 0, (
        "no fixture carried an expected.invalidates matrix - the reader-kind "
        "independence contract would be unasserted"
    )
