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

from conformance_assert import consumption_failures


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


if _MANIFEST:
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
    """Persist the read manifest, then fail on unconsumed assertion keys.

    Consumption is a session-wide property (#lzassertunknownkeys): fixtures are
    routinely loaded by more than one test — a dedicated assertion test plus a
    parametrized round-trip sweep — and the question worth answering is whether
    ANY runner checked the key, not whether each individual load did. So the
    verdict lands here rather than in a per-test teardown.

    The failure is expressed as a non-zero exit status, not just printed output.
    A conformance guard whose only signal is a line in the log is the same
    silent-skip failure one level up.
    """
    _write_manifest()

    failures = consumption_failures()
    if not failures:
        return
    report = [
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
    sys.stderr.write("\n".join(report) + "\n")
    if exitstatus == 0:
        session.exitstatus = 1
