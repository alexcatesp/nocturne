"""Safety configuration schema — SPEC sections 5.2 and 9.

Values in ``config/safety.yaml`` are enforced, not advisory. The schema is the
first line of defence: a safety file that does not make sense must never load.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from nocturne.schemas import SafetyConfig, load_safety_config


@pytest.fixture
def raw(config_dir: Path) -> dict[str, Any]:
    """The shipped safety.yaml parsed to a plain dict, safe to mutate."""
    import yaml

    with (config_dir / "safety.yaml").open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return copy.deepcopy(loaded)


@pytest.fixture
def calibrated(raw: dict[str, Any]) -> dict[str, Any]:
    """A safety config with the meridian calibration filled in plausibly."""
    raw["limits"]["meridian"].update(
        {
            "calibrated": True,
            "calibration_date": "2026-08-01",
            "hour_angle_east_limit_deg": -25.0,
            "hour_angle_west_limit_deg": 20.0,
        }
    )
    return raw


class TestShippedFile:
    def test_shipped_safety_config_validates(self, config_dir: Path) -> None:
        config = load_safety_config(config_dir / "safety.yaml")
        assert isinstance(config, SafetyConfig)

    def test_ships_uncalibrated(self, config_dir: Path) -> None:
        """SPEC section 9.1.1 — non-negotiable shipping default."""
        meridian = load_safety_config(config_dir / "safety.yaml").limits.meridian
        assert meridian.calibrated is False
        assert meridian.calibration_date is None
        assert meridian.hour_angle_east_limit_deg is None
        assert meridian.hour_angle_west_limit_deg is None

    def test_limits_match_spec_section_5_2(self, config_dir: Path) -> None:
        limits = load_safety_config(config_dir / "safety.yaml").limits
        assert limits.altitude_min_deg == pytest.approx(25.0)
        assert limits.altitude_max_deg == pytest.approx(88.0)
        assert limits.sun_avoidance_deg == pytest.approx(30.0)
        assert limits.sun_altitude_max_deg == pytest.approx(-12.0)
        assert limits.meridian.safety_margin_deg == pytest.approx(5.0)
        assert limits.meridian.flip_strategy == "flip"
        assert limits.meridian.require_solve_after_flip is True
        assert limits.cooling.max_delta_from_ambient_c == pytest.approx(35.0)

    def test_abort_conditions_match_spec_section_5_2(self, config_dir: Path) -> None:
        aborts = load_safety_config(config_dir / "safety.yaml").abort_conditions
        assert aborts.guiding_lost_s == 180
        assert aborts.plate_solve_failures_consecutive == 3
        assert aborts.cloud_star_count_drop_percent == pytest.approx(60.0)
        assert aborts.cloud_confirm_frames == 2
        assert aborts.mount_communication_loss_s == 30
        assert aborts.disk_free_min_gb == pytest.approx(20.0)

    def test_agent_budget_matches_spec_section_5_2(self, config_dir: Path) -> None:
        agent = load_safety_config(config_dir / "safety.yaml").agent
        assert agent.max_tokens_per_session == 400_000
        assert agent.max_calls_per_hour == 60
        assert agent.on_budget_exhausted == "autonomous"
        assert agent.on_api_unreachable == "autonomous"


class TestStrictness:
    def test_unknown_key_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["limits"]["ignore_everything"] = True
        with pytest.raises(ValidationError, match="ignore_everything"):
            SafetyConfig.model_validate(raw)

    def test_missing_limits_section_is_rejected(self, raw: dict[str, Any]) -> None:
        del raw["limits"]
        with pytest.raises(ValidationError, match="limits"):
            SafetyConfig.model_validate(raw)

    def test_missing_meridian_section_is_rejected(self, raw: dict[str, Any]) -> None:
        del raw["limits"]["meridian"]
        with pytest.raises(ValidationError, match="meridian"):
            SafetyConfig.model_validate(raw)

    def test_safety_config_is_immutable(self, config_dir: Path) -> None:
        """CLAUDE.md invariant 2: limits cannot be modified at runtime."""
        config = load_safety_config(config_dir / "safety.yaml")
        with pytest.raises(ValidationError):
            config.limits.meridian.calibrated = True  # type: ignore[misc]

    def test_nested_limits_are_immutable(self, config_dir: Path) -> None:
        config = load_safety_config(config_dir / "safety.yaml")
        with pytest.raises(ValidationError):
            config.limits.altitude_min_deg = 0.0  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("altitude_min_deg", -1.0),
            ("altitude_min_deg", 91.0),
            ("altitude_max_deg", 91.0),
            ("sun_avoidance_deg", -1.0),
            ("sun_avoidance_deg", 181.0),
            ("sun_altitude_max_deg", -91.0),
            ("sun_altitude_max_deg", 91.0),
        ],
    )
    def test_out_of_range_limit_is_rejected(
        self, raw: dict[str, Any], field: str, value: float
    ) -> None:
        raw["limits"][field] = value
        with pytest.raises(ValidationError, match=field):
            SafetyConfig.model_validate(raw)

    def test_altitude_floor_above_ceiling_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["limits"]["altitude_min_deg"] = 80.0
        raw["limits"]["altitude_max_deg"] = 40.0
        with pytest.raises(ValidationError, match="altitude_min_deg"):
            SafetyConfig.model_validate(raw)

    def test_unknown_flip_strategy_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["limits"]["meridian"]["flip_strategy"] = "hope"
        with pytest.raises(ValidationError, match="flip_strategy"):
            SafetyConfig.model_validate(raw)

    def test_negative_safety_margin_is_rejected(self, raw: dict[str, Any]) -> None:
        """A negative margin would widen the limit instead of narrowing it."""
        raw["limits"]["meridian"]["safety_margin_deg"] = -5.0
        with pytest.raises(ValidationError, match="safety_margin_deg"):
            SafetyConfig.model_validate(raw)

    def test_unknown_abort_action_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["on_abort"]["action"] = "carry_on"
        with pytest.raises(ValidationError, match="action"):
            SafetyConfig.model_validate(raw)

    def test_unknown_degradation_policy_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["agent"]["on_api_unreachable"] = "ignore"
        with pytest.raises(ValidationError, match="on_api_unreachable"):
            SafetyConfig.model_validate(raw)

    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_agent_budget_is_rejected(
        self, raw: dict[str, Any], value: int
    ) -> None:
        raw["agent"]["max_tokens_per_session"] = value
        with pytest.raises(ValidationError, match="max_tokens_per_session"):
            SafetyConfig.model_validate(raw)


class TestMeridianCalibrationCoupling:
    """SPEC section 9.1.4 — measured values become the enforced hard limits."""

    def test_calibrated_config_validates(self, calibrated: dict[str, Any]) -> None:
        config = SafetyConfig.model_validate(calibrated)
        assert config.limits.meridian.calibrated is True

    @pytest.mark.parametrize(
        "field", ["hour_angle_east_limit_deg", "hour_angle_west_limit_deg"]
    )
    def test_calibrated_true_requires_both_hour_angle_limits(
        self, calibrated: dict[str, Any], field: str
    ) -> None:
        calibrated["limits"]["meridian"][field] = None
        with pytest.raises(ValidationError, match=field):
            SafetyConfig.model_validate(calibrated)

    def test_calibrated_true_requires_a_calibration_date(
        self, calibrated: dict[str, Any]
    ) -> None:
        calibrated["limits"]["meridian"]["calibration_date"] = None
        with pytest.raises(ValidationError, match="calibration_date"):
            SafetyConfig.model_validate(calibrated)

    def test_uncalibrated_may_not_carry_limits(self, calibrated: dict[str, Any]) -> None:
        """Stale measurements must not survive a reset of the calibrated flag."""
        calibrated["limits"]["meridian"]["calibrated"] = False
        with pytest.raises(ValidationError, match="calibrated"):
            SafetyConfig.model_validate(calibrated)

    def test_east_limit_must_be_east_of_west_limit(self, calibrated: dict[str, Any]) -> None:
        calibrated["limits"]["meridian"]["hour_angle_east_limit_deg"] = 30.0
        calibrated["limits"]["meridian"]["hour_angle_west_limit_deg"] = -30.0
        with pytest.raises(ValidationError, match="hour_angle_east_limit_deg"):
            SafetyConfig.model_validate(calibrated)

    def test_east_limit_must_be_negative_hour_angle(self, calibrated: dict[str, Any]) -> None:
        """East of the meridian is a negative hour angle, by convention (SPEC 7.2)."""
        calibrated["limits"]["meridian"]["hour_angle_east_limit_deg"] = 5.0
        with pytest.raises(ValidationError, match="hour_angle_east_limit_deg"):
            SafetyConfig.model_validate(calibrated)

    def test_west_limit_must_be_positive_hour_angle(self, calibrated: dict[str, Any]) -> None:
        calibrated["limits"]["meridian"]["hour_angle_west_limit_deg"] = -5.0
        with pytest.raises(ValidationError, match="hour_angle_west_limit_deg"):
            SafetyConfig.model_validate(calibrated)

    def test_effective_limits_apply_the_safety_margin(self, calibrated: dict[str, Any]) -> None:
        """SPEC section 9.1.4, read against the keep-out geometry of ADR 0020.

        "Measured values minus the margin" means *more conservative*, and the
        limits bracket a forbidden band, so the band grows at both edges.
        """
        config = SafetyConfig.model_validate(calibrated)
        east, west = config.limits.meridian.effective_hour_angle_limits_deg()
        assert east == pytest.approx(-30.0)  # -25 measured, 5 of margin, wider
        assert west == pytest.approx(25.0)  # +20 measured, 5 of margin, wider

    def test_effective_limits_are_unavailable_while_uncalibrated(
        self, config_dir: Path
    ) -> None:
        config = load_safety_config(config_dir / "safety.yaml")
        with pytest.raises(ValueError, match="not calibrated"):
            config.limits.meridian.effective_hour_angle_limits_deg()

    def test_a_margin_wider_than_the_measurement_is_fine_and_stricter(
        self, calibrated: dict[str, Any]
    ) -> None:
        """It used to be refused, on the reading ADR 0020 corrected.

        A margin of 40 against a measurement of -25 gives a band from -65: the
        mount loses a great deal of sky and is in no danger whatsoever. Only a
        margin that closes the circle is refused, and TestTheKeepOutBand covers
        that.
        """
        calibrated["limits"]["meridian"]["safety_margin_deg"] = 40.0
        east, west = SafetyConfig.model_validate(
            calibrated
        ).limits.meridian.effective_hour_angle_limits_deg()
        assert east == pytest.approx(-65.0)
        assert west == pytest.approx(60.0)


class TestTheKeepOutBand:
    """The limits bracket what is forbidden, not what is allowed — ADR 0020."""

    def test_the_margin_widens_the_band_on_both_sides(self, calibrated: dict[str, Any]) -> None:
        """The measured band is -25 to +20; the margin is 5; enforced is -30 to +25."""
        meridian = SafetyConfig.model_validate(calibrated).limits.meridian
        east, west = meridian.effective_hour_angle_limits_deg()
        assert east == pytest.approx(-30.0)
        assert west == pytest.approx(25.0)

    def test_the_margin_never_lends_sky(self, calibrated: dict[str, Any]) -> None:
        """Property, stated once: enforcement is always at least the measurement."""
        meridian = SafetyConfig.model_validate(calibrated).limits.meridian
        east, west = meridian.effective_hour_angle_limits_deg()
        measured_east = calibrated["limits"]["meridian"]["hour_angle_east_limit_deg"]
        measured_west = calibrated["limits"]["meridian"]["hour_angle_west_limit_deg"]
        assert east <= measured_east
        assert west >= measured_west

    def test_a_margin_that_forbids_the_whole_circle_is_refused(
        self, calibrated: dict[str, Any]
    ) -> None:
        calibrated["limits"]["meridian"]["hour_angle_west_limit_deg"] = 179.0
        calibrated["limits"]["meridian"]["safety_margin_deg"] = 5.0
        with pytest.raises(ValidationError, match="whole circle"):
            SafetyConfig.model_validate(calibrated)


class TestTheLatitudeTolerance:
    """A calibration is only valid at the latitude it was measured at — ADR 0021."""

    def test_the_shipped_file_carries_one(self, config_dir: Path) -> None:
        meridian = load_safety_config(config_dir / "safety.yaml").limits.meridian
        assert meridian.latitude_tolerance_deg > 0.0

    @pytest.mark.parametrize("value", [0.0, -0.5])
    def test_a_tolerance_that_is_not_positive_is_refused(
        self, raw: dict[str, Any], value: float
    ) -> None:
        """Zero would mean no two GPS fixes ever agree, so nothing is ever calibrated."""
        raw["limits"]["meridian"]["latitude_tolerance_deg"] = value
        with pytest.raises(ValidationError):
            SafetyConfig.model_validate(raw)

    def test_it_is_small_enough_to_mean_something(self, config_dir: Path) -> None:
        """A degree of latitude tilts the polar axis by a degree.

        The shipped safety margin is what absorbs that, so the tolerance has to
        stay a small fraction of it or a calibration would silently outlive the
        geometry it was measured against.
        """
        meridian = load_safety_config(config_dir / "safety.yaml").limits.meridian
        assert meridian.latitude_tolerance_deg <= meridian.safety_margin_deg / 5.0
