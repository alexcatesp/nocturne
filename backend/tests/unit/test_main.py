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


class TestTheMeridianReport:
    """What the governor will refuse, and on whose authority — ADR 0021."""

    def record_a_calibration(self, config_dir: Path) -> None:
        from datetime import UTC, datetime

        from nocturne.schemas import load_config_bundle
        from nocturne.store import (
            CalibrationStore,
            MeridianMark,
            RecordedCalibration,
            default_database_path,
        )

        equipment = load_config_bundle(config_dir).equipment
        when = datetime(2026, 8, 23, 11, 0, tzinfo=UTC)
        CalibrationStore(default_database_path()).record(
            RecordedCalibration(
                recorded_at=when,
                latitude_deg=equipment.site.latitude,
                longitude_deg=equipment.site.longitude,
                counterweight_fitted=equipment.mount.counterweight_fitted,
                optical_train_id=equipment.active_optical_train.id,
                marks=(
                    MeridianMark("east", -25.0, 20.0, 68.0, "west", when),
                    MeridianMark("west", 20.0, 20.0, 66.0, "east", when),
                ),
            )
        )

    def test_checking_the_configuration_does_not_create_the_database(
        self, config_dir: Path
    ) -> None:
        """A command run to look at things must not initialise anything."""
        from nocturne.store import default_database_path

        assert not default_database_path().exists()
        main(["check-config", "--config-dir", str(config_dir)])
        assert not default_database_path().exists()

    def test_a_stored_calibration_is_reported_with_its_band_and_its_source(
        self, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.record_a_calibration(config_dir)
        try:
            main(["check-config", "--config-dir", str(config_dir)])
            out = capsys.readouterr().out
            # -25 and +20 measured, widened by the shipped 5 degrees of margin.
            assert "no pointing between -30.0 and +25.0" in out
            assert "measured 2026-08-23" in out
        finally:
            from nocturne.store import default_database_path

            default_database_path().unlink()

    def test_without_one_the_report_says_nothing_will_point(
        self, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["check-config", "--config-dir", str(config_dir)])
        out = capsys.readouterr().out
        assert "NOT CALIBRATED" in out
        assert "in any mode" in out
