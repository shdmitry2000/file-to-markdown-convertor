"""Model -> markdown.

Layout follows what a retriever needs, not what a spreadsheet looks like:

* one markdown table per region, so a sheet holding three blocks does not
  collapse into one mangled table;
* a **Formulas** section where each line is self-contained — sheet, label,
  result and the expanded expression — so it still answers correctly if the
  chunker separates it from its table;
* a **Dependencies** section giving the reverse edges, which is what answers
  "what is this total made of" and "what breaks if this changes".

Nothing is dropped silently: truncated reads, hidden rows and formulas with no
cached result are all stated in the output.
"""

from __future__ import annotations

from .formulas import FormulaEngine
from .labels import LabelResolver
from .model import Sheet, WorkbookModel, a1, column_letter
from .regions import Region, find_regions
from .values import format_value

MAX_DEPENDENCY_LINES = 300


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _cell_text(sheet: Sheet, row: int, col: int) -> str:
    cell = sheet.effective_cell(row, col)
    if cell is None or cell.is_blank:
        return ""
    return _escape(format_value(cell.value, cell.number_format))


def _column_heading(sheet: Sheet, region: Region, col: int) -> str:
    parts: list[str] = []
    for header_row in region.header_rows:
        cell = sheet.effective_cell(header_row, col)
        if cell is None or cell.is_blank:
            continue
        text = _escape(format_value(cell.value, cell.number_format))
        if text and text not in parts:
            parts.append(text)
    return " > ".join(parts) if parts else column_letter(col)


def _render_region(sheet: Sheet, region: Region) -> list[str]:
    lines: list[str] = []
    if region.title:
        lines.append(f"**{_escape(region.title)}**")
        lines.append("")

    columns = list(range(region.rect.left, region.rect.right + 1))
    headings = [_column_heading(sheet, region, col) for col in columns]
    lines.append("| " + " | ".join(headings) + " |")
    lines.append("|" + "|".join(["---"] * len(columns)) + "|")

    for row in range(region.data_top, region.rect.bottom + 1):
        values = [_cell_text(sheet, row, col) for col in columns]
        if not any(values):
            continue
        marker = " _(hidden row)_" if row in sheet.hidden_rows else ""
        lines.append("| " + " | ".join(values) + " |" + marker)
    lines.append("")
    return lines


def _render_formulas(sheet: Sheet, engine: FormulaEngine) -> list[str]:
    entries = []
    for (row, col), cell in sorted(sheet.cells.items()):
        if not cell.formula:
            continue
        info = engine.describe(sheet, row, col)
        if info is not None:
            entries.append(info)
    if not entries:
        return []

    lines = [f"### Formulas / נוסחאות — {sheet.name}", ""]
    for info in entries:
        lines.append(f"- **{info.ref}** · {_escape(info.label)} = {_escape(info.result)}")
        lines.append(f"  - `={info.formula}` = {_escape(info.expression)}")
        for note in info.notes:
            lines.append(f"  - _{note}_")
    lines.append("")
    return lines


def _render_dependencies(sheet: Sheet, engine: FormulaEngine, graph: dict) -> list[str]:
    lines: list[str] = []
    for (sheet_name, row, col), consumers in sorted(graph.items()):
        if sheet_name != sheet.name:
            continue
        source = f"{_escape(engine.label_of(sheet_name, row, col))} ({a1(row, col)})"
        targets = ", ".join(
            f"{_escape(engine.label_of(target_sheet, target_row, target_col))} "
            f"({target_sheet}!{a1(target_row, target_col)})"
            for target_sheet, target_row, target_col in consumers
        )
        lines.append(f"- {source} → feeds {targets}")
    if not lines:
        return []
    truncated = len(lines) > MAX_DEPENDENCY_LINES
    if truncated:
        dropped = len(lines) - MAX_DEPENDENCY_LINES
        lines = lines[:MAX_DEPENDENCY_LINES]
        lines.append(f"- _… {dropped} further dependency lines omitted_")
    return [f"### Dependencies / תלויות — {sheet.name}", "", *lines, ""]


def render_markdown(
    model: WorkbookModel,
    max_refs: int = 20,
    depth: int = 1,
    emit_dependencies: bool = True,
) -> str:
    """Render the whole workbook."""
    resolvers: dict[str, LabelResolver] = {}
    regions: dict[str, list[Region]] = {}
    for sheet in model.sheets:
        regions[sheet.name] = find_regions(sheet)
        resolvers[sheet.name] = LabelResolver(sheet, regions[sheet.name], model.defined_names)

    engine = FormulaEngine(model, resolvers, max_refs=max_refs, depth=depth)
    graph = engine.dependents() if emit_dependencies else {}

    lines: list[str] = []
    for sheet in model.sheets:
        notes = []
        if sheet.rtl:
            notes.append("right-to-left")
        if sheet.hidden:
            notes.append("hidden sheet")
        if sheet.truncated:
            notes.append("truncated — cell budget reached")
        suffix = f" _({', '.join(notes)})_" if notes else ""
        lines.append(f"## {sheet.name}{suffix}")
        lines.append("")

        sheet_regions = regions[sheet.name]
        if not sheet_regions:
            lines.append("_empty sheet_")
            lines.append("")
            continue
        for region in sheet_regions:
            lines.extend(_render_region(sheet, region))
        lines.extend(_render_formulas(sheet, engine))
        if emit_dependencies:
            lines.extend(_render_dependencies(sheet, engine, graph))

    return "\n".join(lines).rstrip() + "\n"
