"""Configuration loading — SPEC section 5, CLAUDE.md section 6.

"Fail loudly at startup on invalid config." Every failure mode of loading a
configuration file — missing, unreadable, malformed, or semantically invalid —
raises :class:`ConfigError` with a message naming the file and the reason. No
loader in this module falls back to a default value, and none returns a
partially populated model.

Each shipped file may be layered under an untracked local override —
``equipment.local.yaml`` beside ``equipment.yaml`` — so that the file an
operator has to edit for their own site is not the file git manages. See
:mod:`nocturne.schemas.layering` for why, and for the merge rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, final

import yaml
from pydantic import BaseModel, ValidationError

from .agent import AgentConfig
from .common import StrictModel
from .equipment import EquipmentConfig
from .layering import (
    LOCAL_SUFFIX,
    MergedDocument,
    Override,
    local_path_for,
    merge_documents,
    source_for_error,
)
from .safety import SafetyConfig

EQUIPMENT_FILENAME = "equipment.yaml"
SAFETY_FILENAME = "safety.yaml"
AGENT_FILENAME = "agent.yaml"

#: The three shipped files, in the order the bundle loads them.
CONFIG_FILENAMES = (EQUIPMENT_FILENAME, SAFETY_FILENAME, AGENT_FILENAME)

ModelT = TypeVar("ModelT", bound=BaseModel)


class ConfigError(Exception):
    """A configuration file is missing, malformed or invalid.

    Fatal by contract: nothing in Nocturne catches this to continue with
    defaults. It exists to stop the process before anything moves.
    """


@final
@dataclass(frozen=True, slots=True)
class FileSources:
    """Which files a single configuration model was assembled from."""

    shipped: Path
    #: The override that was found and applied, or ``None`` if there is none.
    local: Path | None
    overrides: tuple[Override, ...]

    @property
    def is_layered(self) -> bool:
        """Whether an override file contributed anything."""
        return self.local is not None


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


def _read_local(path: Path) -> dict[str, object] | None:
    """Read the override beside ``path``, or ``None`` if there is none.

    An override that exists must be valid. Missing is fine — that is the normal
    state of a fresh clone and of CI — but present-and-broken is an error, not a
    reason to quietly fall back to the shipped values. Falling back is precisely
    how an operator ends up observing from the placeholder site while believing
    they fixed it.
    """
    override = local_path_for(path)
    if not override.exists():
        return None
    return _read_mapping(override)


def _format_validation_error(path: Path, exc: ValidationError, sources: dict[str, Path]) -> str:
    """Report the errors, naming the file that supplied each bad value.

    Validation runs on the merged document, so without this every problem is
    reported against the shipped file — sending the operator to edit the one
    file that is not at fault, and that they must not edit.
    """
    blamed = [source_for_error(sources, error["loc"]) for error in exc.errors()]
    culprits = {found for found in blamed if found is not None and found != path}

    header = f"{path}: invalid configuration ({exc.error_count()} problem(s)):"
    if culprits and all(found is not None and found != path for found in blamed):
        named = ", ".join(str(found) for found in sorted(culprits))
        header = (
            f"{path}: invalid configuration ({exc.error_count()} problem(s)), "
            f"and every one of them comes from {named}:"
        )

    lines = [header]
    for error, found in zip(exc.errors(), blamed, strict=True):
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        origin = f"  [{found}]" if found is not None and found != path else ""
        lines.append(f"  - {location}: {error['msg']}{origin}")
    return "\n".join(lines)


def load_layered(model_type: type[ModelT], path: Path) -> tuple[ModelT, FileSources]:
    """Load ``path``, merge its local override over it, and validate the result.

    Returns the model and a record of which files contributed.

    Raises:
        ConfigError: on any failure whatsoever, naming the file responsible —
            which for a value the operator set is their own override rather
            than the shipped file.
    """
    base = _read_mapping(path)
    override = local_path_for(path)
    local = _read_local(path)
    merged: MergedDocument = merge_documents(
        base, path, local, override if local is not None else None
    )

    try:
        model = model_type.model_validate(merged.document)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc, merged.sources)) from exc

    return model, FileSources(
        shipped=path,
        local=override if local is not None else None,
        overrides=merged.overrides,
    )


def load_model(model_type: type[ModelT], path: Path) -> ModelT:
    """Load and validate ``path``, applying its local override if there is one.

    Raises:
        ConfigError: on any failure whatsoever.
    """
    model, _ = load_layered(model_type, path)
    return model


def load_equipment_config(path: Path) -> EquipmentConfig:
    """Load ``equipment.yaml`` — SPEC section 5.1."""
    return load_model(EquipmentConfig, path)


def load_safety_config(path: Path) -> SafetyConfig:
    """Load ``safety.yaml`` — SPEC section 5.2."""
    return load_model(SafetyConfig, path)


def load_agent_config(path: Path) -> AgentConfig:
    """Load ``agent.yaml`` — SPEC section 5.3."""
    return load_model(AgentConfig, path)


class ConfigSources(StrictModel):
    """Where each of the three configurations came from.

    Reported by ``check-config``. A configuration assembled from two files is
    one where "I changed that" and "the change took effect" are different
    statements, and the only way to tell them apart is to be told.
    """

    model_config = StrictModel.model_config | {"arbitrary_types_allowed": True}

    equipment: FileSources
    safety: FileSources
    agent: FileSources

    def all_sources(self) -> tuple[FileSources, ...]:
        return (self.equipment, self.safety, self.agent)


class NocturneConfig(StrictModel):
    """The three configuration files, loaded and validated together."""

    model_config = StrictModel.model_config | {"arbitrary_types_allowed": True}

    equipment: EquipmentConfig
    safety: SafetyConfig
    agent: AgentConfig
    sources: ConfigSources


def load_config_bundle(directory: Path) -> NocturneConfig:
    """Load ``equipment.yaml``, ``safety.yaml`` and ``agent.yaml`` from ``directory``.

    Each is layered under its ``*.local.yaml`` override if one exists.

    This is the startup entry point. It either returns a fully valid,
    immutable configuration or raises :class:`ConfigError`.
    """
    if not directory.is_dir():
        raise ConfigError(f"Configuration directory not found: {directory}")

    equipment, equipment_sources = load_layered(EquipmentConfig, directory / EQUIPMENT_FILENAME)
    safety, safety_sources = load_layered(SafetyConfig, directory / SAFETY_FILENAME)
    agent, agent_sources = load_layered(AgentConfig, directory / AGENT_FILENAME)

    return NocturneConfig(
        equipment=equipment,
        safety=safety,
        agent=agent,
        sources=ConfigSources(
            equipment=equipment_sources, safety=safety_sources, agent=agent_sources
        ),
    )


__all__ = [
    "AGENT_FILENAME",
    "CONFIG_FILENAMES",
    "EQUIPMENT_FILENAME",
    "LOCAL_SUFFIX",
    "SAFETY_FILENAME",
    "ConfigError",
    "ConfigSources",
    "FileSources",
    "NocturneConfig",
    "load_agent_config",
    "load_config_bundle",
    "load_equipment_config",
    "load_layered",
    "load_model",
    "load_safety_config",
]
