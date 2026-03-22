"""Tests for markdown-api service endpoints."""

import pytest
import json
from pathlib import Path
from fastapi.testclient import TestClient
import tempfile
import shutil


# Mock the ZMQ setup to avoid actual socket binding in tests
@pytest.fixture(autouse=True)
def mock_zmq(monkeypatch):
    """Mock ZMQ context and sockets for testing."""
    class MockSocket:
        def bind(self, address):
            pass
        
        def send_string(self, data):
            pass
        
        def recv_json(self):
            return {"conversion_id": "test-id", "status": "completed"}
        
        def close(self):
            pass
    
    class MockContext:
        def socket(self, socket_type):
            return MockSocket()
        
        def term(self):
            pass
    
    monkeypatch.setattr("zmq.Context", lambda: MockContext())


@pytest.fixture
def test_file(tmp_path):
    """Create a temporary test file."""
    test_file = tmp_path / "test_document.pdf"
    test_file.write_bytes(b"PDF content here")
    return test_file


@pytest.fixture
def client():
    """Create test client."""
    # Import here to avoid ZMQ binding on module load
    from app.api.main import app
    return TestClient(app)


def test_health_endpoint(client):
    """Test /health endpoint returns healthy status."""
    response = client.get("/health")
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "markdown-api"


def test_convert_endpoint_success(client, test_file):
    """Test /convert endpoint with valid file."""
    response = client.post(
        "/convert",
        json={"file_path": str(test_file)}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "conversion_id" in data
    assert isinstance(data["conversion_id"], str)
    assert len(data["conversion_id"]) > 0


def test_convert_endpoint_file_not_found(client):
    """Test /convert endpoint with non-existent file."""
    response = client.post(
        "/convert",
        json={"file_path": "/nonexistent/file.pdf"}
    )
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_get_status_endpoint(client, monkeypatch):
    """Test /convert/{conversion_id} status endpoint."""
    # Mock the status database
    from app.api import main
    test_id = "test-conversion-123"
    main.conversion_status_db[test_id] = "completed"
    
    response = client.get(f"/convert/{test_id}")
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"


def test_get_status_not_found(client):
    """Test /convert/{conversion_id} with non-existent ID."""
    response = client.get("/convert/nonexistent-id")
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_get_converted_file_success(client, tmp_path, monkeypatch):
    """Test /converted/{file_path} endpoint."""
    # Create a mock converted file
    converted_dir = tmp_path / "converted_files"
    converted_dir.mkdir()
    test_file = converted_dir / "test_doc.md"
    test_content = "# Test Document\n\nThis is converted markdown."
    test_file.write_text(test_content)
    
    # Mock os.path.exists and file reading
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr("builtins.open", lambda p, mode: test_file.open(mode))
    
    response = client.get("/converted/test_doc.md")
    
    assert response.status_code == 200
    data = response.json()
    assert "content" in data
    assert data["content"] == test_content


def test_get_converted_file_not_found(client):
    """Test /converted/{file_path} with non-existent file."""
    response = client.get("/converted/nonexistent.md")
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_conversion_request_validation(client):
    """Test request validation for /convert endpoint."""
    # Missing file_path
    response = client.post("/convert", json={})
    assert response.status_code == 422  # Validation error
    
    # Invalid JSON
    response = client.post(
        "/convert",
        data="invalid json",
        headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_conversion_flow_integration(client, test_file, monkeypatch):
    """Test full conversion flow: submit -> poll -> retrieve."""
    # Step 1: Submit conversion
    response = client.post(
        "/convert",
        json={"file_path": str(test_file)}
    )
    assert response.status_code == 200
    conversion_id = response.json()["conversion_id"]
    
    # Step 2: Mock status as completed
    from app.api import main
    main.conversion_status_db[conversion_id] = "completed"
    
    # Check status
    status_response = client.get(f"/convert/{conversion_id}")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "completed"
    
    # Step 3: Mock converted file
    test_md_content = "# Converted Document"
    monkeypatch.setattr("os.path.exists", lambda p: True)
    
    def mock_open(path, mode):
        from io import StringIO
        return StringIO(test_md_content)
    
    monkeypatch.setattr("builtins.open", mock_open)
    
    # Retrieve converted content
    content_response = client.get(f"/converted/{test_file.name}")
    assert content_response.status_code == 200
    assert content_response.json()["content"] == test_md_content


def test_multiple_concurrent_conversions(client, tmp_path):
    """Test handling multiple conversion requests."""
    # Create multiple test files
    files = []
    for i in range(5):
        test_file = tmp_path / f"doc_{i}.pdf"
        test_file.write_bytes(f"PDF content {i}".encode())
        files.append(test_file)
    
    # Submit multiple conversions
    conversion_ids = []
    for test_file in files:
        response = client.post(
            "/convert",
            json={"file_path": str(test_file)}
        )
        assert response.status_code == 200
        conversion_ids.append(response.json()["conversion_id"])
    
    # Verify all IDs are unique
    assert len(conversion_ids) == len(set(conversion_ids))
    assert all(len(cid) > 0 for cid in conversion_ids)


def test_capabilities_endpoint(client):
    """Test /capabilities endpoint returns registered converters."""
    response = client.get("/capabilities")
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify response structure
    assert "converters" in data
    converters = data["converters"]
    assert isinstance(converters, list)
    assert len(converters) > 0
    
    # Verify expected converters are present
    converter_names = [c["name"] for c in converters]
    assert "pymupdf" in converter_names
    assert "markitdown" in converter_names
    assert "vlm" in converter_names
    assert "docling" in converter_names
    
    # Verify each converter has required fields
    for converter in converters:
        assert "name" in converter
        assert "label" in converter
        assert "description" in converter
        assert isinstance(converter["name"], str)
        assert isinstance(converter["label"], str)
        assert isinstance(converter["description"], str)
        assert len(converter["name"]) > 0
        assert len(converter["label"]) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
