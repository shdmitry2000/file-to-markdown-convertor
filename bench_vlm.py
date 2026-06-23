"""VLM (Vertex/Gemini) lane of the Hebrew head-to-head benchmark.

Reuses the service's app.converters.vlm.VLMConverter via its `factory`
backend, which routes the vision call through shared.llm_factory ->
litellm -> Vertex (the same provider the rest of the platform uses).

Run with the repo-root .env sourced so LLM_FACTORY_* + ADC are present:

  set -a; source ../.env; set +a
  python bench_vlm.py <pdf> [--pages 3]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import fitz

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))            # for `shared`
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `app`


def hebrew_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(0x590 <= ord(c) <= 0x5FF for c in letters) / len(letters)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", type=int, default=3)
    args = ap.parse_args()

    src = Path(args.pdf)
    outdir = Path("bench_out")
    outdir.mkdir(exist_ok=True)

    # small subset — Vertex vision is priced per page
    doc = fitz.open(str(src))
    n = min(args.pages, doc.page_count)
    sub_path = outdir / f"_vlm_subset_{n}p.pdf"
    sub = fitz.open()
    sub.insert_pdf(doc, from_page=0, to_page=n - 1)
    sub.save(str(sub_path))
    sub.close(); doc.close()

    from app.converters.vlm import VLMConverter

    conv = VLMConverter(backend="factory")
    print(f"VLM model: {conv._model}  (backend=factory)")

    t = time.perf_counter()
    md = conv.convert(sub_path)
    dt = time.perf_counter() - t

    (outdir / f"sub_vlm.md").write_text(md, encoding="utf-8")
    print(f"  vlm(vertex)    {dt:7.2f}s  ({dt/n:6.3f}s/pg)  "
          f"{len(md):>8d} chars  heb={hebrew_ratio(md):.2f}")
    print("  saved -> bench_out/sub_vlm.md")


if __name__ == "__main__":
    main()
