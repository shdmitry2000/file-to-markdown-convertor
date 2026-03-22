import zmq
import json
import os
import time
import threading
from pathlib import Path
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.datamodel.base_models import InputFormat
import frontmatter
from datetime import datetime
import logging
import argparse

# Import configuration
from app.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Load settings
settings = get_settings()
logger.info(f"Worker starting in {settings.ENVIRONMENT} mode")
logger.info(f"Converted files directory: {settings.CONVERTED_FILES_DIR}")
logger.info(f"ZeroMQ task URL: {settings.zeromq_task_url}")
logger.info(f"ZeroMQ result URL: {settings.zeromq_result_url}")

# ---------------------------------------------------------------------------
# OpenTelemetry setup — initialized once at worker startup
# ---------------------------------------------------------------------------

def _setup_telemetry():
    """Initialize OTel tracer for this worker process."""
    try:
        import sys
        # Allow both Docker (/app/shared) and local (repo root/shared) paths
        for p in ["/app", str(Path(__file__).parent.parent.parent.parent)]:
            if p not in sys.path:
                sys.path.insert(0, p)
        from shared.utils.telemetry import init_telemetry
        return init_telemetry("markdown-worker")
    except Exception as e:
        logger.warning(f"[telemetry] Could not load shared telemetry: {e}. Tracing disabled.")
        from unittest.mock import MagicMock
        t = MagicMock()
        t.start_as_current_span.return_value.__enter__ = lambda s, *a: MagicMock()
        t.start_as_current_span.return_value.__exit__ = lambda s, *a: None
        return t

tracer = _setup_telemetry()


def _clean_markdown_content(content: str) -> str:
    """Clean markdown content to handle problematic characters.
    
    This function removes ONLY problematic characters that cause technical issues:
    - Null bytes (\x00) that PostgreSQL cannot store
    - Control characters (0x00-0x1F except newlines/tabs/CR, 0x7F-0x9F) that cause parsing issues
    
    PRESERVES all valid Unicode including:
    - Hebrew (עברית), Arabic (العربية), Chinese (中文)
    - All other valid UTF-8 characters
    - Valid whitespace (newlines, tabs, carriage returns)
    
    By cleaning at the source (markdown converter), all RAG templates
    receive clean, compatible content without needing template-specific fixes.
    
    Args:
        content: Raw markdown content from docling
    
    Returns:
        Cleaned markdown content safe for storage and embedding
    """
    if not content:
        return content
    
    import re
    
    # Remove null bytes (PostgreSQL can't store them)
    content = content.replace('\x00', '')
    
    # Remove control characters that cause parsing issues
    # Keep: \n (0x0A), \r (0x0D), \t (0x09)
    # Remove: All other control chars (0x00-0x08, 0x0B-0x0C, 0x0E-0x1F, 0x7F-0x9F)
    content = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F-\x9F]', '', content)
    
    return content


