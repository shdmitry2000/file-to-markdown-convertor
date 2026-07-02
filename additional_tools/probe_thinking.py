"""Measure VLM per-page latency with thinking DISABLED vs ON, on 368_8.pdf.
Proves the reasoning_effort='disable' default actually cuts the ~55s/page."""
import os, time
os.environ["VLM_MODEL"] = os.getenv("VLM_MODEL", "vertex_ai/gemini-3.5-flash")
from pathlib import Path
import fitz
from app.converters.vlm import VLMConverter
from app.converters.dbank import _heb_ratio

SRC = "/Users/dmitrysh/code/rag/rag_eval_bank/files_to_check/368_8.pdf"
PAGES = [10, 40, 80]  # spread: Hebrew-region + English API-table appendix
print("VLM_MODEL =", os.environ["VLM_MODEL"])


def render(page):
    return VLMConverter._render_page_as_b64(page)


def main():
    doc = fitz.open(SRC)
    imgs = [(i, render(doc[i])) for i in PAGES]

    variants = {
        "thinking ON (baseline)": VLMConverter(backend="factory", reasoning_effort=""),
        "thinking DISABLED": VLMConverter(backend="factory", reasoning_effort="disable"),
    }
    for name, conv in variants.items():
        print(f"\n=== {name}  (temp={conv._temperature}, effort={conv._reasoning_effort!r}) ===")
        for i, b in imgs:
            t0 = time.time()
            out = conv.transcribe_page(b)
            dt = time.time() - t0
            print(f"  page {i:>3}: {dt:6.1f}s  {len(out):>5} chars  heb={_heb_ratio(out):.2f}")


if __name__ == "__main__":
    main()
