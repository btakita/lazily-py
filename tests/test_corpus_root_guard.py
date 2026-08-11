"""Each shared ``lazily-spec`` root has ONE seam, and this test makes that true.

TWO roots are guarded, because there are two overrides:

* ``LAZILY_SPEC_CONFORMANCE_DIR`` repoints every conformance replay at a scratch
  copy of the CORPUS, honoured by ``corpus_dir`` / ``corpus_path`` /
  ``corpus_subdir`` / ``corpus_fixture``;
* ``LAZILY_SPEC_SCHEMAS_DIR`` repoints every schema-validating runner at a
  scratch copy of the sibling ``schemas`` tree, honoured by ``schemas_dir`` /
  ``schema_path`` / ``schema_json`` (``#lzspecschemasoverride``).

Never edit the shared ``lazily-spec`` checkout in place -- ten bindings read it,
and a perturbation there reddens all of them at once. Both overrides only work
while every runner resolves through :mod:`tests.conformance_assert`. A module
that spells either default root itself is *invisible* to its override, and the
failure is silent in the worst possible way: a runner reading the unperturbed
canonical bytes while believing it was redirected is green either way, so a
probe that truncates fixtures -- or flips a field in a schema -- in the scratch
copy reddens nothing and reports the binding conformant over bytes nobody
perturbed (``#lzcorpusrootguards``, ``#lzoverrideallrunners``,
``#lzspecschemasoverride``).

That is not hypothetical. lazily-zig had 14 such sites across 12 areas, so the
override moved 2 runners of 14: truncating 14 fixtures reddened 0 tests before
the fix and 26 after. lazily-rs was worse -- 0 of 25 areas honoured it. This
binding MEASURED CLEAN (24/24 areas reddened under a truncation probe), so this
guard is regression prevention, not a repair: it converts the property from a
convention -- asserted in prose in ``corpus_path``'s own docstring -- into
something mechanically enforced on every ``make check``.

What it catches, and why the second form matters:

* the single-literal form -- any string that spells ``lazily-spec/conformance``;
* the JOINED-SEGMENT form -- ``Path("..") / "lazily-spec" / "conformance"``,
  ``os.path.join("..", "lazily-spec", "conformance")``, ``.joinpath(...)``,
  f-strings, ``+`` concatenation, and module-level string constants standing in
  for any of those segments. lazily-go's and lazily-js's guards were both
  single-literal greps, and both were proven evadable by exactly this shape.

Adjacency of the two segments is the whole test: ``lazily-spec`` followed by
``conformance`` names the corpus, ``lazily-spec`` followed by ``schemas`` names
the schemas tree, and ``lazily-spec/docs`` -- which no override moves and which
nothing validates against -- names neither.

The schemas root was NOT guarded when this module landed, on the reasoning that
it "is not overridden". That was the defect, not the exemption: the three
``parents[2] / "lazily-spec" / "schemas"`` sites this guard used to whitelist
were exactly why perturbing a schema meant dirtying the shared checkout. They
are violations now.

Comments and docstrings are skipped -- several modules legitimately quote the
path while explaining it, and the AST gives that for free (comments are dropped
by the parser; a bare string statement is prose, not a path).

A scan that examines zero files FAILS. A guard that walks nothing and reports OK
is the vacuous green this family of guards exists to prevent, and it is the
exact shape that hid the coverage guard's own silent skip.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

#: The two path segments that, spelled ADJACENTLY, name the default corpus root.
#: Adjacency is what separates the corpus from its sibling ``schemas`` tree.
CORPUS_SEGMENTS = ("lazily-spec", "conformance")

#: The same, for the sibling ``schemas`` tree ``LAZILY_SPEC_SCHEMAS_DIR`` moves.
SCHEMAS_SEGMENTS = ("lazily-spec", "schemas")

#: Every guarded root, mapped to the seam a runner must resolve through instead.
GUARDED_ROOTS: dict[tuple[str, str], str] = {
    CORPUS_SEGMENTS: (
        "conformance_assert.corpus_path()/corpus_subdir()/corpus_fixture()"
    ),
    SCHEMAS_SEGMENTS: "conformance_assert.schema_path()/schema_json()",
}

#: The only files allowed to spell either default root.
#:
#: ``conformance_assert.py`` is the seam itself -- ``corpus_dir()`` and
#: ``schemas_dir()`` are where the defaults live and where the overrides are
#: honoured (and where they fail closed). This module is on the list because a
#: guard for a spelling has to contain that spelling to test itself; every other
#: entry would be a hole.
ALLOWED = frozenset(
    {
        "tests/conformance_assert.py",
        "tests/test_corpus_root_guard.py",
    }
)

#: Directories that are not this repo's source: build output, vendored wheels,
#: caches, and agent worktree copies. Every dotted directory is skipped too,
#: which covers ``.venv``, ``.git``, ``.ruff_cache``, and ``.claude/worktrees``.
SKIP_DIRS = frozenset(
    {
        "build",
        "dist",
        "dist_repaired",
        "publish-dist",
        "wheelhouse",
        "dl-artifacts",
        "node_modules",
        "__pycache__",
        "venv",
        "env",
    }
)

#: The floor for "this scan actually looked at this repo". The tree carries ~130
#: Python files; a scan that finds a handful has lost its root, and reporting OK
#: from there is the vacuous pass.
MIN_FILES_EXAMINED = 50


class GuardError(RuntimeError):
    """The scan could not produce evidence -- not a finding, a broken scan."""


class Violation:
    """One source site that spells a guarded default root."""

    def __init__(self, path: str, line: int, detail: str) -> None:
        self.path = path
        self.line = line
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.detail}"

    __repr__ = __str__


def _split_segments(text: str) -> list[str | None]:
    """A literal path string, as its segments. Both separators, on purpose."""
    return text.replace("\\", "/").split("/")


def _fold_text(node: ast.AST, consts: dict[str, str]) -> str | None:
    """The string this expression is, when it is one statically."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_text(node.left, consts)
        right = _fold_text(node.right, consts)
        if left is not None and right is not None:
            return left + right
    return None