def _convert_with_docling(file_path: str, conversion_id: str, result_sender_socket):
    """
    Original Docling conversion logic exactly unchanged (isolated).
    """
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    with tracer.start_as_current_span("convert_file") as span:
        span.set_attribute("conversion.id", conversion_id)
        span.set_attribute("file.path", file_path)
        span.set_attribute("file.size_bytes", file_size)
        span.set_attribute("file.name", Path(file_path).name)
        span.set_attribute("converter_type", "docling")

        logger.info(f"[{conversion_id}] Received conversion task for: {file_path}")
        result_sender_socket.send_json(
            {"conversion_id": conversion_id, "status": "processing"}
        )

        try:
            # Use file path as-is - API and worker share same filesystem view
            if not os.path.exists(file_path):
                error_msg = f"File not found: {file_path}"
                logger.error(f"[{conversion_id}] {error_msg}")
                span.set_attribute("error", True)
                span.set_attribute("error.message", error_msg)
                result_sender_socket.send_json(
                    {"conversion_id": conversion_id, "status": "failed", "error": error_msg}
                )
                return
            
            # Write converted files to shared cache directory
            converted_dir = os.getenv("CONVERTED_FILES_DIR", "./data/converted_files")
            os.makedirs(converted_dir, exist_ok=True)
            
            # Use file stem for flat structure (e.g., 439.pdf -> 439.md)
            stem = Path(file_path).stem
            converted_file_path = os.path.join(converted_dir, f"{stem}.md")
            span.set_attribute("output.path", converted_file_path)
            
            logger.info(f"[{conversion_id}] Converting: {file_path} -> {converted_file_path}")

            # Configure PDF pipeline with OCR setting from environment
            do_ocr = os.getenv("DOCLING_DO_OCR", "false").lower() in ("true", "1", "yes")
            span.set_attribute("docling.ocr_enabled", do_ocr)
            
            pdf_options = PdfPipelineOptions()
            pdf_options.do_ocr = do_ocr
            
            # Convert the file with configured OCR setting
            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)
                }
            )
            
            if do_ocr:
                logger.info(f"[{conversion_id}] OCR enabled - may download models on first run")
            
            # Add timeout for conversion (default 2 hours for large/complex PDFs)
            CONVERSION_TIMEOUT_SECONDS = int(os.getenv("DOCLING_TIMEOUT_SECONDS", "7200"))
            span.set_attribute("docling.timeout_seconds", CONVERSION_TIMEOUT_SECONDS)
            
            # Use threading to implement timeout for synchronous conversion
            conversion_result = [None]  # Mutable container to store result
            conversion_error = [None]
            
            def convert_with_timeout():
                try:
                    conversion_result[0] = converter.convert(file_path)
                except Exception as e:
                    conversion_error[0] = e
            
            # ── Docling conversion span ──────────────────────────────────
            with tracer.start_as_current_span("docling.convert") as docling_span:
                docling_span.set_attribute("file.path", file_path)
                docling_span.set_attribute("ocr_enabled", do_ocr)

                conversion_thread = threading.Thread(target=convert_with_timeout)
                conversion_thread.daemon = True
                conversion_thread.start()
                conversion_thread.join(timeout=CONVERSION_TIMEOUT_SECONDS)
                
                if conversion_thread.is_alive():
                    # Timeout occurred
                    error_msg = f"Conversion exceeded {CONVERSION_TIMEOUT_SECONDS}s timeout"
                    logger.error(f"[{conversion_id}] {error_msg}")
                    docling_span.set_attribute("error", True)
                    docling_span.set_attribute("error.message", error_msg)
                    span.set_attribute("error", True)
                    result_sender_socket.send_json({
                        "conversion_id": conversion_id,
                        "status": "failed",
                        "error": error_msg
                    })
                    return
                
                if conversion_error[0]:
                    docling_span.record_exception(conversion_error[0])
                    raise conversion_error[0]
                
                if conversion_result[0] is None:
                    raise ValueError("Conversion returned no result")
                
                result = conversion_result[0]
                num_pages = result.document.num_pages()
                docling_span.set_attribute("document.num_pages", num_pages)
                span.set_attribute("document.num_pages", num_pages)
                logger.info(f"[{conversion_id}] Docling converted {num_pages} pages")

            markdown_content = result.document.export_to_markdown()
            
            # Clean markdown content to handle anomalies
            markdown_content = _clean_markdown_content(markdown_content)
            span.set_attribute("output.markdown_length", len(markdown_content))

            # ── File write span ──────────────────────────────────────────
            with tracer.start_as_current_span("write_output") as write_span:
                write_span.set_attribute("output.path", converted_file_path)

                # Create metadata
                metadata = {
                    "source_file": file_path,
                    "conversion_id": conversion_id,
                    "conversion_date": datetime.now().isoformat(),
                    "docling_name": repr(result.document.name),
                    "docling_origin": repr(result.document.origin),
                    "docling_num_pages": result.document.num_pages(),
                }

                # Create a frontmatter Post
                post = frontmatter.Post(markdown_content)
                post.metadata = metadata

                # Save the converted file with metadata header
                with open(converted_file_path, "w", encoding='utf-8', errors='replace') as f:
                    f.write(frontmatter.dumps(post))

            result_sender_socket.send_json(
                {"conversion_id": conversion_id, "status": "completed"}
            )
            logger.info(f"[{conversion_id}] Successfully converted to {converted_file_path}")

        except Exception as e:
            logger.error(f"[{conversion_id}] Conversion failed: {e}", exc_info=True)
            try:
                span.record_exception(e)
                span.set_attribute("error", True)
            except Exception:
                pass
            result_sender_socket.send_json(
                {"conversion_id": conversion_id, "status": "failed", "error": str(e)}
            )


