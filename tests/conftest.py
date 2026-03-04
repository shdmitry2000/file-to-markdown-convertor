"""Shared test fixtures and configuration."""

import pytest
import tempfile
import shutil
from pathlib import Path


@pytest.fixture(scope="session")
def test_data_dir():
    """Create a temporary directory for test data."""
    temp_dir = tempfile.mkdtemp(prefix="markdown_test_")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def files_to_convert_dir(test_data_dir):
    """Create files_to_convert directory."""
    dir_path = test_data_dir / "files_to_convert"
    dir_path.mkdir(exist_ok=True)
    return dir_path


@pytest.fixture
def converted_files_dir(test_data_dir):
    """Create converted_files directory."""
    dir_path = test_data_dir / "converted_files"
    dir_path.mkdir(exist_ok=True)
    return dir_path


@pytest.fixture
def sample_pdf(files_to_convert_dir):
    """Create a minimal valid PDF file for testing."""
    pdf_path = files_to_convert_dir / "test_document.pdf"
    
    # Minimal PDF header and content
    pdf_content = b"""%PDF-1.4
1 0 obj
<<
/Type /Catalog
/Pages 2 0 R
>>
endobj
2 0 obj
<<
/Type /Pages
/Kids [3 0 R]
/Count 1
>>
endobj
3 0 obj
<<
/Type /Page
/Parent 2 0 R
/Resources <<
/Font <<
/F1 <<
/Type /Font
/Subtype /Type1
/BaseFont /Helvetica
>>
>>
>>
/MediaBox [0 0 612 792]
/Contents 4 0 R
>>
endobj
4 0 obj
<<
/Length 44
>>
stream
BT
/F1 12 Tf
100 700 Td
(Test Document) Tj
ET
endstream
endobj
xref
0 5
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000317 00000 n 
trailer
<<
/Size 5
/Root 1 0 R
>>
startxref
409
%%EOF
"""
    pdf_path.write_bytes(pdf_content)
    return pdf_path


@pytest.fixture
def sample_text_file(files_to_convert_dir):
    """Create a text file for testing."""
    txt_path = files_to_convert_dir / "test_document.txt"
    txt_path.write_text("# Test Document\n\nThis is a test file.")
    return txt_path