def _segments(node: ast.AST, consts: dict[str, str]) -> list[str | None]:
    """The path segments an expression builds, ``None`` where unresolvable.

    This is the joined-segment machinery. Anything that concatenates -- ``/``
    on paths, call arguments, list/tuple elements, f-string parts, ``+`` on
    strings -- contributes its own segments in order, so the two corpus segments
    land next to each other however they were spelled.
    """
    text = _fold_text(node, consts)
    if text is not None:
        return _split_segments(text)

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _segments(node.left, consts) + _segments(node.right, consts)

    if isinstance(node, ast.JoinedStr):
        parts: list[str | None] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.extend(_split_segments(value.value))
            else:
                parts.append(None)
        return parts

    if isinstance(node, ast.Call):
        # Deliberately every call, not a whitelist of ``Path``/``os.path.join``:
        # a helper of any name that is handed these segments in order is
        # building this path, and naming the constructors is how a guard gets
        # evaded.
        parts = []
        for arg in node.args:
            inner = arg.value if isinstance(arg, ast.Starred) else arg
            parts.extend(_segments(inner, consts))
        return parts

    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        parts = []
        for element in node.elts:
            inner = element.value if isinstance(element, ast.Starred) else element
            parts.extend(_segments(inner, consts))
        return parts

    return [None]


def _spells_root(segments: list[str | None], root: tuple[str, str]) -> bool:
    """True when ``root``'s segments appear adjacently, in order."""
    first, second = root
    for index in range(len(segments) - 1):
        if segments[index] == first and segments[index + 1] == second:
            return True
    return False


