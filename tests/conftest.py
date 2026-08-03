"""Runtime conformance manifest (#lazilyupgradeconformance).

The static coverage guard greps test sources for fixture filenames. That catches a
fixture nobody mentions, but not one mentioned in a comment and hand-transcribed —
the drift found in lazily-cpp's queue tests, where the source names the fixture and
the bytes are never opened. Only observing the read proves the corpus was replayed.

This wraps ``Path.read_text`` / ``Path.open`` for the session rather than editing
every load site, and that is the stronger instrument rather than the lazier one: it
records what the suite REALLY read, so a runner that stops loading a fixture is
caught even though its source still names it. Editing call sites would only record
what each site claims to load.

Writes to ``LAZILY_CONFORMANCE_MANIFEST``; a no-op when unset, so a bare
``pytest tests/`` is unaffected.
"""

import os
import sys
from pathlib import Path

from conformance_assert import (
    consumption_failures,
    prose_failures,
    scenario_failures,
)


_MANIFEST = os.environ.get("LAZILY_CONFORMANCE_MANIFEST")
_MARKER = os.path.join("lazily-spec", "conformance") + os.sep
_opened: set[str] = set()


def _record(path: object) -> None:
    try:
        text = os.fspath(path)  # type: ignore[arg-type]
    except TypeError:
        return
    idx = text.find(_MARKER)
    if idx == -1:
        return
    rel = text[idx + len(_MARKER) :]
    _opened.add(rel.replace(os.sep, "/"))


# Recording is unconditional; only the *write* is gated on the env var. The
# per-scenario guard (#lzscenariocoverage) needs to know which fixtures were
# opened in order to decide which ones to hold to scenario accounting, and gating
# that on an env var would make a bare `pytest tests/` silently drop the fourth
# rung — the same vacuous green the manifest exists to prevent.
_orig_read_text = Path.read_text
_orig_open = Path.open


