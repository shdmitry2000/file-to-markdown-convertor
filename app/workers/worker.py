import zmq
import json
import os
import time
import multiprocessing
import queue as queue_module
from pathlib import Path
import frontmatter
from datetime import datetime
import logging
import argparse
import uuid

# Import configuration
from app.config import get_settings

# Import chunkers to trigger @register_chunker decorators
import app.chunkers  # noqa: F401


def _read_do_ocr() -> bool:
    """Read DOCLING_DO_OCR env var. Defaults to OFF.

    Read once per request in the parent so the OTel span and the value
    handed to the subprocess always agree.
    """
    return os.getenv("DOCLING_DO_OCR", "false").lower() in ("true", "1", "yes")


def _read_do_table_structure() -> bool:
    """Read DOCLING_DO_TABLE_STRUCTURE env var. Defaults to ON.
    
    Table extraction is 4x slower but preserves table structure in markdown.
    """
    return os.getenv("DOCLING_DO_TABLE_STRUCTURE", "true").lower() in ("true", "1", "yes")


def _read_timeout_seconds() -> int:
    """Conversion hard timeout in seconds. Default 2h."""
    return int(os.getenv("DOCLING_TIMEOUT_SECONDS", "7200"))

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


def _letter_count(text: str) -> int:
    import unicodedata
    return sum(1 for c in text if unicodedata.category(c).startswith("L"))


def _docling_output_unusable(markdown: str) -> bool:
    """Detect text-layer garbage — e.g. an embedded font with no ToUnicode CMap,
    where extractors emit glyph/symbol codes with almost no real letters. Such
    documents are only recoverable via the VLM (vision) path."""
    if not markdown or not markdown.strip():
        return True
    letters = _letter_count(markdown)
    if letters == 0:
        return True
    min_ratio = float(os.getenv("VLM_FALLBACK_MIN_LETTER_RATIO", "0.10"))
    return len(markdown) >= 200 and (letters / len(markdown)) < min_ratio


def _vlm_fallback_enabled() -> bool:
    return os.getenv("VLM_FALLBACK_ENABLED", "true").lower() in ("1", "true", "yes")