def _root_spelled(segments: list[str | None]) -> tuple[str, str] | None:
    """The guarded root this expression spells, or ``None``."""
    for root in GUARDED_ROOTS:
        if _spells_root(segments, root):
            return root
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so a named segment resolves."""
    consts: dict[str, str] = {}
    for statement in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
            value = statement.value
        if value is None:
            continue
        text = _fold_text(value, consts)
        if text is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = text
    return consts


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Every string that is a bare statement: a docstring, i.e. prose, not a path.

    Comments need no handling -- the parser has already dropped them.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            ids.add(id(node.value))
    return ids


def violations_in_source(source: str, label: str) -> list[Violation]:
    """Every site in one module that spells a guarded default root."""
    tree = ast.parse(source, filename=label)
    consts = _module_constants(tree)
    skip = _docstring_ids(tree)
    found: list[Violation] = []

    def visit(node: ast.AST) -> None:
        if id(node) in skip:
            return
        if isinstance(node, ast.expr) and isinstance(
            getattr(node, "ctx", None), ast.Store | ast.Del
        ):
            # An assignment TARGET is not a path expression; the name being
            # bound resolves to the same literal and would double-report.
            return
        if isinstance(node, ast.expr):
            root = _root_spelled(_segments(node, consts))
            if root is not None:
                found.append(
                    Violation(
                        label,
                        getattr(node, "lineno", 0),
                        f"spells the default {'/'.join(root)} root instead of "
                        f"resolving it through {GUARDED_ROOTS[root]}",
                    )
                )
                return  # outermost site only; its parts are the same finding
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def python_sources(root: Path) -> list[Path]:
    """This repo's Python sources, excluding build output and worktree copies."""
    sources: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".")
            and name not in SKIP_DIRS
            and not name.endswith(".egg-info")
        ]
        for name in filenames:
            if name.endswith(".py"):
                sources.append(Path(dirpath) / name)
    return sorted(sources)


def scan_tree(root: Path, allowed: frozenset[str] = ALLOWED) -> tuple[int, list[str]]:
    """Scan ``root``; return ``(files_examined, violations)``.

    Raises :class:`GuardError` when the walk examined ZERO files. An empty or
    misdirected tree is a broken scan, and a broken scan that returns "no
    violations" is indistinguishable from a clean repo -- which is precisely the
    failure mode this whole family of guards exists to make impossible.
    """
    root = Path(root)
    examined = 0
    findings: list[str] = []
    for source in python_sources(root):
        relative = source.relative_to(root).as_posix()
        examined += 1
        if relative in allowed:
            continue
        findings.extend(
            str(violation)
            for violation in violations_in_source(
                source.read_text(encoding="utf-8"), relative
            )
        )
    if examined == 0:
        raise GuardError(
            f"spec-root guard examined ZERO Python files under {root}. "
            f"A scan that walks nothing and reports OK is the vacuous green this "
            f"guard exists to prevent, so this is a failure rather than a pass "
            f"(#lzcorpusrootguards)."
        )
    return examined, findings


def test_no_source_outside_the_seam_spells_a_guarded_root() -> None:
    examined, findings = scan_tree(REPO_ROOT)
    assert examined >= MIN_FILES_EXAMINED, (
        f"spec-root guard examined only {examined} Python files under "
        f"{REPO_ROOT} (expected at least {MIN_FILES_EXAMINED}). The scan lost "
        f"its root; a green verdict from here would be vacuous."
    )
    assert not findings, (
        "these sources compute a shared lazily-spec root themselves, so "
        "LAZILY_SPEC_CONFORMANCE_DIR / LAZILY_SPEC_SCHEMAS_DIR do not move them "
        "and a perturbation probe would read unperturbed canonical bytes and "
        "report green (#lzcorpusrootguards, #lzspecschemasoverride). Resolve "
        "through conformance_assert.corpus_path()/corpus_subdir()/"
        "corpus_fixture() or conformance_assert.schema_path()/schema_json() "
        "instead:\n  " + "\n  ".join(findings)
    )


def test_the_scan_reached_the_seam_itself() -> None:
    """Positive evidence the walk found this repo, not some empty directory."""
    examined = {
        path.relative_to(REPO_ROOT).as_posix() for path in python_sources(REPO_ROOT)
    }
    for expected in ALLOWED:
        assert expected in examined, (
            f"{expected} was not examined by the spec-root scan; the walk is "
            f"not looking at this repo."
        )


