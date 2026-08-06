"""Configuration loading — CLAUDE.md section 6: fail loudly at startup.

An invalid configuration file must stop the process with a message that names
the file, the offending field and the reason. It must never be papered over
with a default value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nocturne.schemas import (
    ConfigError,
    NocturneConfig,
    load_agent_config,
    load_config_bundle,
    load_equipment_config,
    load_safety_config,
)


class TestBundle:
    def test_bundle_loads_all_three_files(self, config_dir: Path) -> None:
        config = load_config_bundle(config_dir)
        assert isinstance(config, NocturneConfig)
        assert config.equipment.site.is_placeholder  # the shipped reference site
        assert config.safety.limits.meridian.calibrated is False
        assert config.agent.autonomy_level == "advisory"

    def test_bundle_is_immutable(self, config_dir: Path) -> None:
        config = load_config_bundle(config_dir)
        with pytest.raises(ValidationError, match="frozen"):
            config.agent = config.agent  # type: ignore[misc]

    def test_missing_directory_names_the_directory(self, tmp_path: Path) -> None:
        missing = tmp_path / "nowhere"
        with pytest.raises(ConfigError) as excinfo:
            load_config_bundle(missing)
        assert str(missing) in str(excinfo.value)


class TestLoudFailure:
    def test_missing_file_names_the_path(self, tmp_path: Path) -> None:
        path = tmp_path / "equipment.yaml"
        with pytest.raises(ConfigError) as excinfo:
            load_equipment_config(path)
        message = str(excinfo.value)
        assert str(path) in message
        assert "not found" in message.lower()

    def test_malformed_yaml_names_the_path_and_the_cause(self, tmp_path: Path) -> None:
        path = tmp_path / "safety.yaml"
        path.write_text("limits:\n  altitude_min_deg: [unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            load_safety_config(path)
        message = str(excinfo.value)
        assert str(path) in message
        assert "yaml" in message.lower()

    def test_empty_file_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "agent.yaml"
        path.write_text("", encoding="utf-8")
        with pytest.raises(ConfigError, match="empty"):
            load_agent_config(path)

    def test_non_mapping_document_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "agent.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="mapping"):
            load_agent_config(path)

    def test_validation_error_names_field_and_reason(
        self, tmp_path: Path, config_dir: Path
    ) -> None:
        source = (config_dir / "safety.yaml").read_text(encoding="utf-8")
        path = tmp_path / "safety.yaml"
        path.write_text(
            source.replace("altitude_min_deg: 25", "altitude_min_deg: -40"), "utf-8"
        )
        with pytest.raises(ConfigError) as excinfo:
            load_safety_config(path)
        message = str(excinfo.value)
        assert str(path) in message
        assert "altitude_min_deg" in message
        assert "invalid" in message.lower()

    def test_config_error_is_not_silently_recoverable(self, tmp_path: Path) -> None:
        """A ConfigError is fatal by contract: it is not an OSError or a warning."""
        assert issubclass(ConfigError, Exception)
        assert not issubclass(ConfigError, Warning)

    def test_directory_instead_of_file_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not a file"):
            load_equipment_config(tmp_path)
