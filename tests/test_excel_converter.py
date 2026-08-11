"""Tests for the formula-aware Excel converter.

Runs against the fixture corpus in tests/fixtures/excel (rebuild with
``python tests/fixtures/excel/build_fixtures.py``). The assertions are about
meaning, not formatting: which cells were found, what they are called, what a
formula is made of, and — as importantly — what the converter refuses to invent.
"""

from pathlib import Path

import pytest

from app.converters.excel import ExcelConverter
from app.converters.excel.formulas import FormulaEngine
from app.converters.excel.labels import LabelResolver, coverage
from app.converters.excel.reader import read_workbook
from app.converters.excel.regions import find_regions
from app.converters.excel.render_md import render_markdown
from app.converters.excel.values import NO_RESULT

FIXTURES = Path(__file__).parent / "fixtures" / "excel"
HEB = FIXTURES / "heb_loans_rtl.xlsx"
CROSSTAB = FIXTURES / "crosstab_multiheader.xlsx"
MULTI = FIXTURES / "multi_region.xlsx"
EDGE = FIXTURES / "edge_cases.xlsx"
STATEMENTS = FIXTURES / "three_statement_heb.xlsx"
LEGACY_XLS = FIXTURES / "legacy_biff8.xls"

pytestmark = pytest.mark.skipif(
    not HEB.exists(), reason="fixture corpus not built (tests/fixtures/excel)"
)


def analyze(path: Path):
    """Read a workbook and build the per-sheet resolvers and engine."""
    model = read_workbook(path)
    resolvers = {
        sheet.name: LabelResolver(sheet, find_regions(sheet), model.defined_names)
        for sheet in model.sheets
    }
    return model, resolvers, FormulaEngine(model, resolvers)


def describe(path: Path, sheet_name: str, ref: str):
    model, _, engine = analyze(path)
    sheet = model.sheet(sheet_name)
    column = ord(ref[0]) - 64
    return engine.describe(sheet, int(ref[1:]), column)


# -- reader ------------------------------------------------------------------


def test_reader_captures_the_metadata_that_labels_depend_on():
    model = read_workbook(HEB)
    sheet = model.sheets[0]
    assert sheet.rtl is True
    assert sheet.freeze_row == 2                      # 'A3' freezes rows 1-2
    assert sheet.autofilter is not None
    assert [rect.ref for rect in sheet.merged] == ["A1:E1"]
    assert model.defined_names == {"סך_יתרות": "הלוואות!$C$8"}
    assert sheet.cells[(8, 3)].comment == "סך יתרות ההלוואות בכל הבנקים"


def test_reader_keeps_formula_and_cached_result_together():
    model = read_workbook(HEB)
    total = model.sheets[0].cells[(8, 3)]
    assert total.formula == "SUM(C3:C7)"
    assert total.value == 2_736_000


def test_reader_marks_a_formula_with_no_saved_result():
    model = read_workbook(EDGE)
    cell = model.sheet("NoCache").cells[(3, 1)]
    assert cell.formula == "SUM(A1:A2)"
    assert cell.value is None


def test_reader_recognises_array_formulas_and_tables():
    model = read_workbook(EDGE)
    sheet = model.sheet("Edge")
    assert sheet.cells[(1, 6)].is_array_formula is True
    assert [(t.name, t.columns) for t in sheet.tables] == [
        ("Parts", ["item", "qty", "price"])
    ]


# -- regions -----------------------------------------------------------------


def test_independent_tables_on_one_sheet_stay_independent():
    model = read_workbook(MULTI)
    regions = find_regions(model.sheets[0])
    assert [region.title for region in regions] == [
        "Q1 Revenue by Region",
        "Headcount by Department",
        "Cost per Employee",
    ]


def test_two_level_header_is_detected_as_two_header_rows():
    model = read_workbook(CROSSTAB)
    region = find_regions(model.sheets[0])[0]
    assert region.header_rows == [1, 2]
    assert region.label_cols == [1]
    assert region.orientation.value == "matrix"


def test_a_title_row_is_not_mistaken_for_a_header():
    model = read_workbook(HEB)
    region = find_regions(model.sheets[0])[0]
    assert region.title == "תיק הלוואות — רבעון 2, 2026"
    assert region.header_rows == [2]
    assert region.header_source == "freeze"


# -- labels ------------------------------------------------------------------


