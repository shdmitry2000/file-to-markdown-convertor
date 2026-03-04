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


# In-memory database to store conversion status (this should be a shared resource in a real application)


def convert_file_to_markdown(file_path: str, conversion_id: str, result_sender_socket):
    """
    Converts a file to markdown and sends status updates back to the API.
    """
    logger.info(f"Received task to convert {file_path} with ID {conversion_id}")
    result_sender_socket.send_json(
        {"conversion_id": conversion_id, "status": "processing"}
    )

    try:
        # Write to converted_files/<stem>.md so API can find via GET /converted/<original_name>
        os.makedirs("converted_files", exist_ok=True)
        stem = Path(file_path).stem
        converted_file_path = os.path.join("converted_files", f"{stem}.md")

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
        logger.info(f"Successfully converted {file_path} to {converted_file_path}")

    except Exception as e:
        logger.error(f"Failed to convert {file_path}: {e}", exc_info=True)
        result_sender_socket.send_json(
            {"conversion_id": conversion_id, "status": "failed"}
        )


def main():
    """
    Main worker loop to process tasks from the queue.
    """
    parser = argparse.ArgumentParser(description="ZeroMQ worker for file conversion.")
    parser.add_argument(
        "--host", type=str, default=None, help="The host of the ZeroMQ server."
    )
    args = parser.parse_args()
    
    # Auto-detect environment: use 'api' for Docker, 'localhost' for standalone
    if args.host:
        host = args.host
    else:
        # Check if running in Docker by looking for /.dockerenv or checking hostname
        is_docker = os.path.exists('/.dockerenv') or os.environ.get('DOCKER_CONTAINER', '').lower() == 'true'
        host = "api" if is_docker else "localhost"
    
    logger.info(f"Connecting to ZeroMQ host: {host} (Docker mode: {host == 'api'})")

    context = zmq.Context()

    task_receiver_socket = context.socket(zmq.PULL)
    task_receiver_socket.connect(f"tcp://{host}:5555")

    result_sender_socket = context.socket(zmq.PUSH)
    result_sender_socket.connect(f"tcp://{host}:5556")

    logger.info("Successfully connected to ZeroMQ sockets.")

    while True:
        message = task_receiver_socket.recv_string()
        task = json.loads(message)

        conversion_id = task["conversion_id"]
        file_path = task["file_path"]

        convert_file_to_markdown(file_path, conversion_id, result_sender_socket)


if __name__ == "__main__":
    # In a real application, you'd run multiple instances of this worker.
    # For this example, we'll just run one.
    logger.info(f"Worker with PID {os.getpid()} started.")
    main()