def convert_file_to_markdown(file_path: str, conversion_id: str, result_sender_socket, converter_type: str = "docling"):
    """
    Routing proxy that delegates Docling tasks to strict insulation or dynamically uses other plugins.
    """
    if converter_type == "docling" or not converter_type:
        return _convert_with_docling(file_path, conversion_id, result_sender_socket)
        
    # Use PyMuPDF, MarkItDown, etc plugin
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    with tracer.start_as_current_span("convert_file") as span:
        span.set_attribute("conversion.id", conversion_id)
        span.set_attribute("file.path", file_path)
        span.set_attribute("file.size_bytes", file_size)
        span.set_attribute("file.name", Path(file_path).name)
        span.set_attribute("converter_type", converter_type)

        logger.info(f"[{conversion_id}] Received conversion task for: {file_path} using {converter_type}")
        result_sender_socket.send_json(
            {"conversion_id": conversion_id, "status": "processing"}
        )

        try:
            if not os.path.exists(file_path):
                error_msg = f"File not found: {file_path}"
                result_sender_socket.send_json({"conversion_id": conversion_id, "status": "failed", "error": error_msg})
                return
            
            # Use configured converted files directory
            converted_dir = settings.CONVERTED_FILES_DIR
            os.makedirs(converted_dir, exist_ok=True)
            stem = Path(file_path).stem
            converted_file_path = os.path.join(converted_dir, f"{stem}.md")
            span.set_attribute("output.path", converted_file_path)
            
            # Load dynamic converter from new converted backend
            from app.converters.pymupdf import PyMuPDFConverter
            from app.converters.markitdown import MarkItDownConverter
            from app.converters.vlm import VLMConverter
            from app.converters.marker import MarkerConverter
            from app.converters.docling import DoclingConverter

            converters_map = {
                "pymupdf": PyMuPDFConverter,
                "markitdown": MarkItDownConverter,
                "vlm": VLMConverter,
                "marker": MarkerConverter,
                "docling": DoclingConverter
            }
            converter_cls = converters_map.get(converter_type)
            if not converter_cls:
                raise ValueError(f"Unknown converter type: {converter_type}")
            converter = converter_cls()
            
            markdown_content = converter.convert(Path(file_path))
            markdown_content = _clean_markdown_content(markdown_content)
            span.set_attribute("output.markdown_length", len(markdown_content))

            with tracer.start_as_current_span("write_output") as write_span:
                write_span.set_attribute("output.path", converted_file_path)
                
                metadata = {
                    "source_file": file_path,
                    "conversion_id": conversion_id,
                    "conversion_date": datetime.now().isoformat(),
                    "converter_used": converter_type
                }
                post = frontmatter.Post(markdown_content)
                post.metadata = metadata

                with open(converted_file_path, "w", encoding='utf-8', errors='replace') as f:
                    f.write(frontmatter.dumps(post))

            result_sender_socket.send_json({"conversion_id": conversion_id, "status": "completed"})
            logger.info(f"[{conversion_id}] Successfully converted via {converter_type} to {converted_file_path}")

        except Exception as e:
            logger.error(f"[{conversion_id}] Conversion via {converter_type} failed: {e}", exc_info=True)
            try:
                span.record_exception(e)
                span.set_attribute("error", True)
            except Exception:
                pass
            result_sender_socket.send_json({"conversion_id": conversion_id, "status": "failed", "error": str(e)})


def main():
    parser = argparse.ArgumentParser(description="ZeroMQ worker for file conversion.")
    parser.add_argument("--host", type=str, default=None, help="ZeroMQ host (overrides auto-detection)")
    args = parser.parse_args()
    
    # Use CLI arg, or fall back to settings (which auto-detects)
    host = args.host if args.host else settings.ZEROMQ_HOST
    
    logger.info(f"Worker connecting to ZeroMQ host: {host}")
    logger.info(f"Task port: {settings.ZEROMQ_TASK_PORT}, Result port: {settings.ZEROMQ_RESULT_PORT}")
    
    context = zmq.Context()
    task_receiver_socket = context.socket(zmq.PULL)
    task_receiver_socket.connect(f"tcp://{host}:{settings.ZEROMQ_TASK_PORT}")
    result_sender_socket = context.socket(zmq.PUSH)
    result_sender_socket.connect(f"tcp://{host}:{settings.ZEROMQ_RESULT_PORT}")

    while True:
        message = task_receiver_socket.recv_string()
        task = json.loads(message)
        conversion_id = task["conversion_id"]
        file_path = task["file_path"]
        converter_type = task.get("converter_type", "docling")
        convert_file_to_markdown(file_path, conversion_id, result_sender_socket, converter_type)

if __name__ == "__main__":
    main()
