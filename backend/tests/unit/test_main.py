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
        assert "Site:" in capsys.readouterr().out

    def test_report_states_that_the_mount_is_uncalibrated(
        self, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The operator must not have to go looking for this (SPEC section 9.1)."""
        main(["check-config", "--config-dir", str(config_dir)])
        out = capsys.readouterr().out
        assert "meridian" in out.lower()
        assert "not calibrated" in out.lower()

    def test_report_names_the_storage_and_its_free_space(
        self, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """docs/FIELD-NOTES-M1.md section 6: say what we are running on."""
        main(["check-config", "--config-dir", str(config_dir)])
        out = capsys.readouterr().out
        assert "Storage:" in out
        assert "GB free" in out

    def test_removable_flash_is_called_out_not_merely_reported(
        self,
        config_dir: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A microSD is the reference rig's current state; it must not read as fine."""
        from nocturne import main as main_module
        from nocturne.storage import DeviceKind, StorageReport

        monkeypatch.setattr(
            main_module,
            "inspect_storage",
            lambda path: StorageReport(
                path=path,
                device="mmcblk0",
                kind=DeviceKind.SD_CARD,
                removable=True,
                rotational=False,
                free_gb=19.4,
                total_gb=29.0,
            ),
        )
        assert main(["check-config", "--config-dir", str(config_dir)]) == 0
        out = capsys.readouterr().out
        assert "mmcblk0" in out
        assert "stacking" in out.lower()
        assert "refused" in out.lower()

    def test_a_storage_that_cannot_be_read_does_not_stop_the_check(
        self,
        config_dir: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The configuration is what this command validates. Storage is a bonus."""
        from nocturne import main as main_module

        def explode(_path: Path) -> None:
            raise OSError("no such thing")

        monkeypatch.setattr(main_module, "inspect_storage", explode)
        assert main(["check-config", "--config-dir", str(config_dir)]) == 0
        assert "could not be inspected" in capsys.readouterr().out

    def test_the_placeholder_site_is_called_out_loudly(
        self, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The shipped config has one. Nothing about it fails on its own."""
        assert main(["check-config", "--config-dir", str(config_dir)]) == 0
        out = capsys.readouterr().out
        assert "STILL THE SHIPPED PLACEHOLDER" in out
        # It must say what goes wrong, not just that something is unset.
        assert "meridian" in out.lower()
        assert "equipment.yaml" in out

    def test_a_real_site_produces_no_placeholder_warning(
        self, tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for name in ("equipment.yaml", "safety.yaml", "agent.yaml"):
            (tmp_path / name).write_text(
                (config_dir / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
        equipment = (tmp_path / "equipment.yaml").read_text(encoding="utf-8")
        (tmp_path / "equipment.yaml").write_text(
            equipment.replace("latitude: 45.0", "latitude: 51.4778").replace(
                "longitude: 0.0", "longitude: -0.0015"
            ),
            encoding="utf-8",
        )

        assert main(["check-config", "--config-dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "STILL THE SHIPPED PLACEHOLDER" not in out
        assert "51.4778" in out

    def test_report_names_the_mount_port_and_whether_it_is_there(
        self, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["check-config", "--config-dir", str(config_dir)])
        out = capsys.readouterr().out
        assert "Mount port:" in out
        assert "driver" in out  # the shipped config sets none

    def test_a_configured_port_that_is_absent_fails_the_check(
        self, tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A cable in the wrong socket must not read as a healthy startup."""
        for name in ("equipment.yaml", "safety.yaml", "agent.yaml"):
            (tmp_path / name).write_text(
                (config_dir / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
        equipment = (tmp_path / "equipment.yaml").read_text(encoding="utf-8")
        (tmp_path / "equipment.yaml").write_text(
            equipment.replace("  port: null", '  port: "/dev/ttyACM47"'), encoding="utf-8"
        )

        assert main(["check-config", "--config-dir", str(tmp_path)]) != 0
        err = capsys.readouterr().err
        assert "/dev/ttyACM47" in err
        assert "dialout" in err

    def test_an_unset_port_never_fails_the_check(self, config_dir: Path) -> None:
        """Nothing was claimed, so nothing can be missing."""
        assert main(["check-config", "--config-dir", str(config_dir)]) == 0

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
