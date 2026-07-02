"""Head-to-head PDF->Markdown benchmark on a Hebrew document.

Compares text-extraction engines on speed and Hebrew-text fidelity:
  - pymupdf4llm   (already in the service as `pymupdf`)
  - kreuzberg     (new candidate; PDFium + Tesseract, Rust core)
  - liteparse     (new candidate; PDFium + Tesseract, Rust core)
  - docling       (current default — the slow one we want to replace)

VLM (Vertex/Gemini) is benchmarked separately in bench_vlm.py because it
needs network + credentials and is priced per page.

Usage:
  python bench_headtohead.py <pdf> [--pages N] [--full]

  --pages N : page count for the apples-to-apples subset run (default 10)
  --full    : also run the FAST local engines on the entire document
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF


def hebrew_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    heb = sum(0x590 <= ord(c) <= 0x5FF for c in letters)
    return heb / len(letters)


def make_subset(src: Path, n: int, out: Path) -> Path:
    """Write the first n pages of src to out and return out."""
    doc = fitz.open(str(src))
    n = min(n, doc.page_count)
    sub = fitz.open()
    sub.insert_pdf(doc, from_page=0, to_page=n - 1)
    sub.save(str(out))
    sub.close()
    doc.close()
    return out


# --- engine adapters: each returns markdown given a pdf path ----------------

def run_pymupdf(path: Path) -> str:
    import pymupdf4llm
    return pymupdf4llm.to_markdown(str(path))


def run_kreuzberg(path: Path) -> str:
    import kreuzberg
    cfg = kreuzberg.ExtractionConfig(output_format=kreuzberg.OutputFormat.MARKDOWN)
    return kreuzberg.extract_file_sync(str(path), config=cfg).content


def run_liteparse(path: Path) -> str:
    import liteparse
    lp = liteparse.LiteParse(output_format="markdown", ocr_language="heb", quiet=True)
    return lp.parse(str(path)).text


def run_docling(path: Path) -> str:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.base_models import InputFormat
    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.do_table_structure = True
    conv = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    return conv.convert(str(path)).document.export_to_markdown()


ENGINES = {
    "pymupdf4llm": run_pymupdf,
    "kreuzberg": run_kreuzberg,
    "liteparse": run_liteparse,
    "docling": run_docling,
}
FAST = ["pymupdf4llm", "kreuzberg", "liteparse"]


def bench(label: str, fn, path: Path, pages: int, outfile: Path) -> dict:
    t = time.perf_counter()
    try:
        md = fn(path)
        dt = time.perf_counter() - t
    except Exception as e:  # noqa: BLE001 — benchmark: report, don't crash the suite
        dt = time.perf_counter() - t
        print(f"  {label:14s} FAILED after {dt:6.2f}s: {type(e).__name__}: {e}")
        return {"engine": label, "ok": False, "secs": dt, "error": str(e)}
    outfile.write_text(md, encoding="utf-8")
    row = {
        "engine": label,
        "ok": True,
        "secs": dt,
        "secs_per_page": dt / pages,
        "chars": len(md),
        "heb_ratio": hebrew_ratio(md),
    }
    print(f"  {label:14s} {dt:7.2f}s  ({dt/pages:6.3f}s/pg)  "
          f"{len(md):>8d} chars  heb={row['heb_ratio']:.2f}")
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", type=int, default=10)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    src = Path(args.pdf)
    if not src.exists():
        raise SystemExit(f"not found: {src}")

    outdir = Path("bench_out")
    outdir.mkdir(exist_ok=True)

    total_pages = fitz.open(str(src)).page_count
    sub = make_subset(src, args.pages, outdir / f"_subset_{args.pages}p.pdf")
    n = min(args.pages, total_pages)

    print(f"\nDocument: {src.name}  ({total_pages} pages, "
          f"text-layer Hebrew)\n")
    print(f"=== SUBSET ({n} pages) — all engines head-to-head ===")
    for name in ENGINES:
        bench(name, ENGINES[name], sub, n, outdir / f"sub_{name}.md")

    if args.full:
        print(f"\n=== FULL DOC ({total_pages} pages) — fast local engines ===")
        for name in FAST:
            bench(name, ENGINES[name], src, total_pages, outdir / f"full_{name}.md")

    print("\nDone. Markdown outputs in ./bench_out/  (inspect for Hebrew quality)")


if __name__ == "__main__":
    main()
