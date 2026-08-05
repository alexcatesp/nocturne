"""Configuration loading — SPEC section 5, CLAUDE.md section 6.

"Fail loudly at startup on invalid config." Every failure mode of loading a
configuration file — missing, unreadable, malformed, or semantically invalid —
raises :class:`ConfigError` with a message naming the file and the reason. No
loader in this module falls back to a default value, and none returns a
partially populated model.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from .agent import AgentConfig
from .common import StrictModel
from .equipment import EquipmentConfig
from .safety import SafetyConfig

EQUIPMENT_FILENAME = "equipment.yaml"
SAFETY_FILENAME = "safety.yaml"
AGENT_FILENAME = "agent.yaml"

ModelT = TypeVar("ModelT", bound=BaseModel)


class ConfigError(Exception):
    """A configuration file is missing, malformed or invalid.

    Fatal by contract: nothing in Nocturne catches this to continue with
    defaults. It exists to stop the process before anything moves.
    """


def _read_mapping(path: Path) -> dict[str, object]:
    """Read ``path`` as a YAML mapping, or raise :class:`ConfigError`."""
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")
    if not path.is_file():
        raise ConfigError(f"Configuration path is not a file: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    if document is None:
        raise ConfigError(f"{path}: configuration file is empty")
    if not isinstance(document, dict):
        raise ConfigError(
            f"{path}: the top-level document must be a mapping, got {type(document).__name__}"
        )
    return document


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: invalid configuration ({exc.error_count()} problem(s)):"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def load_model(model_type: type[ModelT], path: Path) -> ModelT:
    """Load and validate ``path`` into ``model_type``.

    Raises:
        ConfigError: on any failure whatsoever.
    """
    document = _read_mapping(path)
    try:
        return model_type.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def load_equipment_config(path: Path) -> EquipmentConfig:
    """Load ``equipment.yaml`` — SPEC section 5.1."""
    return load_model(EquipmentConfig, path)


def load_safety_config(path: Path) -> SafetyConfig:
    """Load ``safety.yaml`` — SPEC section 5.2."""
    return load_model(SafetyConfig, path)


def load_agent_config(path: Path) -> AgentConfig:
    """Load ``agent.yaml`` — SPEC section 5.3."""
    return load_model(AgentConfig, path)


class NocturneConfig(StrictModel):
    """The three configuration files, loaded and validated together."""

    equipment: EquipmentConfig
    safety: SafetyConfig
    agent: AgentConfig


def load_config_bundle(directory: Path) -> NocturneConfig:
    """Load ``equipment.yaml``, ``safety.yaml`` and ``agent.yaml`` from ``directory``.

    This is the startup entry point. It either returns a fully valid,
    immutable configuration or raises :class:`ConfigError`.
    """
    if not directory.is_dir():
        raise ConfigError(f"Configuration directory not found: {directory}")
    return NocturneConfig(
        equipment=load_equipment_config(directory / EQUIPMENT_FILENAME),
        safety=load_safety_config(directory / SAFETY_FILENAME),
        agent=load_agent_config(directory / AGENT_FILENAME),
    )