def test_the_detector_fires_on_the_real_seam() -> None:
    """The live detector, run against the one file that really spells the roots.

    Without this the suite could pass with a detector that matches nothing at
    all -- the allowlist would hide it. ``conformance_assert.py`` is known to
    contain BOTH defaults, so both must come back as findings when it is not
    excused. Asserting per-root rather than "findings is non-empty" is what
    stops the corpus half from vouching for the schemas half.
    """
    seam = REPO_ROOT / "tests" / "conformance_assert.py"
    findings = violations_in_source(
        seam.read_text(encoding="utf-8"), "tests/conformance_assert.py"
    )
    for root in GUARDED_ROOTS:
        spelling = "/".join(root)
        assert any(spelling in finding.detail for finding in findings), (
            f"the detector found no {spelling} site in the seam that defines "
            f"that default -- it is matching nothing for this root, and every "
            f"green above is vacuous for it. Findings were: {findings}"
        )


def test_detects_the_single_literal_form() -> None:
    source = 'CORPUS = "../lazily-spec/conformance"\n'
    findings = violations_in_source(source, "synthetic.py")
    assert len(findings) == 1, findings
    assert findings[0].line == 1


@pytest.mark.parametrize(
    "source",
    [
        'from pathlib import Path\nP = Path("..") / "lazily-spec" / "conformance"\n',
        'import os\nP = os.path.join("..", "lazily-spec", "conformance")\n',
        'P = HERE.parents[2] / "lazily-spec" / "conformance" / "collections"\n',
        'P = HERE.joinpath("lazily-spec", "conformance")\n',
        'SPEC = "lazily-spec"\nP = ROOT / SPEC / "conformance"\n',
        'P = f"{ROOT}/lazily-spec/conformance"\n',
        'P = "lazily-spec" + "/conformance"\n',
        'P = os.sep.join(["..", "lazily-spec", "conformance"])\n',
        'P = load(*["lazily-spec", "conformance", "queue"])\n',
    ],
)
def test_detects_the_joined_segment_form(source: str) -> None:
    """The shape lazily-go's and lazily-js's single-literal greps both missed."""
    assert violations_in_source(source, "synthetic.py"), source


@pytest.mark.parametrize(
    "source",
    [
        '"""Replays ../lazily-spec/conformance/queue fixtures."""\n',
        "x = 1  # reads ../lazily-spec/conformance/collections\n",
        'def f():\n    """See lazily-spec/conformance/presence."""\n    return 1\n',
        'X = 1\n"""Attribute prose about lazily-spec/conformance."""\n',
    ],
)
def test_ignores_comments_and_docstrings(source: str) -> None:
    assert violations_in_source(source, "synthetic.py") == []


@pytest.mark.parametrize(
    "source",
    [
        'S = Path(__file__).resolve().parents[2] / "lazily-spec" / "schemas"\n',
        'S = os.path.join("..", "lazily-spec", "schemas", "defs.json")\n',
        'S = "../lazily-spec/schemas"\n',
        'SPEC = "lazily-spec"\nS = ROOT / SPEC / "schemas" / "defs.json"\n',
        'S = f"{ROOT}/lazily-spec/schemas/message-passing.json"\n',
    ],
)
def test_detects_the_schemas_root(source: str) -> None:
    """The exact three shapes this repo carried before #lzspecschemasoverride.

    They were WHITELISTED here on the reasoning that the schemas tree "is not
    overridden". It is now, so computing it outside the seam is invisible to
    LAZILY_SPEC_SCHEMAS_DIR in precisely the way the corpus sites were.
    """
    findings = violations_in_source(source, "synthetic.py")
    assert findings, source
    assert "lazily-spec/schemas" in findings[0].detail, findings


@pytest.mark.parametrize(
    "source",
    [
        'S = "../lazily-spec/docs/state-charts.md"\n',
        'S = ROOT / "lazily-spec" / "scripts" / "check-assertion-ordering.py"\n',
        'S = "lazily-specific/conformance"\n',
    ],
)
def test_ignores_unguarded_lazily_spec_trees(source: str) -> None:
    """No override moves these, and nothing validates against them."""
    assert violations_in_source(source, "synthetic.py") == []


def test_an_empty_tree_fails_rather_than_reporting_ok(tmp_path: Path) -> None:
    with pytest.raises(GuardError, match="ZERO Python files"):
        scan_tree(tmp_path)


def test_a_nonexistent_tree_fails_rather_than_reporting_ok(tmp_path: Path) -> None:
    with pytest.raises(GuardError, match="ZERO Python files"):
        scan_tree(tmp_path / "does-not-exist")
