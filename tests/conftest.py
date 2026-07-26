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
from pathlib import Path


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

    def pytest_sessionfinish(session: object, exitstatus: object) -> None:
        """Append (not truncate) — pytest-xdist and reruns each contribute reads."""
        if not _opened:
            return
        try:
            with open(_MANIFEST, "a", encoding="utf-8") as handle:
                handle.write("\n".join(sorted(_opened)) + "\n")
        except OSError:
            # A manifest we cannot write shows up downstream as missing evidence,
            # which is correct. Never fail the suite over bookkeeping.
            pass
