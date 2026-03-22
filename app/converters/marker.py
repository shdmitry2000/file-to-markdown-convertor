"""
PDF-to-Markdown converter backed by Marker.
"""

from pathlib import Path
from app.registry import register_converter
from .base import PDFConverter

@register_converter(
    name="marker",
    label="Marker",
    description="High-quality PDF conversion with advanced layout detection. OCR disabled - extracts text only. Use VLM for scanned/image content.",
)
class MarkerConverter(PDFConverter):
    """High-quality converter using Marker library.

    Marker is designed for high-quality PDF to markdown conversion with:
    - Advanced layout detection and analysis
    - Excellent table extraction
    - Mathematical equation support (LaTeX)
    - Figure and image handling
    - Multi-column layout support
    
    **OCR Policy: DISABLED**
    - Only extracts existing selectable text from PDFs
    - Does NOT perform OCR on scanned pages or images
    - Use VLM converter for scanned PDFs or image-based content
    
    Best suited for:
    - Digital PDFs with selectable text
    - Academic papers and research documents
    - Technical documents with complex layouts
    - Documents with tables, equations, and figures
    
    Note: Marker requires more resources and time than simpler converters,
    but produces superior output quality for complex documents.

    Install:
        pip install marker-pdf
    """

    def __init__(self) -> None:
        """Initialize Marker converter with lazy imports."""
        # Lazy import to avoid loading heavy dependencies unless needed
        pass

    def convert(self, pdf_path: Path) -> str:
        """Convert PDF to Markdown using Marker.
        
        Args:
            pdf_path: Path to the PDF file to convert
            
        Returns:
            Markdown string of the converted document
            
        Raises:
            FileNotFoundError: If PDF file doesn't exist
            ImportError: If marker-pdf is not installed
        """
        self.validate_path(pdf_path)
        
        try:
            from marker.converters.pdf import PdfConverter
            from marker.models import create_model_dict
        except ImportError as e:
            raise ImportError(
                "Marker library not installed. Install with: pip install marker-pdf"
            ) from e
        
        # Create converter with OCR disabled
        converter = PdfConverter(
            artifact_dict=create_model_dict(),
            config={
                "use_ocr": False,  # Disable OCR - text extraction only
            }
        )
        
        # Convert the PDF
        rendered = converter(str(pdf_path))
        
        return rendered.markdown
