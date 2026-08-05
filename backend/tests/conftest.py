"""Shared pytest fixtures and repository paths."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_dir() -> Path:
    """Absolute path to the shipped ``config/`` directory."""
    return CONFIG_DIR
