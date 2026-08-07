"""Shared pytest fixtures and repository paths."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
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
    """Absolute path to the shipped ``config/`` directory.

    **Read-only.** It is the operator's live configuration on the rig and the
    repository's shipped defaults everywhere else. :func:`writable_config_dir`
    is for any test that needs to change something.
    """
    return CONFIG_DIR


@pytest.fixture
def writable_config_dir(tmp_path: Path) -> Path:
    """A throwaway copy of the shipped configuration.

    Use this, not :func:`config_dir`, whenever a test writes a file — including
    a ``*.local.yaml`` override, which is the easy one to get wrong because it
    is a file the shipped directory does not contain and so looks like it is
    nobody's.
    """
    directory = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, directory)
    return directory


def _config_fingerprint() -> dict[str, str]:
    return {
        str(path.relative_to(CONFIG_DIR)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(CONFIG_DIR.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


@pytest.fixture(scope="session", autouse=True)
def _the_suite_does_not_touch_the_real_configuration() -> Iterator[None]:
    """Fail the run if any test changed the repository's ``config/``.

    Not hypothetical. The ``config_dir`` fixture hands out the real directory,
    and a test written to exercise the ``*.local.yaml`` overrides created two of
    them there — which silently changed the shipped safety margin that a
    *different* test asserts, and would have left them on the operator's rig
    overriding their own meridian limits.

    Nothing in ``nocturne`` may write a configuration file
    (``test_safety_boundaries.py``); this is the same rule applied to the tests,
    which are the other thing that runs in that directory.
    """
    before = _config_fingerprint()
    assert before, f"no configuration files found under {CONFIG_DIR}"
    yield
    after = _config_fingerprint()

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    touched = added + removed + changed
    assert not touched, (
        f"the test suite modified {CONFIG_DIR}: "
        f"added={added} removed={removed} changed={changed}. "
        "Use the writable_config_dir fixture rather than config_dir."
    )