def test_author_written_comment_wins_over_inferred_headers():
    model, resolvers, _ = analyze(HEB)
    label = resolvers["הלוואות"].label_for(8, 3)
    assert label.rung == "comment"
    assert label.text == "סך יתרות ההלוואות בכל הבנקים"
    assert label.unit == "₪"


def test_label_combines_row_and_column_and_carries_units():
    model, resolvers, _ = analyze(HEB)
    label = resolvers["הלוואות"].label_for(3, 5)
    assert label.row_label == "הפועלים · משכנתא"
    assert label.col_label == "תשלום חודשי"
    assert label.qualified == "הפועלים · משכנתא · תשלום חודשי (₪)"


def test_totals_row_does_not_inherit_the_category_above_it():
    """A blank sub-label on a totals row must not read as the last detail row."""
    _, resolvers, _ = analyze(HEB)
    label = resolvers["הלוואות"].label_for(8, 5)
    assert "צרכנית" not in label.text
    assert label.text.startswith('סה"כ')


def test_multi_level_header_reads_top_down():
    _, resolvers, _ = analyze(CROSSTAB)
    assert resolvers["Budget"].label_for(6, 2).col_label == "2026 > Q1"


def test_every_formula_in_a_headed_sheet_gets_a_real_label():
    """The metric that decides whether an AI labelling pass is needed at all."""
    labels = []
    for path in (HEB, CROSSTAB, MULTI, STATEMENTS):
        model, resolvers, _ = analyze(path)
        for sheet in model.sheets:
            labels += [
                resolvers[sheet.name].label_for(row, col)
                for (row, col), cell in sheet.cells.items()
                if cell.formula
            ]
    counts = coverage(labels)
    assert counts.get("address", 0) == 0, counts
    assert sum(counts.values()) == 43


# -- formulas ----------------------------------------------------------------


def test_sum_becomes_a_list_of_labelled_terms():
    info = describe(HEB, "הלוואות", "C8")
    assert info.result == "2,736,000"
    assert "הפועלים · משכנתא · יתרה (₪)=850,000" in info.expression
    assert info.expression.count(" + ") == 4


def test_arithmetic_formula_substitutes_each_reference():
    info = describe(HEB, "הלוואות", "E3")
    assert info.expression == (
        "(הפועלים · משכנתא · יתרה (₪)=850,000)*"
        "(הפועלים · משכנתא · ריבית (%)=3.2%)/12"
    )


def test_cross_sheet_reference_is_named_by_its_own_sheet():
    info = describe(STATEMENTS, "מאזן", "B6")
    assert info.formula == "'רווח והפסד'!B8"
    assert "רווח נקי · 2024=1,463,000" in info.expression


def test_dependencies_run_both_ways():
    model, _, engine = analyze(HEB)
    graph = engine.dependents()
    assert ("הלוואות", 8, 3) in graph                    # the total feeds D8
    consumers = graph[("הלוואות", 3, 3)]                 # C3 feeds E3 and the total
    assert ("הלוואות", 8, 3) in consumers


def test_dynamic_reference_is_flagged_rather_than_guessed():
    info = describe(EDGE, "Edge", "F4")
    assert "INDIRECT" in info.expression                 # left as written
    assert any("calculation time" in note for note in info.notes)


def test_missing_result_is_reported_not_computed():
    info = describe(EDGE, "NoCache", "A3")
    assert info.result == NO_RESULT
    assert any("no cached result" in note for note in info.notes)


def test_structured_table_reference_resolves_to_its_column():
    info = describe(EDGE, "Edge", "F3")
    assert info.expression == "Parts · qty=100 + Parts · qty=250 + Parts · qty=80"


def test_whole_column_reference_excludes_the_header_caption():
    info = describe(EDGE, "Edge", "F2")
    assert "=qty" not in info.expression                 # B1 is a caption, not a term
    assert "hidden item · qty=999" in info.expression    # Excel does count hidden rows


def test_external_workbook_reference_is_marked_unresolved():
    model, resolvers, engine = analyze(EDGE)
    sheet = model.sheet("Edge")
    refs = engine.parse_refs("[1]Book!A1+1", sheet)
    assert [ref.kind.value for ref in refs] == ["external"]


