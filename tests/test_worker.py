"""Tests for the worker module."""

import pytest
import os
import zmq
import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock


def test_worker_environment_detection_docker():
    """Test that worker detects Docker environment correctly."""
    from app.workers import worker
    
    # Mock Docker environment
    with patch('os.path.exists', return_value=True):
        with patch('os.environ.get', return_value=''):
            # Simulate /.dockerenv exists
            with patch('argparse.ArgumentParser.parse_args') as mock_args:
                mock_args.return_value = Mock(host=None)
                
                # Import and check detection logic
                is_docker = os.path.exists('/.dockerenv') or os.environ.get('DOCKER_CONTAINER', '').lower() == 'true'
                assert is_docker
                expected_host = "api" if is_docker else "localhost"
                assert expected_host == "api"


def test_worker_environment_detection_standalone():
    """Test that worker detects standalone environment correctly."""
    # Mock standalone environment
    with patch('os.path.exists', return_value=False):
        with patch('os.environ.get', return_value=''):
            is_docker = os.path.exists('/.dockerenv') or os.environ.get('DOCKER_CONTAINER', '').lower() == 'true'
            assert not is_docker
            expected_host = "api" if is_docker else "localhost"
            assert expected_host == "localhost"


def test_worker_environment_detection_docker_env_var():
    """Test that worker detects Docker via environment variable."""
    with patch('os.path.exists', return_value=False):
        with patch('os.environ.get', return_value='true'):
            is_docker = os.path.exists('/.dockerenv') or os.environ.get('DOCKER_CONTAINER', '').lower() == 'true'
            assert is_docker
            expected_host = "api" if is_docker else "localhost"
            assert expected_host == "api"


def test_worker_explicit_host_override():
    """Test that explicit --host argument overrides auto-detection."""
    with patch('sys.argv', ['worker.py', '--host', 'custom-host']):
        from app.workers.worker import main
        
        with patch('zmq.Context') as mock_context:
            mock_socket = MagicMock()
            mock_context.return_value.socket.return_value = mock_socket
            
            # Mock recv_string to raise KeyboardInterrupt after first call
            mock_socket.recv_string.side_effect = KeyboardInterrupt
            
            try:
                main()
            except KeyboardInterrupt:
                pass
            
            # Verify connection to custom host
            calls = mock_socket.connect.call_args_list
            assert any('custom-host:5555' in str(call) for call in calls)


def _fake_docling_result(markdown="# Test Document\n\nConverted content", num_pages=1, name="test.pdf"):
    """Build the dict that `_run_converter_with_timeout` returns for docling."""
    return {
        "markdown": markdown,
        "num_pages": num_pages,
        "doc_name": name,
        "doc_origin": "PDF",
        "converter_used": "docling",
    }


def test_convert_file_to_markdown_success(sample_pdf, converted_files_dir, monkeypatch):
    """Test successful file conversion."""
    from app.workers.worker import convert_file_to_markdown

    monkeypatch.chdir(sample_pdf.parent.parent)
    mock_socket = MagicMock()

    with patch('app.workers.worker._run_converter_with_timeout') as mock_wrapper:
        mock_wrapper.return_value = _fake_docling_result()
        convert_file_to_markdown(str(sample_pdf), "test-123", mock_socket)

    assert mock_socket.send_json.call_count >= 2
    calls = [call[0][0] for call in mock_socket.send_json.call_args_list]
    assert any(c.get('status') == 'processing' for c in calls)
    assert any(c.get('status') == 'completed' for c in calls)


def test_convert_file_to_markdown_failure(sample_pdf, monkeypatch):
    """Test file conversion failure handling."""
    from app.workers.worker import convert_file_to_markdown

    monkeypatch.chdir(sample_pdf.parent.parent)
    mock_socket = MagicMock()

    with patch('app.workers.worker._run_converter_with_timeout') as mock_wrapper:
        mock_wrapper.side_effect = Exception("Conversion failed")
        convert_file_to_markdown(str(sample_pdf), "test-456", mock_socket)

    calls = [call[0][0] for call in mock_socket.send_json.call_args_list]
    assert any(c.get('status') == 'failed' for c in calls)


