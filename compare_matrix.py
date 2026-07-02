"""Head-to-head comparison MATRIX for PDF->markdown engines.

Runs every engine on the SAME two pages — one TEXT page and one TABLE page —
and emits:
  1. a metrics matrix (engine x metrics) for text and for tables, and
  2. a side-by-side report (text-vs-text, tables-vs-tables) so the actual
     Hebrew output can be eyeballed.

docling is listed first as the baseline. VLM (Vertex) is included unless --no-vlm.

Usage (source ../.env first so the VLM/Vertex lane works):
  set -a; source ../.env; set +a
  python compare_matrix.py <pdf> --text-page 0 --table-page 50 [--no-vlm]
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import fitz


def heb_ratio(s: str) -> float:
    L = [c for c in s if c.isalpha()]
    return sum(0x590 <= ord(c) <= 0x5FF for c in L) / len(L) if L else 0.0


def table_lines(md: str) -> list[str]:
    return [ln for ln in md.splitlines() if ln.count("|") >= 2]


def one_page_pdf(src: Path, page: int, out: Path) -> Path:
    d = fitz.open(str(src)); s = fitz.open()
    s.insert_pdf(d, from_page=page, to_page=page)
    s.save(str(out)); s.close(); d.close()
    return out


# --- engine adapters -------------------------------------------------------

def run_docling(p: Path) -> str:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.base_models import InputFormat
    o = PdfPipelineOptions(); o.do_ocr = False; o.do_table_structure = True
    c = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=o)})
    return c.convert(str(p)).document.export_to_markdown()


def run_pymupdf(p: Path) -> str:
    import pymupdf4llm
    return pymupdf4llm.to_markdown(str(p))


def run_kreuzberg_text(p: Path) -> str:
    import kreuzberg
    cfg = kreuzberg.ExtractionConfig(output_format=kreuzberg.OutputFormat.MARKDOWN)
    return kreuzberg.extract_file_sync(str(p), config=cfg).content


def run_kreuzberg_tables(p: Path) -> str:
    import kreuzberg
    cfg = kreuzberg.ExtractionConfig(
        output_format=kreuzberg.OutputFormat.MARKDOWN,
        layout=kreuzberg.LayoutDetectionConfig(table_model="tatr", apply_heuristics=True),
    )
    return kreuzberg.extract_file_sync(str(p), config=cfg).content


def run_liteparse(p: Path) -> str:
    import liteparse
    return liteparse.LiteParse(output_format="markdown", ocr_language="heb", quiet=True).parse(str(p)).text


def run_vlm(p: Path) -> str:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # shared
    sys.path.insert(0, str(Path(__file__).resolve().parent))      # app
    from app.converters.vlm import VLMConverter
    return VLMConverter(backend="factory").convert(p)


# engine name -> (text adapter, table adapter). docling FIRST.
ENGINES = {
    "docling":     (run_docling, run_docling),
    "pymupdf4llm": (run_pymupdf, run_pymupdf),
    "kreuzberg":   (run_kreuzberg_text, run_kreuzberg_tables),  # tatr for tables
    "liteparse":   (run_liteparse, run_liteparse),
    "vlm(vertex)": (run_vlm, run_vlm),
}


def measure(fn, p: Path) -> tuple[str, float]:
    t = time.perf_counter()
    try:
        md = fn(p)
    except Exception as e:  # report, keep the matrix going
        return f"__ERROR__ {type(e).__name__}: {e}", time.perf_counter() - t
    return md, time.perf_counter() - t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--text-page", type=int, default=0)
    ap.add_argument("--table-page", type=int, default=50)
    ap.add_argument("--no-vlm", action="store_true")
    args = ap.parse_args()

    src = Path(args.pdf)
    out = Path("bench_out"); out.mkdir(exist_ok=True)
    tp = one_page_pdf(src, args.text_page, out / "_cmp_text.pdf")
    bp = one_page_pdf(src, args.table_page, out / "_cmp_table.pdf")

    names = [n for n in ENGINES if not (args.no_vlm and n.startswith("vlm"))]

    text_res, table_res = {}, {}
    for n in names:
        text_fn, tbl_fn = ENGINES[n]
        text_res[n] = measure(text_fn, tp)
        table_res[n] = measure(tbl_fn, bp)

    lines: list[str] = []
    w = lines.append
    w(f"# Comparison matrix — {src.name}")
    w(f"\nTEXT page = {args.text_page}, TABLE page = {args.table_page}\n")

    # --- metrics matrices ---
    w("## Metrics — TEXT page\n")
    w("| engine | time (s) | chars | heb_ratio |")
    w("|---|---|---|---|")
    for n in names:
        md, dt = text_res[n]
        w(f"| {n} | {dt:.2f} | {len(md)} | {heb_ratio(md):.2f} |")

    w("\n## Metrics — TABLE page\n")
    w("| engine | time (s) | chars | table_rows | heb_ratio |")
    w("|---|---|---|---|---|")
    for n in names:
        md, dt = table_res[n]
        w(f"| {n} | {dt:.2f} | {len(md)} | {len(table_lines(md))} | {heb_ratio(md):.2f} |")

    # --- side by side: text vs text ---
    w("\n## TEXT vs TEXT (first ~500 chars)\n")
    for n in names:
        md, _ = text_res[n]
        w(f"### {n}\n```\n{md[:500].strip()}\n```\n")

    # --- side by side: tables vs tables ---
    w("\n## TABLES vs TABLES (first 12 table rows, or note if flattened)\n")
    for n in names:
        md, _ = table_res[n]
        tl = table_lines(md)
        if tl:
            body = "\n".join(tl[:12])
        else:
            body = "(no markdown table — cells flattened to text)\n\n" + md[:500].strip()
        w(f"### {n}\n```\n{body}\n```\n")

    report = out / "MATRIX.md"
    report.write_text("\n".join(lines), encoding="utf-8")

    # also echo the two metrics matrices to stdout
    print("\n".join(l for l in lines if l.startswith("|") or l.startswith("## Metrics")))
    print(f"\nFull side-by-side report -> {report}")


if __name__ == "__main__":
    main()
