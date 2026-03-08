import zmq
import json
import os
import time
from pathlib import Path
from docling.document_converter import DocumentConverter
import frontmatter
from datetime import datetime
import logging
import argparse

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


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

        # Convert the file
        converter = DocumentConverter()
        result = converter.convert(file_path)
        markdown_content = result.document.export_to_markdown()

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
        with open(converted_file_path, "w") as f:
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
