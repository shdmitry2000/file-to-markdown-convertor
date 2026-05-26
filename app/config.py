"""
Configuration management for file-to-markdown-convertor service.

Handles environment detection and path configuration for:
- Standalone mode (local development)
- Docker container mode
- Kubernetes deployment

Configuration priority:
1. Environment variables (highest)
2. .env file
3. Default values (lowest)
"""

import os
from pathlib import Path
from functools import lru_cache
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def detect_environment() -> str:
    """
    Detect the runtime environment.
    
    Returns:
        str: 'kubernetes', 'docker', or 'standalone'
    """
    if os.environ.get('KUBERNETES_SERVICE_HOST'):
        return 'kubernetes'
    elif os.path.exists('/.dockerenv') or os.environ.get('DOCKER_CONTAINER', '').lower() == 'true':
        return 'docker'
    else:
        return 'standalone'


class WorkerSettings(BaseSettings):
    """Worker service configuration with environment-aware defaults."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )
    
    # ── Environment Detection ──────────────────────────────────
    ENVIRONMENT: str = detect_environment()
    """Detected environment: 'kubernetes', 'docker', or 'standalone'"""
    
    # ── File Paths ─────────────────────────────────────────────
    CONVERTED_FILES_DIR: str | None = None
    """Directory for converted markdown files. Auto-configured if not set."""
    
    PROJECTS_BASE_PATH: str | None = None
    """Base path for project files (used in Docker/K8s for shared volumes)"""
    
    # ── ZeroMQ Configuration ───────────────────────────────────
    ZEROMQ_HOST: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ZEROMQ_HOST", "ZMQ_HOST"),
    )
    """Peer hostname/IP for task + result sockets (worker connects here).

    Helm charts often set ``ZMQ_HOST``; standalone docs use ``ZEROMQ_HOST``.
    For Kubernetes the robust setup is **worker sidecar + ``127.0.0.1``**
    (see README — avoids kube-proxy / multi-replica split brain).
    """
    
    ZMQ_TASK_PORT: int = 5555
    """ZeroMQ port for task queue (PUSH/PULL). Configurable via env var."""
    
    ZMQ_RESULT_PORT: int = 5556
    """ZeroMQ port for result queue (PUSH/PULL). Configurable via env var."""

    ZMQ_CHUNK_PORT: int = 5557
    """ZeroMQ port for the chunking ROUTER service (REQ/REP). External
    clients (e.g. v2's DoclingHybridChunker plugin) open a REQ socket and
    talk directly to chunk_server. Symmetric topology — no ingress proxy
    or reply_to hack needed. Conversion still uses 5555/5556 PUSH/PULL.
    """
    
    # ── Logging ────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    """Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL"""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._configure_paths()
        self._configure_zeromq()
    
    def _configure_paths(self):
        """Auto-configure file paths based on environment if not explicitly set."""
        if self.CONVERTED_FILES_DIR is None:
            if self.ENVIRONMENT == 'kubernetes':
                # K8s: Use shared persistent volume
                self.CONVERTED_FILES_DIR = "/app/converted_files"
            elif self.ENVIRONMENT == 'docker':
                # Docker: Use volume mount or default container path
                self.CONVERTED_FILES_DIR = "/app/converted_files"
            else:
                # Standalone: Use local data directory relative to project root
                project_root = Path(__file__).parent.parent
                self.CONVERTED_FILES_DIR = str(project_root / "data" / "converted_files")
        
        # Ensure directory exists
        Path(self.CONVERTED_FILES_DIR).mkdir(parents=True, exist_ok=True)
    
    def _configure_zeromq(self):
        """Auto-configure ZeroMQ host based on environment if not explicitly set."""
        peer_override = os.environ.get("MARKDOWN_ZMQ_PEER_HOST")
        if peer_override:
            self.ZEROMQ_HOST = peer_override.strip()
            return

        if self.ZEROMQ_HOST is not None:
            return

        if self.ENVIRONMENT == "kubernetes":
            # Compose historically used service name `api`; cluster DNS uses `markdown-api`.
            self.ZEROMQ_HOST = "markdown-api"
        elif self.ENVIRONMENT == "docker":
            self.ZEROMQ_HOST = "api"
        else:
            self.ZEROMQ_HOST = "localhost"
    
    @property
    def zeromq_task_url(self) -> str:
        """Full ZeroMQ URL for task queue."""
        return f"tcp://{self.ZEROMQ_HOST}:{self.ZMQ_TASK_PORT}"
    
    @property
    def zeromq_chunk_url(self) -> str:
        """Full ZeroMQ URL for the chunking ROUTER service."""
        return f"tcp://{self.ZEROMQ_HOST}:{self.ZMQ_CHUNK_PORT}"

    @property
    def zeromq_result_url(self) -> str:
        """Full ZeroMQ URL for result queue."""
        return f"tcp://{self.ZEROMQ_HOST}:{self.ZMQ_RESULT_PORT}"


@lru_cache
def get_settings() -> WorkerSettings:
    """
    Get cached settings instance.
    
    Returns:
        WorkerSettings: Singleton configuration instance
    """
    return WorkerSettings()


# Convenience function for getting converted files directory
def get_converted_files_dir() -> Path:
    """
    Get the converted files directory as a Path object.
    
    Returns:
        Path: Directory where converted files are stored
    """
    return Path(get_settings().CONVERTED_FILES_DIR)
