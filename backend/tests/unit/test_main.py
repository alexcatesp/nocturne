"""Startup entry point — CLAUDE.md section 6: fail loudly at startup."""

from __future__ import annotations

from pathlib import Path

import pytest

from nocturne.main import main


class TestCheckConfig:
    def test_valid_config_exits_zero(
        self, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["check-config", "--config-dir", str(config_dir)]) == 0
        assert "Tudela de Duero" in capsys.readouterr().out

    def test_report_states_that_the_mount_is_uncalibrated(
        self, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The operator must not have to go looking for this (SPEC section 9.1)."""
        main(["check-config", "--config-dir", str(config_dir)])
        out = capsys.readouterr().out
        assert "meridian" in out.lower()
        assert "not calibrated" in out.lower()

    def test_invalid_config_exits_non_zero_and_explains(
        self, tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for name in ("equipment.yaml", "safety.yaml", "agent.yaml"):
            (tmp_path / name).write_text(
                (config_dir / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
        broken = (tmp_path / "safety.yaml").read_text(encoding="utf-8")
        (tmp_path / "safety.yaml").write_text(
            broken.replace("altitude_min_deg: 25", "altitude_min_deg: 500"), encoding="utf-8"
        )
        assert main(["check-config", "--config-dir", str(tmp_path)]) != 0
        assert "altitude_min_deg" in capsys.readouterr().err

    def test_missing_config_directory_exits_non_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["check-config", "--config-dir", str(tmp_path / "absent")]) != 0
        assert "absent" in capsys.readouterr().err
