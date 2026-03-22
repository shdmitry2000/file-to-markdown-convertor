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
    ZEROMQ_HOST: str | None = None
    """ZeroMQ host. Auto-configured based on environment if not set."""
    
    ZEROMQ_TASK_PORT: int = 5585
    """ZeroMQ port for task queue (PUSH/PULL)"""
    
    ZEROMQ_RESULT_PORT: int = 5586
    """ZeroMQ port for result queue (PUSH/PULL)"""
    
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
        if self.ZEROMQ_HOST is None:
            if self.ENVIRONMENT in ['kubernetes', 'docker']:
                # Docker/K8s: Connect to API service by hostname
                self.ZEROMQ_HOST = "api"
            else:
                # Standalone: Connect to localhost
                self.ZEROMQ_HOST = "localhost"
    
    @property
    def zeromq_task_url(self) -> str:
        """Full ZeroMQ URL for task queue."""
        return f"tcp://{self.ZEROMQ_HOST}:{self.ZEROMQ_TASK_PORT}"
    
    @property
    def zeromq_result_url(self) -> str:
        """Full ZeroMQ URL for result queue."""
        return f"tcp://{self.ZEROMQ_HOST}:{self.ZEROMQ_RESULT_PORT}"


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
