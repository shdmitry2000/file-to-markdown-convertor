# Excel fixture corpus

Workbooks for the `excel` converter — the path that keeps formulas and their
row/column meaning instead of flattening a sheet to bare numbers.

Rebuild them (inside the container; openpyxl ships with `markitdown[all]`):

```bash
docker compose run --rm markdown-api python tests/fixtures/excel/build_fixtures.py
```

## What each file covers

| File | Exercises |
|---|---|
| `heb_loans_rtl.xlsx` | Hebrew RTL sheet, merged title above the header row, per-row formulas, totals row, ₪/% number formats, cell comment, defined name, freeze panes, autofilter |
| `three_statement_heb.xlsx` | Three linked sheets (P&L / balance / cash flow), cross-sheet refs with quoted Hebrew sheet names, multi-year columns, indented row hierarchy |
| `multi_region.xlsx` | Three independent tables on one sheet — two side by side, one below — each with its own title row |
| `crosstab_multiheader.xlsx` | Merged two-level header (`2026` spanning `Q1..Q4`), totals on both axes |
| `edge_cases.xlsx` | Array formula, whole-column ref (`B:B`), structured table ref (`Parts[qty]`), `INDIRECT`, `#DIV/0!` error result, quoted cross-sheet ref, hidden row, and a sheet whose formulas have **no cached result** |

## Cached values

An .xlsx stores each formula twice — the formula and the result Excel computed
at its last save. openpyxl writes the formula but leaves the result empty, so a
workbook it produces reads back as `None` for every formula cell and would test
nothing. `xlsx_cache.inject_cached()` writes the results into the sheet XML so
the fixtures behave like real, Excel-saved files.

`edge_cases.xlsx!NoCache` deliberately keeps empty results — that is the path
where the converter must emit the formula without inventing a number.

For fully authentic recalculation, add `libreoffice-calc` to a dev image and run
`soffice --headless --convert-to xlsx`. That is ~400MB, so it stays out of the
runtime image and out of CI; injection is the default.

## legacy_biff8.xls

A genuine Excel 97-2003 workbook (OLE2 container, BIFF version 80) — not a
renamed .xlsx. Legacy .xls cannot be produced by openpyxl, and the writers that
could (xlwt) are gone from modern pandas, so this one is checked in rather than
generated.

It backs the `.xls` route: openpyxl cannot read the format at all and docling
does not list it among its supported formats, so `.xls` routes to markitdown
(which reads it via xlrd) and arrives values-only.

Source: `pandas/tests/io/data/excel/test1.xls` from pandas-dev/pandas,
BSD-3-Clause. Used unmodified as a test asset.

## Real-world reference files

Not committed (licensing/size). Useful public workbooks that are Excel-saved and
therefore carry real cached values:

- `https://raw.githubusercontent.com/dgorissen/pycel/master/tests/fixtures/excelcompiler.xlsx` — 8 sheets, 75 formulas, all cached (SUM/INDEX/LINEST/SUBTOTAL)
- `https://raw.githubusercontent.com/vinci1it2000/formulas/master/test/test_files/excel.xlsx` — small, 8 formulas, all cached
- `https://go.microsoft.com/fwlink/?LinkID=521962` — Microsoft "Financial Sample", 700 rows of realistic finance data, no formulas

Israeli sources (Bank of Israel, CBS) publish JS-rendered pages with no static
file links, and their published statistics are values-only — no formulas — so
they add nothing beyond RTL layout, which `heb_loans_rtl.xlsx` already covers.