def test_reference_expansion_is_capped():
    model, resolvers, _ = analyze(HEB)
    engine = FormulaEngine(model, resolvers, max_refs=2)
    info = engine.describe(model.sheets[0], 8, 3)
    assert info.expression.endswith(" + …")
    assert any("truncated" in note for note in info.notes)


def test_nested_expansion_is_opt_in():
    model, resolvers, _ = analyze(HEB)
    flat = FormulaEngine(model, resolvers, depth=1).describe(model.sheets[0], 8, 5)
    deep = FormulaEngine(model, resolvers, depth=2).describe(model.sheets[0], 8, 5)
    assert "[" not in flat.expression
    assert "יתרה (₪)=850,000" in deep.expression         # one level further down


# -- rendering ---------------------------------------------------------------


def test_each_region_renders_as_its_own_table():
    model = read_workbook(MULTI)
    markdown = render_markdown(model)
    assert markdown.count("|---|---|") == 3
    assert "**Q1 Revenue by Region**" in markdown


def test_rendered_sheet_carries_formulas_and_dependencies():
    markdown = render_markdown(read_workbook(HEB))
    assert "### Formulas / נוסחאות — הלוואות" in markdown
    assert "### Dependencies / תלויות — הלוואות" in markdown
    assert "→ feeds" in markdown


def test_right_to_left_sheets_are_marked():
    assert "_(right-to-left)_" in render_markdown(read_workbook(HEB))


def test_percentages_and_currency_render_as_displayed():
    markdown = render_markdown(read_workbook(HEB))
    assert "| 850,000 | 3.2% |" in markdown              # not 0.032


# -- converter ---------------------------------------------------------------


def test_converter_produces_markdown_end_to_end():
    markdown = ExcelConverter().convert(HEB)
    assert markdown.startswith("## הלוואות")
    assert "`=SUM(C3:C7)`" in markdown


def test_converter_refuses_legacy_xls(tmp_path):
    legacy = tmp_path / "book.xls"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(ValueError, match="excel converter handles"):
        ExcelConverter().convert(legacy)


def test_converter_is_registered_for_the_capabilities_endpoint():
    from app.registry import registry

    names = {entry["name"] for entry in registry.get_capabilities()["converters"]}
    assert "excel" in names


# -- the legacy .xls route ---------------------------------------------------
#
# .xls does not route here — openpyxl cannot read the format and docling does
# not support it at all, so it is format-locked to markitdown. That route had no
# real coverage: the existing markitdown test patches the library with a
# MagicMock, so it exercises the wrapper and nothing else. These run the real
# thing against a real Excel 97-2003 workbook.


def test_legacy_xls_actually_converts_through_markitdown():
    from app.converters.markitdown import MarkItDownConverter

    markdown = MarkItDownConverter().convert(LEGACY_XLS)
    assert "## Sheet1" in markdown
    assert "0.980269" in markdown                        # a real cell value


def test_legacy_xls_is_values_only():
    """Documents what the .xls route cannot do — xlrd exposes no formulas."""
    import xlrd

    book = xlrd.open_workbook(LEGACY_XLS)
    assert book.biff_version == 80                       # genuine Excel 97-2003
    assert not hasattr(book.sheet_by_index(0), "formula")


def test_excel_converter_rejects_the_legacy_format_it_cannot_read():
    with pytest.raises(ValueError, match="excel converter handles"):
        ExcelConverter().convert(LEGACY_XLS)


# -- the debug/health check --------------------------------------------------


def test_health_check_routes_each_format_to_its_real_converter():
    """The check is worthless if it exercises a pipeline ingest never uses."""
    from app.format_routes import converter_for

    assert converter_for("report.pdf") == "docling"
    assert converter_for("book.xlsx") == "excel"
    assert converter_for("macros.xlsm") == "excel"
    assert converter_for("BOOK.XLSX") == "excel"          # case-insensitive
    assert converter_for("legacy.xls") == "markitdown"    # docling cannot read it
    assert converter_for("scan.png") == "docling"         # unchanged default


def test_markitdown_baseline_keeps_the_numbers_but_loses_the_formulas():
    """Documents the gap this converter exists to close."""
    from markitdown import MarkItDown

    baseline = MarkItDown().convert(str(HEB)).text_content
    assert "2736000" in baseline.replace(",", "")        # values survive
    assert "SUM" not in baseline                         # relationships do not
    assert "SUM(C3:C7)" in ExcelConverter().convert(HEB)
