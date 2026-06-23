"""PyMuPDF page pre-filter — stage 1 of the hybrid pipeline.

Idea (from the Finance-Bill workflow): a long PDF is mostly noise; only a
fraction of pages carry the content you actually want to index. Use cheap
PyMuPDF structural analysis to KEEP only those pages, then send the small
filtered PDF to the expensive engine (VLM / docling). Saves conversion +
embedding cost and removes noise chunks.

PyMuPDF reliably catches *structural* noise (blank, sparse, boilerplate-only,
duplicate pages). "Relevant" in the semantic sense (e.g. 'pages with
amendments') needs a predicate on top — here exposed as min_heb / keywords so
the rule is explicit and per-corpus, not a hidden heuristic.

Usage:
  python prefilter.py <pdf> [--min-chars 300] [--min-heb 0.5] [--keywords a,b]
                            [--write filtered.pdf]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import fitz


@dataclass
class PageFeatures:
    page: int
    chars: int
    words: int
    heb_ratio: float
    images: int
    tables: int


def analyze(pdf_path: Path) -> list[PageFeatures]:
    out: list[PageFeatures] = []
    with fitz.open(str(pdf_path)) as doc:
        for i, pg in enumerate(doc):
            text = pg.get_text()
            letters = [c for c in text if c.isalpha()]
            heb = (sum(0x590 <= ord(c) <= 0x5FF for c in letters) / len(letters)
                   if letters else 0.0)
            try:
                ntab = len(pg.find_tables().tables)
            except Exception:
                ntab = 0
            out.append(PageFeatures(
                page=i, chars=len(text.strip()), words=len(text.split()),
                heb_ratio=heb, images=len(pg.get_images()), tables=ntab,
            ))
    return out


def select(
    feats: list[PageFeatures],
    min_chars: int = 0,
    min_heb: float = 0.0,
    keywords: list[str] | None = None,
    pdf_path: Path | None = None,
) -> list[int]:
    """Return the 0-indexed pages to KEEP.

    A page is kept if it clears the structural bar (min_chars) AND the content
    bar (min_heb), OR if it contains any of `keywords` (case-sensitive substring;
    requires pdf_path to re-read text). Keyword match overrides the bars so a
    relevant sparse page is never dropped.
    """
    kw_pages: set[int] = set()
    if keywords and pdf_path is not None:
        with fitz.open(str(pdf_path)) as doc:
            for i, pg in enumerate(doc):
                t = pg.get_text()
                if any(k in t for k in keywords):
                    kw_pages.add(i)

    keep = []
    for f in feats:
        if f.page in kw_pages:
            keep.append(f.page)
        elif f.chars >= min_chars and f.heb_ratio >= min_heb:
            keep.append(f.page)
    return keep


def write_subset(pdf_path: Path, pages: list[int], out: Path) -> Path:
    src = fitz.open(str(pdf_path))
    dst = fitz.open()
    for p in pages:
        dst.insert_pdf(src, from_page=p, to_page=p)
    dst.save(str(out))
    dst.close(); src.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--min-chars", type=int, default=300)
    ap.add_argument("--min-heb", type=float, default=0.0)
    ap.add_argument("--keywords", default="")
    ap.add_argument("--write", default="")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    feats = analyze(pdf)
    kws = [k for k in args.keywords.split(",") if k]
    keep = select(feats, args.min_chars, args.min_heb, kws, pdf)

    total = len(feats)
    kept = len(keep)
    print(f"{pdf.name}: {total} pages -> keep {kept} "
          f"({kept/total*100:.0f}%), drop {total-kept} ({(total-kept)/total*100:.0f}%)")
    print(f"filter: min_chars={args.min_chars} min_heb={args.min_heb} "
          f"keywords={kws or '-'}")
    print(f"estimated cost reduction (VLM/embeddings): {(total-kept)/total*100:.0f}%")
    # show kept page ranges compactly
    if keep:
        ranges, start, prev = [], keep[0], keep[0]
        for p in keep[1:]:
            if p == prev + 1:
                prev = p
            else:
                ranges.append((start, prev)); start = prev = p
        ranges.append((start, prev))
        pretty = ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in ranges)
        print(f"kept pages: {pretty}")

    if args.write and keep:
        out = write_subset(pdf, keep, Path(args.write))
        print(f"wrote filtered PDF -> {out} ({kept} pages)")


if __name__ == "__main__":
    main()