def _convert_in_subprocess(file_path: str, converter_type: str, do_ocr: bool, result_queue, do_table_structure: bool = True) -> None:
    """Subprocess target: run a single conversion and put the result on `result_queue`.

    Module-level so multiprocessing can pickle it under the `spawn` start method,
    and so tests can swap in their own callable without monkeypatching.
    """
    try:
        converter_used = converter_type or "docling"
        if converter_type == "docling" or not converter_type:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.datamodel.base_models import InputFormat

            pdf_options = PdfPipelineOptions()
            pdf_options.do_ocr = do_ocr
            pdf_options.do_table_structure = do_table_structure
            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)
                }
            )
            result = converter.convert(file_path)
            markdown = result.document.export_to_markdown()
            num_pages = result.document.num_pages()
            doc_name = repr(result.document.name)
            doc_origin = repr(result.document.origin)

            # Auto-fallback: a broken text layer (e.g. embedded font with no
            # ToUnicode CMap -> glyph garbage) yields markdown with almost no real
            # letters. Retry such docs with the VLM (vision) converter, which reads
            # the rendered page instead of the text layer. Fail loudly if even VLM
            # produces nothing, so the doc is flagged rather than silently indexed.
            if _vlm_fallback_enabled() and _docling_output_unusable(markdown):
                logger.warning(
                    "docling output unusable for %s (letters=%d, len=%d) — falling back to VLM",
                    file_path, _letter_count(markdown), len(markdown),
                )
                from app.converters.vlm import VLMConverter

                vlm_markdown = VLMConverter(backend="factory").convert(Path(file_path))
                if _docling_output_unusable(vlm_markdown):
                    raise Exception(
                        "Unreadable document: both docling and VLM produced no usable "
                        "text (likely scanned/encrypted/empty) — needs review."
                    )
                markdown = vlm_markdown
                converter_used = "docling+vlm"
                logger.info("VLM fallback recovered %s (letters=%d)", file_path, _letter_count(markdown))
        else:
            from app.converters.pymupdf import PyMuPDFConverter
            from app.converters.markitdown import MarkItDownConverter
            from app.converters.vlm import VLMConverter
            from app.converters.marker import MarkerConverter

            converters_map = {
                "pymupdf": PyMuPDFConverter,
                "markitdown": MarkItDownConverter,
                "vlm": VLMConverter,
                "marker": MarkerConverter,
            }
            cls = converters_map.get(converter_type)
            if not cls:
                raise ValueError(f"Unknown converter type: {converter_type}")

            converter = cls()
            markdown = converter.convert(Path(file_path))
            num_pages = max(1, len(markdown) // 2000)
            doc_name = str(Path(file_path).name)
            doc_origin = f"converter:{converter_type}"

        result_queue.put(("success", {
            "markdown": markdown,
            "num_pages": num_pages,
            "doc_name": doc_name,
            "doc_origin": doc_origin,
            "converter_used": converter_used,
        }))
    except Exception as e:
        import traceback
        result_queue.put(("error", str(e), traceback.format_exc()))


def _run_converter_with_timeout(
    file_path: str,
    conversion_id: str,
    converter_type: str,
    timeout_seconds: int,
    do_ocr: bool = False,
    do_table_structure: bool = True,
    *,
    _target=None,
) -> dict:
    """Run a conversion in a separate process with a hard timeout.

    Args:
        file_path: Path to file to convert.
        conversion_id: Used for log correlation.
        converter_type: docling | pymupdf | markitdown | marker | vlm.
        timeout_seconds: Hard wall-clock limit; process is killed if exceeded.
        do_ocr: Forwarded to the docling pipeline. Caller decides — there is no
            implicit env lookup here, so the parent's OTel attributes and the
            subprocess always see the same value.
        do_table_structure: Enable table structure extraction (4x slower).
        _target: Test hook; defaults to `_convert_in_subprocess`.

    Why we drain the queue before joining: a child that put() a large payload
    (multi-MB markdown) blocks until the OS pipe is drained. If we join() first
    we deadlock until the timeout, then kill a process that actually succeeded.
    Reading from the queue first unblocks the put and lets the child exit.
    """
    target = _target or _convert_in_subprocess
    result_queue = multiprocessing.Queue()

    process = multiprocessing.Process(
        target=target,
        args=(file_path, converter_type, do_ocr, result_queue, do_table_structure),
    )
    process.start()

    # Drain the queue first; poll so we can also notice early subprocess death.
    deadline = time.monotonic() + timeout_seconds
    item = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            item = result_queue.get(timeout=min(2.0, remaining))
            break
        except queue_module.Empty:
            if not process.is_alive():
                # Crashed / killed without producing a result.
                break

    if item is None:
        if process.is_alive():
            logger.warning(
                f"[{conversion_id}] Conversion timeout ({timeout_seconds}s), terminating process..."
            )
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join()
            raise TimeoutError(
                f"Conversion exceeded {timeout_seconds}s timeout and was terminated"
            )
        # Process exited without producing a result — surface the exit code.
        process.join(timeout=1)
        raise Exception(
            f"Conversion process exited (code={process.exitcode}) without producing a result"
        )

    # Got a result; let the child exit cleanly.
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join()

    if item[0] == "error":
        error_msg = item[1]
        traceback_info = item[2] if len(item) > 2 else ""
        logger.error(f"[{conversion_id}] Subprocess error:\n{error_msg}\n{traceback_info}")
        raise Exception(error_msg)
    return item[1]


def _convert_with_docling(file_path: str, conversion_id: str, result_sender_socket):
    """
    Docling conversion with timeout protection via multiprocessing.
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

            do_ocr = _read_do_ocr()
            do_table_structure = _read_do_table_structure()
            CONVERSION_TIMEOUT_SECONDS = _read_timeout_seconds()
            
            # Log config for debugging
            logger.info(f"[{conversion_id}] Docling config: OCR={do_ocr}, Tables={do_table_structure}, Timeout={CONVERSION_TIMEOUT_SECONDS}s")
            
            span.set_attribute("docling.timeout_seconds", CONVERSION_TIMEOUT_SECONDS)
            span.set_attribute("docling.ocr_enabled", do_ocr)
            span.set_attribute("docling.table_structure_enabled", do_table_structure)

            # ── Docling conversion span ──────────────────────────────────
            with tracer.start_as_current_span("docling.convert") as docling_span:
                docling_span.set_attribute("file.path", file_path)
                docling_span.set_attribute("ocr_enabled", do_ocr)
                docling_span.set_attribute("table_structure_enabled", do_table_structure)

                try:
                    result_data = _run_converter_with_timeout(
                        file_path=file_path,
                        conversion_id=conversion_id,
                        converter_type="docling",
                        timeout_seconds=CONVERSION_TIMEOUT_SECONDS,
                        do_ocr=do_ocr,
                        do_table_structure=do_table_structure,
                    )
                    
                    markdown_content = result_data["markdown"]
                    num_pages = result_data["num_pages"]
                    doc_name = result_data["doc_name"]
                    doc_origin = result_data["doc_origin"]
                    
                    docling_span.set_attribute("document.num_pages", num_pages)
                    span.set_attribute("document.num_pages", num_pages)
                    logger.info(f"[{conversion_id}] Docling converted {num_pages} pages")
                    
                except TimeoutError as e:
                    error_msg = str(e)
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
                except Exception as e:
                    logger.error(f"[{conversion_id}] Conversion error: {e}")
                    docling_span.record_exception(e)
                    docling_span.set_attribute("error", True)
                    span.set_attribute("error", True)
                    result_sender_socket.send_json({
                        "conversion_id": conversion_id,
                        "status": "failed",
                        "error": str(e)
                    })
                    return
            
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
                    "docling_name": doc_name,
                    "docling_origin": doc_origin,
                    "docling_num_pages": num_pages,
                }

                # Create a frontmatter Post
                post = frontmatter.Post(markdown_content)
                post.metadata = metadata

                # Save the converted file atomically
                tmp_file_path = f"{converted_file_path}.tmp_{uuid.uuid4().hex}"
                with open(tmp_file_path, "w", encoding='utf-8', errors='replace') as f:
                    f.write(frontmatter.dumps(post))
                os.replace(tmp_file_path, converted_file_path)

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
        
    # Use PyMuPDF, MarkItDown, Marker, VLM, etc plugin with timeout protection
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
            
            CONVERSION_TIMEOUT_SECONDS = _read_timeout_seconds()
            span.set_attribute("converter.timeout_seconds", CONVERSION_TIMEOUT_SECONDS)

            with tracer.start_as_current_span("converter.convert") as converter_span:
                converter_span.set_attribute("converter_type", converter_type)

                try:
                    result_data = _run_converter_with_timeout(
                        file_path=file_path,
                        conversion_id=conversion_id,
                        converter_type=converter_type,
                        timeout_seconds=CONVERSION_TIMEOUT_SECONDS,
                    )
                    
                    markdown_content = result_data["markdown"]
                    num_pages = result_data.get("num_pages", 0)
                    converter_span.set_attribute("document.num_pages", num_pages)
                    
                except TimeoutError as e:
                    error_msg = f"{converter_type} conversion: {str(e)}"
                    logger.error(f"[{conversion_id}] {error_msg}")
                    converter_span.set_attribute("error", True)
                    converter_span.set_attribute("error.message", error_msg)
                    result_sender_socket.send_json({
                        "conversion_id": conversion_id,
                        "status": "failed",
                        "error": error_msg
                    })
                    return

            markdown_content = _clean_markdown_content(markdown_content)
            span.set_attribute("output.markdown_length", len(markdown_content))

            with tracer.start_as_current_span("write_output") as write_span:
                write_span.set_attribute("output.path", converted_file_path)
                
                metadata = {
                    "source_file": file_path,
                    "conversion_id": conversion_id,
                    "conversion_date": datetime.now().isoformat(),
                    "converter_used": converter_type,
                    "num_pages": num_pages
                }
                post = frontmatter.Post(markdown_content)
                post.metadata = metadata

                # Save the converted file atomically
                tmp_file_path = f"{converted_file_path}.tmp_{uuid.uuid4().hex}"
                with open(tmp_file_path, "w", encoding='utf-8', errors='replace') as f:
                    f.write(frontmatter.dumps(post))
                os.replace(tmp_file_path, converted_file_path)

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


def chunk_file(file_path: str, chunk_id: str, result_sender_socket, chunker_type: str = "docling_hybrid", params: dict = None):
    """Handle a chunking task delivered via the convert worker's PUSH queue.

    Result is sent back on result_sender_socket (5556) with type='chunk',
    matching the existing HTTP /chunk-async + status-polling flow in
    app/api/main.py. For synchronous REQ/REP ingest (the v2 indexer path),
    use chunk_server.py instead — it bypasses the convert queue entirely.
    """
    params = params or {}
    result_socket = result_sender_socket

    with tracer.start_as_current_span("chunk_file") as span:
        span.set_attribute("chunk.id", chunk_id)
        span.set_attribute("file.path", file_path)
        span.set_attribute("file.name", Path(file_path).name)
        span.set_attribute("chunker_type", chunker_type)

        logger.info(f"[{chunk_id}] Received chunking task for: {file_path} using {chunker_type}")

        result_socket.send_json({
            "chunk_id": chunk_id,
            "status": "processing",
            "type": "chunk"
        })
        
        try:
            if not os.path.exists(file_path):
                error_msg = f"File not found: {file_path}"
                result_socket.send_json({
                    "chunk_id": chunk_id,
                    "status": "failed",
                    "error": error_msg,
                    "type": "chunk"
                })
                return
            
            # Get chunker from registry
            from app.registry import registry
            chunker = registry.get_chunker(chunker_type)
            
            if chunker is None:
                error_msg = f"Unknown chunker type: {chunker_type}"
                result_socket.send_json({
                    "chunk_id": chunk_id,
                    "status": "failed",
                    "error": error_msg,
                    "type": "chunk"
                })
                return
            
            with tracer.start_as_current_span("chunker.chunk") as chunker_span:
                chunker_span.set_attribute("chunker_type", chunker_type)
                chunker_span.set_attribute("params", json.dumps(params))
                
                # Chunk the file (PDF → chunks directly)
                chunks = chunker.chunk(file_path, params)
                
                chunker_span.set_attribute("chunks.count", len(chunks))
            
            result_socket.send_json({
                "chunk_id": chunk_id,
                "status": "completed",
                "chunks": chunks,
                "type": "chunk"
            })
            logger.info(f"[{chunk_id}] Successfully chunked {file_path} into {len(chunks)} chunks")
            
        except Exception as e:
            logger.error(f"[{chunk_id}] Chunking failed: {e}", exc_info=True)
            try:
                span.record_exception(e)
                span.set_attribute("error", True)
            except Exception:
                pass
            result_socket.send_json({
                "chunk_id": chunk_id,
                "status": "failed",
                "error": str(e),
                "type": "chunk"
            })


def main():
    parser = argparse.ArgumentParser(description="ZeroMQ worker for file conversion and chunking.")
    parser.add_argument("--host", type=str, default=None, help="ZeroMQ host (overrides auto-detection)")
    args = parser.parse_args()
    
    # Use CLI arg, or fall back to settings (which auto-detects)
    host = args.host if args.host else settings.ZEROMQ_HOST

    logger.info(f"Worker connecting to ZeroMQ host: {host}")
    logger.info(f"Task port: {settings.ZMQ_TASK_PORT}, Result port: {settings.ZMQ_RESULT_PORT}")

    context = zmq.Context()
    task_receiver_socket = context.socket(zmq.PULL)
    task_receiver_socket.connect(f"tcp://{host}:{settings.ZMQ_TASK_PORT}")
    result_sender_socket = context.socket(zmq.PUSH)
    result_sender_socket.connect(f"tcp://{host}:{settings.ZMQ_RESULT_PORT}")

    while True:
        message = task_receiver_socket.recv_string()
        task = json.loads(message)
        task_type = task.get("type", "convert")  # Default: convert
        
        if task_type == "convert":
            # Existing conversion flow
            conversion_id = task["conversion_id"]
            file_path = task["file_path"]
            converter_type = task.get("converter_type", "docling")
            convert_file_to_markdown(file_path, conversion_id, result_sender_socket, converter_type)
        elif task_type == "chunk":
            # Chunking via HTTP /chunk-async flow. Synchronous REQ/REP ingest
            # uses chunk_server.py (port 5557) instead.
            chunk_id = task["chunk_id"]
            file_path = task["file_path"]
            chunker_type = task.get("chunker", "docling_hybrid")
            params = task.get("params", {})
            chunk_file(file_path, chunk_id, result_sender_socket, chunker_type, params)
        else:
            logger.error(f"Unknown task type: {task_type}")
            # Send error response (need an ID field)
            task_id = task.get("chunk_id") or task.get("conversion_id") or "unknown"
            result_sender_socket.send_json({
                "task_id": task_id,
                "status": "failed",
                "error": f"Unknown task type: {task_type}"
            })

if __name__ == "__main__":
    # Required for multiprocessing on some platforms
    multiprocessing.set_start_method('spawn', force=True)
    main()
