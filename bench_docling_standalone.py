"""Standalone docling benchmark — mirrors app/workers/worker.py:_warm_docling_loop
EXACTLY (DocumentConverter built once, do_ocr=False, do_table_structure=True),
then converts N PDFs sequentially in ONE process.

Purpose: isolate pure docling cost from our service plumbing (ZeroMQ, per-job
subprocess spawn, model reload). If file #2+ here are ~1s but the service is
~20s/file, the warm worker isn't staying warm (a service bug). If file #2+ here
are also ~20s, docling itself is the cost (no service bug).

Run:  /opt/homebrew/anaconda3/bin/python bench_docling_standalone.py <pdf1> <pdf2> ...
"""
import sys
import time

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.datamodel.base_models import InputFormat


def main(pdfs):
    t0 = time.perf_counter()
    pdf_options = PdfPipelineOptions()
    pdf_options.do_ocr = False
    pdf_options.do_table_structure = True
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)}
    )
    load_s = time.perf_counter() - t0
    print(f"[model load once] {load_s:.2f}s")

    for i, pdf in enumerate(pdfs, 1):
        t = time.perf_counter()
        result = converter.convert(pdf)
        md = result.document.export_to_markdown()
        dt = time.perf_counter() - t
        print(f"[convert {i}] {pdf.split('/')[-1]}: {dt:.2f}s  ({len(md)} chars, "
              f"{result.document.num_pages()} pages)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: bench_docling_standalone.py <pdf> [<pdf> ...]")
    main(sys.argv[1:])
