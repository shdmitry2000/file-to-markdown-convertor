"""Inject Excel-style cached results into an .xlsx written by openpyxl.

Why this exists
---------------
An .xlsx stores each formula cell twice: the formula (``<f>``) and the result
Excel computed the last time it saved (``<v>``). openpyxl **writes formulas but
leaves the result empty**, so a workbook it produces returns ``None`` for every
formula cell under ``data_only=True`` — nothing like a real, Excel-saved file.

Fixtures must look like real files, so this rewrites the sheet XML to add the
results. The alternative is recalculating with LibreOffice (see README); that is
more faithful but needs a ~400MB package in the image, so it is opt-in and this
stays the zero-dependency default for CI.

Usage::

    wb.save(path)
    inject_cached(path, {"Sheet1": {"C6": 350, "D6": "n/a", "E2": "#DIV/0!"}})

Values map to Excel cell types: numbers stay numeric, strings become ``t="str"``,
and strings starting with ``#`` become error cells (``t="e"``).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path


def _cell_type_attr(value: object) -> str:
    if isinstance(value, str):
        return ' t="e"' if value.startswith("#") else ' t="str"'
    if isinstance(value, bool):
        return ' t="b"'
    return ""


def _cell_body(value: object) -> str:
    if isinstance(value, bool):
        return f"<v>{int(value)}</v>"
    return f"<v>{value}</v>"


def inject_cached(path: str | Path, cache: dict[str, dict[str, object]]) -> None:
    """Add cached results to the formula cells named in *cache*.

    Args:
        path: workbook to rewrite in place.
        cache: ``{sheet_name: {cell_ref: cached_value}}``.

    Raises:
        ValueError: if the workbook has no sheets, a named sheet is absent, or a
            named cell holds no formula. Silence here would produce a fixture
            that quietly tests nothing, so every miss is fatal.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as zin:
        items = {name: zin.read(name) for name in zin.namelist()}

    # Attribute order inside <sheet .../> is not guaranteed — openpyxl emits
    # name= last — so match the tag first and pull name= from anywhere in it.
    names = [
        m.decode()
        for m in re.findall(rb'<sheet\b[^>]*?\bname="([^"]+)"', items["xl/workbook.xml"])
    ]
    if not names:
        raise ValueError(f"{path}: no <sheet> entries in xl/workbook.xml")
    unknown = set(cache) - set(names)
    if unknown:
        raise ValueError(f"{path}: no such sheet(s): {sorted(unknown)}")

    for index, sheet in enumerate(names, start=1):
        wanted = cache.get(sheet)
        if not wanted:
            continue
        key = f"xl/worksheets/sheet{index}.xml"
        xml = items[key].decode()
        for ref, value in wanted.items():
            attrs = _cell_type_attr(value)
            body = _cell_body(value)
            # <f> may carry attributes (t="array" ref="..." for array formulas),
            # and openpyxl leaves an empty <v></v> placeholder behind it.
            pattern = (
                rf'<c r="{ref}"((?:(?!</c>).)*?)>'
                rf"(<f\b[^>]*>.*?</f>|<f\b[^>]*/>)"
                r"\s*(?:<v\s*/>|<v></v>)?\s*</c>"
            )
            xml, hits = re.subn(
                pattern,
                lambda m: f'<c r="{ref}"{m.group(1)}{attrs}>{m.group(2)}{body}</c>',
                xml,
            )
            if not hits:
                raise ValueError(f"{path}: {sheet}!{ref} holds no formula to cache")
        items[key] = xml.encode()

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, blob in items.items():
            zout.writestr(name, blob)
