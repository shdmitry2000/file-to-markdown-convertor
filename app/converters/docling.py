"""
PDF-to-Markdown converter using Docling.
"""

from pathlib import Path
from app.registry import register_converter
from .base import PDFConverter

@register_converter(
    name="docling",
    label="Docling",
    description="Advanced document understanding with text extraction only. OCR disabled - use VLM for scanned PDFs or images.",
)
class DoclingConverter(PDFConverter):
    """Converter using IBM's Docling library.

    **OCR Policy: DISABLED**
    - Only extracts existing selectable text from PDFs
    - Does NOT perform OCR on scanned pages or images
    - Use VLM converter for scanned PDFs or image-based content

    Best suited for:
    - Digital PDFs with complex layouts
    - Documents with tables and figures (text-based)
    - Sophisticated document structure analysis
    
    Note: Slower but highest quality conversion for digital PDFs.

    Install:
        pip install docling
    """

    def convert(self, pdf_path: Path) -> str:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.datamodel.base_models import InputFormat
        
        self.validate_path(pdf_path)
        
        # Configure PDF pipeline with OCR explicitly disabled
        # We use VLM for image/picture conversion instead
        pdf_options = PdfPipelineOptions()
        pdf_options.do_ocr = False  # No OCR - text extraction only
        
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)
            }
        )
        
        result = converter.convert(str(pdf_path))
        return result.document.export_to_markdown()
