"""Probe: does docling detect TABLE regions cheaply (do_table_structure=False)?

We need reliable per-page table detection to route table pages to the VLM.
Question: can the CHEAP docling pass (no TableFormer) still tell us a page has a
table region, or must we pay for do_table_structure=True?

Runs on a few known pages of duah_hapoalim.pdf and prints, per page and per
mode: #table-items detected + wall time.
"""
import sys, time, tempfile, os
from pathlib import Path
import fitz

SRC = Path("/Users/dmitrysh/code/rag/rag_eval_bank/files_to_check/duah_hapoalim.pdf")
PAGES = [11, 40, 12, 41, 5]  # mix: prose-ish + known dense financial tables

os.environ.setdefault("DOCLING_CPU_THREADS", "1")


def one_page_pdf(doc, i):
    sub = fitz.open()
    sub.insert_pdf(doc, from_page=i, to_page=i)
    fd, name = tempfile.mkstemp(suffix=f"_p{i}.pdf"); os.close(fd)
    sub.save(name); sub.close()
    return name


def build(table_structure: bool):
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions, AcceleratorOptions, AcceleratorDevice
    from docling.datamodel.base_models import InputFormat
    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.do_table_structure = table_structure
    opts.accelerator_options = AcceleratorOptions(num_threads=1, device=AcceleratorDevice.CPU)
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


def count_tables(doc_result):
    d = doc_result.document
    n = len(getattr(d, "tables", []) or [])
    return n


def main():
    doc = fitz.open(str(SRC))
    tmp = {i: one_page_pdf(doc, i) for i in PAGES}
    for mode in (False, True):
        conv = build(mode)
        # warm the models once (first call loads weights)
        _ = conv.convert(tmp[PAGES[0]])
        print(f"\n=== do_table_structure={mode} ===")
        for i in PAGES:
            t0 = time.time()
            res = conv.convert(tmp[i])
            dt = time.time() - t0
            print(f"  page {i:>3}: tables={count_tables(res)}  time={dt:.2f}s")
    for p in tmp.values():
        os.unlink(p)


if __name__ == "__main__":
    main()