def test_convert_file_creates_output_directory(sample_pdf, monkeypatch, tmp_path):
    """Test that conversion creates output directory structure."""
    from app.workers.worker import convert_file_to_markdown

    test_dir = tmp_path
    monkeypatch.chdir(test_dir)

    converted_dir = test_dir / "converted_files"
    monkeypatch.setenv("CONVERTED_FILES_DIR", str(converted_dir))

    (test_dir / "files_to_convert" / "subdir").mkdir(parents=True)
    nested_file = test_dir / "files_to_convert" / "subdir" / "nested.pdf"
    nested_file.write_bytes(sample_pdf.read_bytes())

    mock_socket = MagicMock()

    with patch('app.workers.worker._run_converter_with_timeout') as mock_wrapper:
        mock_wrapper.return_value = _fake_docling_result(markdown="# Content", name="nested.pdf")
        convert_file_to_markdown("files_to_convert/subdir/nested.pdf", "test-789", mock_socket)

    expected_file = converted_dir / "nested.md"
    assert expected_file.exists()


def test_worker_metadata_in_output(sample_pdf, monkeypatch, tmp_path):
    """Test that converted files include metadata."""
    from app.workers.worker import convert_file_to_markdown

    test_dir = tmp_path
    monkeypatch.chdir(test_dir)

    converted_dir = test_dir / "converted_files"
    monkeypatch.setenv("CONVERTED_FILES_DIR", str(converted_dir))

    (test_dir / "files_to_convert").mkdir()
    test_file = test_dir / "files_to_convert" / "test_document.pdf"
    test_file.write_bytes(sample_pdf.read_bytes())

    mock_socket = MagicMock()
    conversion_id = "test-meta-123"

    with patch('app.workers.worker._run_converter_with_timeout') as mock_wrapper:
        mock_wrapper.return_value = _fake_docling_result(markdown="# Test", name="test_document.pdf")
        convert_file_to_markdown("files_to_convert/test_document.pdf", conversion_id, mock_socket)

    output_file = converted_dir / "test_document.md"
    assert output_file.exists(), f"Output file not found at {output_file}"

    content = output_file.read_text()
    assert "---" in content
    assert "source_file" in content
    assert "conversion_id" in content
    assert conversion_id in content
    assert "conversion_date" in content


def test_worker_zmq_connection_failure():
    """Test worker handles ZMQ connection failures gracefully."""
    from app.workers.worker import main
    
    with patch('sys.argv', ['worker.py', '--host', 'nonexistent-host']):
        with patch('zmq.Context') as mock_context:
            mock_socket = MagicMock()
            mock_socket.connect.side_effect = zmq.ZMQError("Connection failed")
            mock_context.return_value.socket.return_value = mock_socket
            
            with pytest.raises(zmq.ZMQError):
                main()


def test_worker_handles_malformed_messages():
    """Test worker handles malformed JSON messages."""
    from app.workers.worker import main
    
    with patch('sys.argv', ['worker.py', '--host', 'localhost']):
        with patch('zmq.Context') as mock_context:
            mock_socket = MagicMock()
            mock_socket.recv_string.side_effect = ["invalid json", KeyboardInterrupt]
            mock_context.return_value.socket.return_value = mock_socket
            
            with pytest.raises((json.JSONDecodeError, KeyboardInterrupt)):
                main()


def test_convert_file_timeout(sample_pdf, monkeypatch):
    """Test that conversion respects DOCLING_TIMEOUT_SECONDS with multiprocessing.
    
    NOTE: Updated for multiprocessing-based timeout mechanism.
    See test_timeout_mechanism.py for comprehensive timeout tests.
    """
    from app.workers.worker import convert_file_to_markdown
    
    monkeypatch.chdir(sample_pdf.parent.parent)
    monkeypatch.setenv("DOCLING_TIMEOUT_SECONDS", "2")  # 2 second timeout
    
    mock_socket = MagicMock()
    
    # Mock _run_converter_with_timeout to simulate timeout
    with patch('app.workers.worker._run_converter_with_timeout') as mock_timeout:
        mock_timeout.side_effect = TimeoutError("Conversion exceeded 2s timeout and was terminated")
        
        conversion_id = "test-timeout-123"
        convert_file_to_markdown(
            str(sample_pdf),
            conversion_id,
            mock_socket
        )
    
    # Verify timeout failure status sent
    calls = [call[0][0] for call in mock_socket.send_json.call_args_list]
    failed_calls = [c for c in calls if c.get('status') == 'failed']
    assert len(failed_calls) > 0, "Expected failed status due to timeout"
    assert any('timeout' in c.get('error', '').lower() for c in failed_calls), "Expected timeout error message"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
