import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.converters.markitdown import MarkItDownConverter
from app.converters.pymupdf import PyMuPDFConverter
from app.converters.vlm import VLMConverter

def test_markitdown_converter_success(sample_pdf):
    with patch('markitdown.MarkItDown') as mock_mid_cls:
        mock_mid = MagicMock()
        mock_mid_cls.return_value = mock_mid
        mock_result = MagicMock()
        mock_result.text_content = "# Test Document\n\nConverted using markitdown"
        mock_mid.convert.return_value = mock_result
        
        converter = MarkItDownConverter()
        result = converter.convert(sample_pdf)
        
        assert result == "# Test Document\n\nConverted using markitdown"
        mock_mid.convert.assert_called_once_with(str(sample_pdf))

def test_pymupdf_converter_success(sample_pdf):
    with patch('pymupdf4llm.to_markdown') as mock_to_markdown:
        mock_to_markdown.return_value = "# Test Document\n\nConverted using pymupdf4llm"
        
        converter = PyMuPDFConverter()
        result = converter.convert(sample_pdf)
        
        assert result == "# Test Document\n\nConverted using pymupdf4llm"
        # 1.4.8.6: PyMuPDF 2.0 would OCR pages it judges scanned; the converter turns it off.
        mock_to_markdown.assert_called_once_with(str(sample_pdf), use_ocr=False)

def test_vlm_converter_success(sample_pdf):
    """Each page goes to the job's client (vision_sync); a markdown fence is stripped."""
    client = MagicMock()
    client.vision_sync.return_value = "```markdown\n# Test Document\n\nConverted using VLM\n```"
    with patch("app.converters.vlm_lane.client", return_value=client):
        with patch('fitz.open') as mock_fitz_open:
            mock_pdf = MagicMock()
            mock_fitz_open.return_value.__enter__.return_value = mock_pdf
            mock_pdf.page_count = 1
            mock_page = MagicMock()
            mock_pdf.__getitem__.return_value = mock_page

            mock_pixmap = MagicMock()
            mock_pixmap.tobytes.return_value = b"pngdata"
            mock_page.get_pixmap.return_value = mock_pixmap

            result = VLMConverter().convert(sample_pdf)

            assert result == "# Test Document\n\nConverted using VLM"
