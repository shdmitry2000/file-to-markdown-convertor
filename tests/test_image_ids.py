"""Image placeholders carry their page, position and id (app/converters/image_ids.py)."""

from docling_core.types.doc import BoundingBox, CoordOrigin, DoclingDocument, ProvenanceItem, Size

from app.converters.image_ids import IMAGE_TAG, annotate_images, parse_image_tag


def _doc(pages: list[int]) -> DoclingDocument:
    doc = DoclingDocument(name="t")
    for p in sorted(set(pages)):
        doc.add_page(page_no=p, size=Size(width=100, height=200))
    for k, p in enumerate(pages):
        doc.add_text(label="text", text=f"paragraph {k}")
        doc.add_picture(prov=ProvenanceItem(
            page_no=p,
            bbox=BoundingBox(l=10, t=190, r=90, b=110, coord_origin=CoordOrigin.BOTTOMLEFT),
            charspan=(0, 0),
        ))
    return doc


def test_each_placeholder_gets_page_position_id_and_box():
    doc = _doc([1, 1, 2])
    md = annotate_images(doc.export_to_markdown(), doc)
    tags = [parse_image_tag(m.group(1)) for m in IMAGE_TAG.finditer(md)]
    assert [t["id"] for t in tags] == ["p1-i1", "p1-i2", "p2-i1"]
    assert tags[0]["page"] == "1" and tags[0]["n"] == "1"
    assert tags[0]["bbox"] == "0.10,0.05,0.90,0.45", "top-left origin fractions"
    assert "<!-- image -->" not in md


def test_a_single_page_conversion_uses_the_callers_page():
    doc = _doc([1])  # dbank converts one page at a time: docling sees page 1
    md = annotate_images(doc.export_to_markdown(), doc, page=23)
    assert "id=p23-i1" in md and "page=23" in md


def test_counts_that_disagree_leave_the_markdown_alone():
    doc = _doc([1, 2])
    md = doc.export_to_markdown() + "\n\n<!-- image -->\n"
    assert annotate_images(md, doc) == md


def test_markdown_without_images_is_untouched():
    doc = DoclingDocument(name="t")
    assert annotate_images("# title\n\ntext", doc) == "# title\n\ntext"
