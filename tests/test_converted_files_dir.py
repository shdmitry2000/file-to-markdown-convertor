"""Where converted files go — and, more to the point, where they must never go.

The default used to be a bare "/app/converted_files" under both docker and
kubernetes. That is outside every volume the pod mounts: the shared claim lands on
PROJECTS_BASE_PATH ("/app/projects" on both OCP and GKE), so "/app" is the image's
own root filesystem and no lane runs as root. The mkdir that followed lived in
WorkerSettings.__init__, so on-prem the process died at import with EACCES and no
indication of which variable would have fixed it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, WorkerSettings, get_converted_files_dir, get_settings


def test_derives_from_the_mounted_volume():
    s = WorkerSettings(ENVIRONMENT="kubernetes", PROJECTS_BASE_PATH="/app/projects")
    assert s.CONVERTED_FILES_DIR == "/app/projects/.cache/converted_files"


@pytest.mark.parametrize("environment", ["kubernetes", "docker"])
def test_never_falls_back_outside_every_mount(environment):
    """No volume to derive from must name the missing variables, not guess /app."""
    with pytest.raises(ConfigError) as exc:
        WorkerSettings(ENVIRONMENT=environment)
    message = str(exc.value)
    assert "PROJECTS_BASE_PATH" in message and "CONVERTED_FILES_DIR" in message
    assert "/app/converted_files" not in message.split("Refusing")[0]


def test_explicit_setting_wins(tmp_path):
    s = WorkerSettings(
        ENVIRONMENT="kubernetes",
        PROJECTS_BASE_PATH="/app/projects",
        CONVERTED_FILES_DIR=str(tmp_path / "elsewhere"),
    )
    assert s.CONVERTED_FILES_DIR == str(tmp_path / "elsewhere")


def test_standalone_keeps_the_in_checkout_default():
    s = WorkerSettings(ENVIRONMENT="standalone")
    assert s.CONVERTED_FILES_DIR.endswith("/data/converted_files")


def test_resolution_creates_nothing(tmp_path):
    """Construction must not touch the filesystem — that is what made an unwritable
    path an import-time crash instead of a reportable error."""
    target = tmp_path / "not-yet"
    WorkerSettings(ENVIRONMENT="kubernetes", PROJECTS_BASE_PATH=str(target))
    assert not target.exists()


def test_accessor_creates_on_demand(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVERTED_FILES_DIR", str(tmp_path / "made" / "here"))
    get_settings.cache_clear()
    try:
        created = get_converted_files_dir()
        assert created.is_dir()
    finally:
        get_settings.cache_clear()
