"""Strip docling's column padding from markdown tables.

docling pads every cell to its column's widest cell. One long free-text cell in a
Word form (a description, a lessons-learned box) made every row of its table
thousands of characters of spaces and dashes: on a typical form 87% of the
markdown was padding, and a chunker windowing the table produced chunks of
nothing but `|   |---|`, each one enriched and embedded (2026-11-01, v2 on OCP).

Cells keep their text; separator cells become `---` (alignment colons kept), the
shape every table reader here expects (two or more dashes).
"""

from __future__ import annotations

import re

_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_SEP_CELL = re.compile(r"\s*(:?)-+(:?)\s*")


def _compact_row(line: str) -> str:
    cells = _CELL_SPLIT.split(line.strip())
    # A row is `| a | b |`: the split leaves an empty first and last item.
    inner = cells[1:-1] if len(cells) > 2 else cells
    if inner and all(_SEP_CELL.fullmatch(c) for c in inner):
        parts = [f"{m.group(1)}---{m.group(2)}" for m in (_SEP_CELL.fullmatch(c) for c in inner)]
    else:
        parts = [c.strip() for c in inner]
    return "| " + " | ".join(parts) + " |"


def compact_tables(markdown: str) -> str:
    """`markdown` with every table row (a line starting and ending with `|`)
    stripped of cell padding. Everything else is left exactly as it was."""
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if len(s) > 1 and s.startswith("|") and s.endswith("|"):
            lines[i] = _compact_row(s)
    return "\n".join(lines)
