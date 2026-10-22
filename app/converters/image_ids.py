"""Give every image placeholder in docling's markdown an identity.

docling writes one ``<!-- image -->`` per picture and drops what it knows about
it. This replaces each placeholder, in reading order, with

    <!-- image page=3 n=2 id=p3-i2 bbox=0.12,0.40,0.88,0.71 -->

page  the original page (1-based); a caller that converts one page at a time
      (dbank) passes the real page, since docling sees page 1 there.
n     the picture's position among the pictures on that page (1-based).
id    ``p<page>-i<n>``: unique within the file, so ``<file>#<id>`` names one image
      anywhere in the space.
bbox  left, top, right, bottom as fractions of the page (top-left origin), when
      docling reports a box.

Placeholders and pictures are matched by order, which is how docling's exporter
emits them. If the counts disagree the markdown is returned unchanged: an
unidentified image is better than a wrongly identified one.
"""

from __future__ import annotations

import re
from typing import Any, Optional

PLACEHOLDER = "<!-- image -->"
IMAGE_TAG = re.compile(r"<!--\s*image\b([^>]*)-->")


def _pictures(doc: Any) -> list[Any]:
    try:
        from docling_core.types.doc import PictureItem
    except Exception:  # pragma: no cover - docling always present in the service
        return []
    return [item for item, _level in doc.iterate_items() if isinstance(item, PictureItem)]


def _bbox(prov: Any, doc: Any) -> Optional[str]:
    box = getattr(prov, "bbox", None)
    page = getattr(doc, "pages", {}).get(getattr(prov, "page_no", None)) if hasattr(doc, "pages") else None
    size = getattr(page, "size", None)
    if box is None or size is None or not getattr(size, "width", 0) or not getattr(size, "height", 0):
        return None
    try:
        tl = box.to_top_left_origin(page_height=size.height)
        vals = (tl.l / size.width, tl.t / size.height, tl.r / size.width, tl.b / size.height)
    except Exception:
        return None
    return ",".join(f"{max(0.0, min(1.0, v)):.2f}" for v in vals)


def annotate_images(markdown: str, doc: Any, page: Optional[int] = None) -> str:
    """Replace each ``<!-- image -->`` with an identified tag (see module doc)."""
    if PLACEHOLDER not in markdown:
        return markdown
    pictures = _pictures(doc)
    if len(pictures) != markdown.count(PLACEHOLDER):
        return markdown
    per_page: dict[int, int] = {}
    tags = []
    for pic in pictures:
        prov = (getattr(pic, "prov", None) or [None])[0]
        p = page if page is not None else (getattr(prov, "page_no", None) or 0)
        per_page[p] = per_page.get(p, 0) + 1
        n = per_page[p]
        parts = [f"page={p}", f"n={n}", f"id=p{p}-i{n}"]
        box = _bbox(prov, doc) if prov is not None else None
        if box:
            parts.append(f"bbox={box}")
        tags.append(f"<!-- image {' '.join(parts)} -->")
    out, rest = [], markdown
    for tag in tags:
        head, _, rest = rest.partition(PLACEHOLDER)
        out.append(head)
        out.append(tag)
    out.append(rest)
    return "".join(out)


def parse_image_tag(attrs: str) -> dict[str, str]:
    """``page=3 n=2 id=p3-i2`` -> {"page": "3", "n": "2", "id": "p3-i2"}."""
    return dict(m.groups() for m in re.finditer(r"([a-z]+)=(\S+)", attrs))
