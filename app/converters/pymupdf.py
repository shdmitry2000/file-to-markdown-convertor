"""
PDF-to-Markdown converter backed by pymupdf4llm.
"""

from pathlib import Path

import pymupdf4llm
from app.registry import register_converter
from .base import PDFConverter

@register_converter(
    name="pymupdf",
    label="PyMuPDF",
    description="Fast, lightweight. Best for standard digital PDFs with selectable text.",
)
class PyMuPDFConverter(PDFConverter):
    """Fast, lightweight converter using pymupdf4llm.

    Best suited for standard digital PDFs with selectable text.
    Produces clean Markdown with good table support.

    Install:
        pip install pymupdf4llm
    """

    def convert(self, pdf_path: Path) -> str:
        self.validate_path(pdf_path)
        # PyMuPDF 2.0 OCRs pages it judges scanned, with tesseract and English by
        # default: the image ships no tesseract and the documents are Hebrew. The
        # text layer is what this converter reads; scanned pages go to dbank / VLM.
        return pymupdf4llm.to_markdown(str(pdf_path), use_ocr=False)