def _read_text(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
    _record(self)
    return _orig_read_text(self, *args, **kwargs)  # type: ignore[arg-type]


def _open(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
    _record(self)
    return _orig_open(self, *args, **kwargs)  # type: ignore[arg-type]


Path.read_text = _read_text  # type: ignore[method-assign]
Path.open = _open  # type: ignore[method-assign]


def _write_manifest() -> None:
    """Append (not truncate) — pytest-xdist and reruns each contribute reads."""
    if not _MANIFEST or not _opened:
        return
    try:
        with open(_MANIFEST, "a", encoding="utf-8") as handle:
            handle.write("\n".join(sorted(_opened)) + "\n")
    except OSError:
        # A manifest we cannot write shows up downstream as missing evidence,
        # which is correct. Never fail the suite over bookkeeping.
        pass


def pytest_sessionfinish(session, exitstatus) -> None:  # type: ignore[no-untyped-def]
    """Persist the read manifest, then fail on unconsumed keys and unreplayed scenarios.

    Consumption is a session-wide property (#lzassertunknownkeys): fixtures are
    routinely loaded by more than one test — a dedicated assertion test plus a
    parametrized round-trip sweep — and the question worth answering is whether
    ANY runner checked the key, not whether each individual load did. So the
    verdict lands here rather than in a per-test teardown.

    Per-scenario replay (#lzscenariocoverage) is session-wide for the same reason,
    and lands here for one more: the comparison is between the ledger and the
    fixture on disk, and only at the end of the run is the ledger complete. A
    fixture's four scenarios are routinely replayed by four separate tests.

    The failure is expressed as a non-zero exit status, not just printed output.
    A conformance guard whose only signal is a line in the log is the same
    silent-skip failure one level up.
    """
    _write_manifest()

    report: list[str] = []

    # Every rung below reasons about fixtures this run OPENED: no opened fixture
    # means no ledger, no unconsumed key, no unreplayed scenario — three guards
    # reporting green over an empty corpus. That is correct on a local checkout
    # without the lazily-spec sibling (the suites skip by design) and is the
    # vacuous green on CI, where the run is the proof. So on CI, opening zero
    # canonical fixtures is itself the failure.
    vacuous = bool(os.environ.get("CI")) and not _opened
    if vacuous:
        report += [
            "",
            "CONFORMANCE RUN WAS VACUOUS (#lzguardsnotinci)",
            "  This run opened ZERO fixtures from the canonical corpus, so the",
            "  assertion-key and scenario-replay guards below had nothing to check and",
            "  would have reported green. Either the lazily-spec sibling is missing or",
            "  every conformance runner skipped. Clone the corpus, or point",
            "  LAZILY_SPEC_CONFORMANCE_DIR at a copy.",
            "",
        ]

    failures = consumption_failures()
    if failures:
        report += [
            "",
            "CONFORMANCE ASSERTION LEDGER FAILURES",
            "  (#lzassertunknownkeys / #lzconsumednotasserted)",
            "  A fixture asserts something this suite never really checked: a key no",
            "  runner read, a key a runner read and then discarded, or an excuse that",
            "  has gone stale. Replaying a fixture is not testing it, and neither is",
            "  reading a key. Implement the check via assert_key/assert_key_with,",
            "  declare excuse_key(block, key, reason), or — for narration rather than",
            "  an assertion — declare it via tracked(prose=...).",
            "",
        ]
        report += [f"  {line}" for line in failures]
        report.append("")

    # The arming seam for the prose-discharge ledger (#lzprosekeyconvention).
    # `verify_prose` is what compares the discharged set against
    # `assertions.prose` and checks that every named key was really asserted, so
    # a run that never calls it has an unchecked claim sitting in the ledger —
    # exactly as bad as an unconsumed key, and reported the same way. Landing it
    # here rather than in a per-test teardown matches the other rungs: the ledger
    # is fixture-scoped and session-wide, because a paragraph in `assertions` is
    # routinely discharged by an `expect` key asserted much later in the replay.
    prose_bad = prose_failures()
    if prose_bad:
        report += [
            "",
            "CONFORMANCE PROSE DISCHARGE NEVER VERIFIED (#lzprosekeyconvention)",
            "  A fixture declares prose keys — English obligations the corpus names",
            "  in `assertions.prose` — and this run never verified how they were",
            "  discharged. Call verify_prose(fixture) at the end of the runner that",
            "  replays it.",
            "",
        ]
        report += [f"  {line}" for line in prose_bad]
        report.append("")

    scenario_bad, scenario_notes = scenario_failures(_opened)
    if scenario_notes:
        report += ["", "CONFORMANCE SCENARIO LEDGER NOTICES (#lzscenariocoverage)", ""]
        report += [f"  {line}" for line in scenario_notes]
        report.append("")
    if scenario_bad:
        report += [
            "",
            "CONFORMANCE SCENARIO LEDGER FAILURES (#lzscenariocoverage)",
            "  A fixture this suite OPENED carries a scenario it never replayed, or",
            "  an excuse for one that has gone stale. Opening a fixture is not",
            "  replaying it: the coverage guard sees one scenario and is satisfied,",
            "  and the key guards never bind a block nobody reaches. Replay the",
            "  scenario, or declare excuse_scenario(fixture, id, reason) next to",
            "  KNOWN_UNCOVERED in scripts/check-conformance-coverage.sh. A scenario",
            "  carrying neither `id` nor `name` is reported here too: the ledger",
            "  would record it by POSITION, which silently rebinds on a corpus",
            "  reorder (#lzspecscenarioids).",
            "",
        ]
        report += [f"  {line}" for line in scenario_bad]
        report.append("")

    if report:
        sys.stderr.write("\n".join(report) + "\n")
    if (failures or prose_bad or scenario_bad or vacuous) and exitstatus == 0:
        session.exitstatus = 1
