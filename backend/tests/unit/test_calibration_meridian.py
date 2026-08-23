"""Which stored calibration applies to the rig as it stands — ADR 0021.

The store keeps every calibration ever recorded. The governor takes at most one,
and only if the geometry it was measured against is the geometry in front of it
now. Everything here is about that "only if": each test is a way the rig can
differ from what was measured, and each expects nothing back rather than the
nearest thing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nocturne.calibration import resolve_measured_band
from nocturne.schemas import EquipmentConfig, SafetyConfig, load_config_bundle
from nocturne.store import (
    CalibrationStore,
    MeridianMark,
    RecordedCalibration,
    default_database_path,
)
from tests.fixtures.observing import calibrated_safety_config

NOW = datetime(2026, 8, 23, 11, 0, tzinfo=UTC)


def marks() -> tuple[MeridianMark, ...]:
    return (
        MeridianMark("east", -25.0, 20.0, 68.0, "west", NOW),
        MeridianMark("west", 20.0, 20.0, 66.0, "east", NOW),
    )


@pytest.fixture
def equipment(config_dir: Path) -> EquipmentConfig:
    return load_config_bundle(config_dir).equipment


@pytest.fixture
def safety(config_dir: Path) -> SafetyConfig:
    return load_config_bundle(config_dir).safety


@pytest.fixture
def store(tmp_path: Path) -> CalibrationStore:
    return CalibrationStore(tmp_path / "nocturne.db")


def record_at(store: CalibrationStore, equipment: EquipmentConfig, latitude: float) -> None:
    store.record(
        RecordedCalibration(
            recorded_at=NOW,
            latitude_deg=latitude,
            longitude_deg=equipment.site.longitude,
            counterweight_fitted=equipment.mount.counterweight_fitted,
            optical_train_id=equipment.active_optical_train.id,
            marks=marks(),
        )
    )


class TestResolving:
    def test_an_empty_store_resolves_to_nothing(
        self, store: CalibrationStore, equipment: EquipmentConfig, safety: SafetyConfig
    ) -> None:
        assert resolve_measured_band(store, equipment, safety) is None

    def test_a_calibration_at_this_site_resolves(
        self, store: CalibrationStore, equipment: EquipmentConfig, safety: SafetyConfig
    ) -> None:
        record_at(store, equipment, equipment.site.latitude)
        band = resolve_measured_band(store, equipment, safety)
        assert band is not None
        assert (band.east_deg, band.west_deg) == (-25.0, 20.0)

    def test_the_band_says_where_it_came_from(
        self, store: CalibrationStore, equipment: EquipmentConfig, safety: SafetyConfig
    ) -> None:
        """ "No pointing between -30 and +25" is half an answer without "says who"."""
        record_at(store, equipment, equipment.site.latitude)
        band = resolve_measured_band(store, equipment, safety)
        assert band is not None
        assert "2026-08-23" in band.source
        assert equipment.active_optical_train.id in band.source

    def test_a_calibration_from_another_latitude_does_not_resolve(
        self, store: CalibrationStore, equipment: EquipmentConfig, safety: SafetyConfig
    ) -> None:
        """The polar axis is tilted to the latitude; so is the arc past the legs."""
        tolerance = safety.limits.meridian.latitude_tolerance_deg
        record_at(store, equipment, equipment.site.latitude + tolerance * 4.0)
        assert resolve_measured_band(store, equipment, safety) is None

    def test_the_tolerance_comes_from_the_configuration(
        self, store: CalibrationStore, equipment: EquipmentConfig, config_dir: Path
    ) -> None:
        """Not a constant in the resolver: an operator may widen or tighten it."""
        tolerance = 0.5
        record_at(store, equipment, equipment.site.latitude + tolerance / 2.0)
        safety = calibrated_safety_config(config_dir)
        assert safety.limits.meridian.latitude_tolerance_deg == tolerance
        assert resolve_measured_band(store, equipment, safety) is not None

    def test_a_calibration_for_another_optical_train_does_not_resolve(
        self, store: CalibrationStore, equipment: EquipmentConfig, safety: SafetyConfig
    ) -> None:
        store.record(
            RecordedCalibration(
                recorded_at=NOW,
                latitude_deg=equipment.site.latitude,
                longitude_deg=equipment.site.longitude,
                counterweight_fitted=equipment.mount.counterweight_fitted,
                optical_train_id="something-else",
                marks=marks(),
            )
        )
        assert resolve_measured_band(store, equipment, safety) is None


class TestTheDefaultLocation:
    """monkeypatch, not os.environ: the session guard points the whole suite here."""

    def test_the_environment_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOCTURNE_DATA_DIR", str(tmp_path))
        assert default_database_path().parent == tmp_path

    def test_it_is_not_inside_the_repository(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """State is not configuration and does not live beside it."""
        monkeypatch.delenv("NOCTURNE_DATA_DIR", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert repo_root not in default_database_path().parents
