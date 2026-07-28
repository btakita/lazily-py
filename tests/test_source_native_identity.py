"""Native-class identity migration for the v2 Source vocabulary.

These assertions run against whichever ``lazily.cell`` artifact Python loaded:
the interpreted source or the in-place mypyc extension. A stale extension still
exporting ``Cell`` as the native class therefore fails instead of making the
rename look green.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from lazily import Cell, CellSlot, Source, SourceSlot, source


def test_source_is_the_loaded_native_class_identity() -> None:
    loaded = sys.modules["lazily.cell"]
    origin = getattr(loaded, "__file__", "")

    assert origin, "lazily.cell has no import origin"
    assert loaded.Source is Source
    assert loaded.Cell is Source
    assert Cell is Source
    assert Source.__name__ == "Source"
    assert Source.__qualname__ == "Source"
    assert Source.__module__ == "lazily.cell"

    node = Source({}, 1)
    legacy_node = Cell({}, 2)
    assert type(node) is Source
    assert type(legacy_node) is Source
    assert isinstance(node, Cell)
    assert isinstance(legacy_node, Source)


def test_source_slot_factory_and_alias_preserve_identity() -> None:
    loaded = sys.modules["lazily.cell"]

    assert loaded.SourceSlot is SourceSlot
    assert loaded.CellSlot is SourceSlot
    assert CellSlot is SourceSlot
    assert SourceSlot.__name__ == "SourceSlot"
    assert SourceSlot.__qualname__ == "SourceSlot"
    assert SourceSlot.__module__ == "lazily.cell"

    canonical_slot = source(lambda _ctx: 1)
    legacy_slot = CellSlot(lambda _ctx: 2)
    assert type(canonical_slot) is SourceSlot
    assert type(legacy_slot) is SourceSlot
    assert type(canonical_slot({})) is Source
    assert type(legacy_slot({})) is Source


def test_interpreted_fallback_uses_the_same_canonical_identity() -> None:
    loaded = sys.modules["lazily.cell"]
    source_path = Path(loaded.__file__).with_name("cell.py")
    module_name = "lazily._interpreted_cell_identity_test"
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    assert spec is not None
    assert spec.loader is not None

    fallback = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = fallback
    try:
        spec.loader.exec_module(fallback)
    finally:
        sys.modules.pop(module_name, None)

    assert fallback.Source.__name__ == "Source"
    assert fallback.SourceSlot.__name__ == "SourceSlot"
    assert fallback.Cell is fallback.Source
    assert fallback.CellSlot is fallback.SourceSlot
