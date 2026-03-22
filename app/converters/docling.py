"""
PDF-to-Markdown converter using Docling.
"""

from pathlib import Path
from app.registry import register_converter
from .base import PDFConverter

@register_converter(
    name="docling",
    label="Docling",
    description="Advanced document understanding. Best for complex documents with tables, figures.",
)
class DoclingConverter(PDFConverter):
    """Converter using IBM's Docling library.

    Best suited for complex documents with tables, figures, and sophisticated layout.
    Slower but highest quality conversion.

    Install:
        pip install docling
    """

    def convert(self, pdf_path: Path) -> str:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.datamodel.base_models import InputFormat
        
        self.validate_path(pdf_path)
        
        # Configure PDF pipeline (OCR disabled by default for speed)
        pdf_options = PdfPipelineOptions()
        pdf_options.do_ocr = False
        
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)
            }
        )
        
        result = converter.convert(str(pdf_path))
        return result.document.export_to_markdown()
