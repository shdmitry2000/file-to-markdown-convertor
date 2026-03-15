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

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


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


def convert_file_to_markdown(file_path: str, conversion_id: str, result_sender_socket):
    """
    Converts a file to markdown and sends status updates back to the API.
    
    Simple approach: Use file_path as provided by API.
    Both API and worker should share the same PROJECTS_BASE_PATH, so paths are consistent.
    
    - Standalone: Both use /Users/.../data/projects
    - Docker/K8s: Both mount PVC at /app/projects
    """
    logger.info(f"[{conversion_id}] Received conversion task for: {file_path}")
    result_sender_socket.send_json(
        {"conversion_id": conversion_id, "status": "processing"}
    )

    try:
        # Use file path as-is - API and worker share same filesystem view
        if not os.path.exists(file_path):
            error_msg = f"File not found: {file_path}"
            logger.error(f"[{conversion_id}] {error_msg}")
            result_sender_socket.send_json(
                {"conversion_id": conversion_id, "status": "failed", "error": error_msg}
            )
            return
        
        # Write converted files to shared cache directory
        # K8s: /app/projects/.cache/converted_files (on PVC)
        # Standalone: ./data/converted_files (or custom via env var)
        converted_dir = os.getenv("CONVERTED_FILES_DIR", "./data/converted_files")
        os.makedirs(converted_dir, exist_ok=True)
        
        # Use file stem for flat structure (e.g., 439.pdf -> 439.md)
        stem = Path(file_path).stem
        converted_file_path = os.path.join(converted_dir, f"{stem}.md")
        
        logger.info(f"[{conversion_id}] Converting: {file_path} -> {converted_file_path}")

        # Configure PDF pipeline with OCR setting from environment
        # OCR is disabled by default (faster, no model downloads) but can be enabled if needed
        do_ocr = os.getenv("DOCLING_DO_OCR", "false").lower() in ("true", "1", "yes")
        
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
        
        # Add timeout for conversion (default 60 min)
        CONVERSION_TIMEOUT_SECONDS = int(os.getenv("DOCLING_TIMEOUT_SECONDS", "3600"))
        
        # Use threading to implement timeout for synchronous conversion
        conversion_result = [None]  # Mutable container to store result
        conversion_error = [None]
        
        def convert_with_timeout():
            try:
                conversion_result[0] = converter.convert(file_path)
            except Exception as e:
                conversion_error[0] = e
        
        conversion_thread = threading.Thread(target=convert_with_timeout)
        conversion_thread.daemon = True
        conversion_thread.start()
        conversion_thread.join(timeout=CONVERSION_TIMEOUT_SECONDS)
        
        if conversion_thread.is_alive():
            # Timeout occurred
            error_msg = f"Conversion exceeded {CONVERSION_TIMEOUT_SECONDS}s timeout"
            logger.error(f"[{conversion_id}] {error_msg}")
            result_sender_socket.send_json({
                "conversion_id": conversion_id,
                "status": "failed",
                "error": error_msg
            })
            return
        
        if conversion_error[0]:
            raise conversion_error[0]
        
        if conversion_result[0] is None:
            raise ValueError("Conversion returned no result")
        
        result = conversion_result[0]
        markdown_content = result.document.export_to_markdown()
        
        # Clean markdown content to handle anomalies
        # This ensures all templates receive clean, compatible content
        markdown_content = _clean_markdown_content(markdown_content)

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
        # Use UTF-8 with error handling to prevent encoding issues
        with open(converted_file_path, "w", encoding='utf-8', errors='replace') as f:
            f.write(frontmatter.dumps(post))

        result_sender_socket.send_json(
            {"conversion_id": conversion_id, "status": "completed"}
        )
        logger.info(f"[{conversion_id}] Successfully converted to {converted_file_path}")

    except Exception as e:
        logger.error(f"[{conversion_id}] Conversion failed: {e}", exc_info=True)
        result_sender_socket.send_json(
            {"conversion_id": conversion_id, "status": "failed", "error": str(e)}
        )


def main():
    """
    Main worker loop to process tasks from the queue.
    
    Simple approach:
    - Use --host argument (or default to localhost)
    - Connect to ZMQ queues on that host
    - Process conversion tasks as they arrive
    """
    parser = argparse.ArgumentParser(description="ZeroMQ worker for file conversion.")
    parser.add_argument(
        "--host", 
        type=str, 
        default="localhost",
        help="The host of the ZeroMQ server (default: localhost, K8s: markdown-api)"
    )
    args = parser.parse_args()
    
    host = args.host
    logger.info(f"Starting worker connecting to ZMQ host: {host}")
    logger.info(f"PROJECTS_BASE_PATH: {os.getenv('PROJECTS_BASE_PATH', 'not set')}")
    logger.info(f"CONVERTED_FILES_DIR: {os.getenv('CONVERTED_FILES_DIR', './data/converted_files')}")
    logger.info(f"DOCLING_DO_OCR: {os.getenv('DOCLING_DO_OCR', 'false')} (OCR {'enabled' if os.getenv('DOCLING_DO_OCR', 'false').lower() in ('true', '1', 'yes') else 'disabled'})")

    context = zmq.Context()

    task_receiver_socket = context.socket(zmq.PULL)
    task_receiver_socket.connect(f"tcp://{host}:5555")

    result_sender_socket = context.socket(zmq.PUSH)
    result_sender_socket.connect(f"tcp://{host}:5556")

    logger.info("Successfully connected to ZeroMQ sockets. Ready to process conversions.")

    while True:
        message = task_receiver_socket.recv_string()
        task = json.loads(message)

        conversion_id = task["conversion_id"]
        file_path = task["file_path"]

        convert_file_to_markdown(file_path, conversion_id, result_sender_socket)


if __name__ == "__main__":
    logger.info(f"Worker with PID {os.getpid()} started.")
    main()
