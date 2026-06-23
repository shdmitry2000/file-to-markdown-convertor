"""Hebrew text-fidelity check: kreuzberg vs VLM (gemini-2.5-pro).

Gate before wiring kreuzberg into the dbank path. The VLM reads Hebrew reliably,
so we use it as the reference and ask: does kreuzberg capture the SAME Hebrew
text? We compare the multiset of Hebrew words (content fidelity, ignoring markdown
/ whitespace / order) plus a sequence-similarity ratio (reading order), and print
the actual word-level diffs so real text loss is distinguishable from formatting.

Usage (source ../.env first for the VLM lane):
  set -a; source ../.env; set +a
  python compare_text_fidelity.py <pdf> --pages 0,5,10,15,20
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import fitz

HEB = re.compile(r"[֐-׿]+")  # Hebrew block incl. geresh/gershayim/maqaf


def heb_words(md: str) -> list[str]:
    """Hebrew word tokens (markdown noise is irrelevant — we only keep Hebrew runs)."""
    return [t for t in HEB.findall(md) if len(t) >= 2]


def one_page(src: Path, p: int) -> str:
    d = fitz.open(str(src)); s = fitz.open()
    s.insert_pdf(d, from_page=p, to_page=p)
    fn = f"/tmp/_fid{p}.pdf"; s.save(fn); s.close(); d.close()
    return fn


def run_kreuzberg(path: str) -> str:
    import kreuzberg
    cfg = kreuzberg.ExtractionConfig(output_format=kreuzberg.OutputFormat.MARKDOWN)
    return kreuzberg.extract_file_sync(path, config=cfg).content


def run_vlm(path: str) -> str:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from app.converters.vlm import VLMConverter
    return VLMConverter(backend="factory").convert(Path(path))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", default="0,5,10,15,20")
    args = ap.parse_args()
    src = Path(args.pdf)
    pages = [int(x) for x in args.pages.split(",") if x.strip()]

    out = Path("bench_out"); out.mkdir(exist_ok=True)
    report = [f"# Hebrew text fidelity — kreuzberg vs VLM\n\n{src.name}, pages {pages}\n"]
    print(f"{'page':>5} {'k_words':>8} {'v_words':>8} {'multiset_overlap':>16} {'seq_ratio':>10}")
    for p in pages:
        fn = one_page(src, p)
        k_md, v_md = run_kreuzberg(fn), run_vlm(fn)
        (out / f"fid_p{p}_kreuzberg.md").write_text(k_md, encoding="utf-8")
        (out / f"fid_p{p}_vlm.md").write_text(v_md, encoding="utf-8")

        kw, vw = heb_words(k_md), heb_words(v_md)
        ck, cv = Counter(kw), Counter(vw)
        inter = sum((ck & cv).values())
        union = sum((ck | cv).values())
        overlap = inter / union if union else 1.0
        ratio = SequenceMatcher(None, kw, vw).ratio()  # order-sensitive
        print(f"{p:>5} {len(kw):>8} {len(vw):>8} {overlap:>15.1%} {ratio:>10.2f}")

        only_k = list((ck - cv).elements())
        only_v = list((cv - ck).elements())
        report.append(f"\n## page {p}  (overlap {overlap:.1%}, seq {ratio:.2f}, "
                      f"kreuzberg={len(kw)} words, vlm={len(vw)} words)\n")
        report.append(f"**in VLM but NOT kreuzberg ({len(only_v)}):** {' '.join(only_v[:40])}")
        report.append(f"\n**in kreuzberg but NOT VLM ({len(only_k)}):** {' '.join(only_k[:40])}")

    (out / "FIDELITY.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\nword-level diffs + full outputs -> bench_out/FIDELITY.md and fid_p*_*.md")


if __name__ == "__main__":
    main()
