"""lazily build script.

mypyc-compiles the reactive core (``slot`` / ``cell`` / ``signal`` / ``effect``
/ ``batch``) as a single compilation unit so cross-file native-class inheritance
(``BaseSlot`` → ``SourceSlot``, ``Slot`` → ``Effect`` / ``_SignalSlot``) gets
mypyc's early binding. The public classes are decorated with
``@mypyc_attr(allow_interpreted_subclasses=True)`` so interpreted subclasses
(e.g. ``class HttpClient(Slot[...])``) keep working — this is the
``Py_TPFLAGS_BASETYPE``-equivalent that the prior mypyc attempt was missing.

Pure-Python fallback
--------------------
If mypyc is unavailable, or C compilation fails (no toolchain / unsupported
platform), the package builds as ordinary pure-Python with no extension modules.
The same ``.py`` sources then run uncompiled, so the package stays installable
everywhere — losing the mypyc speedup, keeping full correctness and the public
subclassable API. ``make compile`` rebuilds the in-place ``.so`` files for the
dev workflow; ``make build`` produces a wheel that ships the compiled ``.so``
alongside the ``.py`` sources.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence  # noqa: TC003  (runtime import for setup)

from setuptools import setup
from setuptools.command.build_ext import build_ext as _build_ext


# The reactive core, compiled as ONE compilation unit (see module docstring).
_CORE_MODULES: Sequence[str] = (
    "src/lazily/slot.py",
    "src/lazily/cell.py",
    "src/lazily/signal.py",
    "src/lazily/effect.py",
    "src/lazily/batch.py",
)


def _build_ext_modules() -> list:
    try:
        from mypyc.build import mypycify
    except Exception as exc:  # mypyc not installed in the build env
        warnings.warn(
            f"lazily: mypyc unavailable, building pure-Python ({exc!r})",
            stacklevel=2,
        )
        return []
    try:
        # ``--follow-imports=silent`` keeps mypy from surfacing pre-existing
        # mypy errors in modules reached transitively through ``lazily/__init__``
        # re-exports (this project type-checks with ``ty``, not mypy). The five
        # core modules form the compilation unit; everything else is loaded only
        # for type context and is silenced.
        return mypycify(
            ["--follow-imports=silent", *_CORE_MODULES],
            opt_level="3",
        )
    except Exception as exc:  # mypycify() config-time failure
        warnings.warn(
            f"lazily: mypycify failed, building pure-Python ({exc!r})",
            stacklevel=2,
        )
        return []


class build_ext(_build_ext):
    """``build_ext`` that downgrades to pure-Python if C compilation fails.

    Catches both whole-``run`` and per-extension failures so a missing C
    compiler (or a single misbehaving source file) cannot make the wheel
    unbuildable — the extension is simply skipped and the shipped ``.py``
    sources are used at runtime.
    """

    def run(self) -> None:
        try:
            super().run()
        except Exception as exc:
            warnings.warn(
                f"lazily: C compilation failed; shipping pure-Python ({exc!r})",
                stacklevel=2,
            )
            self.extensions = []

    def build_extension(self, ext) -> None:  # type: ignore[override]
        try:
            super().build_extension(ext)
        except Exception as exc:
            warnings.warn(
                f"lazily: building {ext.name} failed; skipped ({exc!r})",
                stacklevel=2,
            )


setup(
    ext_modules=_build_ext_modules(),
    cmdclass={"build_ext": build_ext},
)
