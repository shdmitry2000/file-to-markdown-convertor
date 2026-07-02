"""Test docling's HybridChunker on a table-heavy slice of the Hebrew finance doc.

Evaluates chunk STRUCTURE (do tables stay atomic? is heading context attached?
what sizes?) — not content fidelity (docling reverses Hebrew in table cells).
Runs on MPS (no CPU pinning) for dev speed.
"""
import os, tempfile
import fitz

SRC = "/Users/dmitrysh/code/rag/rag_eval_bank/files_to_check/duah_hapoalim.pdf"
FROM, TO = 98, 103  # a stretch with real financial tables + surrounding prose


def build_subset():
    doc = fitz.open(SRC)
    sub = fitz.open()
    sub.insert_pdf(doc, from_page=FROM, to_page=TO)
    fd, name = tempfile.mkstemp(suffix="_slice.pdf")
    os.close(fd)
    sub.save(name)
    return name


def main():
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.base_models import InputFormat
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker

    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.do_table_structure = True  # need real DoclingDocument tables to chunk
    conv = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    pdf = build_subset()
    dl = conv.convert(pdf).document
    print(f"docling doc: {len(getattr(dl,'tables',[]) or [])} tables, "
          f"pages {FROM}-{TO}")

    chunker = HybridChunker()  # default tokenizer (all-MiniLM-L6-v2)
    chunks = list(chunker.chunk(dl))
    print(f"HybridChunker -> {len(chunks)} chunks\n")

    for i, c in enumerate(chunks):
        ctx = chunker.contextualize(chunk=c)
        labels = [getattr(it, "label", "?") for it in (c.meta.doc_items or [])]
        headings = getattr(c.meta, "headings", None) or []
        is_table = any(str(l) == "table" or "table" in str(l).lower() for l in labels)
        preview = ctx.replace("\n", " ")[:160]
        print(f"[{i:>2}] chars={len(ctx):>5} table={is_table} "
              f"items={labels} headings={headings}")
        print(f"     {preview}")
    os.unlink(pdf)


if __name__ == "__main__":
    main()
