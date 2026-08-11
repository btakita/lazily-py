"""The schemas root has ONE seam, and ``LAZILY_SPEC_SCHEMAS_DIR`` moves it.

``LAZILY_SPEC_CONFORMANCE_DIR`` repoints the conformance CORPUS and nothing
else. Every schema-validating runner resolved ``lazily-spec/schemas`` to the
canonical sibling checkout, so a probe that needed to perturb a SCHEMA had
nowhere to point but the shared repo — and perturbing that reddens all ten
bindings at once and dirties a tree nine other sessions read
(``#lzspecschemasoverride``).

These tests pin the properties that make the override worth having. The one that
matters most is the LAST one: the override has to change which BYTES are
validated against, not merely which path gets printed. An override that resolves
correctly and is then ignored by the readers is the same vacuous green as no
override at all.

``tests/test_corpus_root_guard.py`` enforces the other half — that no source
outside :mod:`conformance_assert` computes the schemas root, which is what makes
"one seam" true rather than merely intended.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from conformance_assert import (
    SCHEMAS_DIR_ENV,
    _reset_schemas_dir,
    corpus_dir,
    schema_json,
    schema_path,
    schemas_dir,
    schemas_override,
)


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _restore_resolution() -> Iterator[None]:
    """Drop the memoised root around each test, and restore it afterwards.

    The memo is process-wide on purpose (a run must not change which schemas it
    reads halfway through), so a test that moves the env has to clear it on both
    sides or it leaks its answer into the rest of the session.
    """
    _reset_schemas_dir()
    yield
    _reset_schemas_dir()


def test_the_default_is_the_corpus_sibling_and_is_unchanged_by_this_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing set, the schemas tree is where it always was.

    Spelled as the corpus root's sibling rather than as
    ``lazily-spec``/``schemas`` because this module is not the seam: spelling it
    here is exactly what the root guard forbids.
    """
    monkeypatch.delenv(SCHEMAS_DIR_ENV, raising=False)
    monkeypatch.delenv("LAZILY_SPEC_CONFORMANCE_DIR", raising=False)
    _reset_schemas_dir()
    assert schemas_override() is None
    assert schemas_dir() == corpus_dir().parent / "schemas"


def test_an_empty_override_is_not_an_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LAZILY_SPEC_CONFORMANCE_DIR", raising=False)
    monkeypatch.setenv(SCHEMAS_DIR_ENV, "")
    _reset_schemas_dir()
    assert schemas_override() is None
    assert schemas_dir() == corpus_dir().parent / "schemas"


def test_an_override_moves_the_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(SCHEMAS_DIR_ENV, str(tmp_path))
    _reset_schemas_dir()
    assert schemas_dir() == tmp_path


def test_an_absent_override_directory_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NOT a skip, and not a silent fallback to the canonical checkout.

    An override naming a directory that is not there is a broken probe. Falling
    back would validate against unperturbed canonical bytes while the operator
    believes the run was redirected — green either way.
    """
    missing = tmp_path / "not-there"
    monkeypatch.setenv(SCHEMAS_DIR_ENV, str(missing))
    _reset_schemas_dir()
    with pytest.raises(RuntimeError, match="does not name a readable directory"):
        schemas_dir()
    # And it stays failed: a memo that cached the failure as a path would let the
    # second reader through.
    with pytest.raises(RuntimeError, match=SCHEMAS_DIR_ENV):
        schema_json("defs.json")


def test_a_missing_schema_file_under_an_override_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A partial copy is a broken probe too, not a reason to read the canonical file."""
    monkeypatch.setenv(SCHEMAS_DIR_ENV, str(tmp_path))
    _reset_schemas_dir()
    with pytest.raises(AssertionError, match=r"has no 'defs\.json'"):
        schema_path("defs.json")


def test_the_root_is_resolved_once_per_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The schemas a run reads cannot change halfway through it."""
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setenv(SCHEMAS_DIR_ENV, str(first))
    _reset_schemas_dir()
    assert schemas_dir() == first
    monkeypatch.setenv(SCHEMAS_DIR_ENV, str(second))
    assert schemas_dir() == first


def test_the_override_changes_which_bytes_are_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The property the whole seam exists for.

    Resolving to a scratch directory is worthless if the readers keep opening the
    canonical file. This writes a schema the canonical tree does not contain and
    proves ``schema_json`` returns THOSE bytes.
    """
    monkeypatch.delenv(SCHEMAS_DIR_ENV, raising=False)
    _reset_schemas_dir()
    canonical = json.loads(schema_path("defs.json").read_text(encoding="utf-8"))

    perturbed = dict(canonical)
    perturbed["title"] = "perturbed-by-the-schemas-override-probe"
    (tmp_path / "defs.json").write_text(json.dumps(perturbed), encoding="utf-8")

    monkeypatch.setenv(SCHEMAS_DIR_ENV, str(tmp_path))
    _reset_schemas_dir()
    assert schema_json("defs.json")["title"] == (
        "perturbed-by-the-schemas-override-probe"
    )
    assert canonical.get("title") != "perturbed-by-the-schemas-override-probe"
