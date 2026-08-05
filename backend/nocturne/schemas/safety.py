"""Schema for ``config/safety.yaml`` — SPEC sections 5.2 and 9.

Values validated here are enforced by the safety governor, not offered as
advice. The schema is deliberately unforgiving: a safety file that does not
make internal sense must never load, because a partially valid safety file is
worse than no file at all.

The models are frozen. There is no supported way to change a limit at runtime
(CLAUDE.md invariant 2).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .common import StrictModel

#: SPEC section 5.2. Approaching the meridian limit either flips the mount or
#: stops the session; there is no "continue anyway".
FlipStrategy = Literal["flip", "stop"]

#: SPEC section 5.2 defines exactly one abort action. Anything else fails loudly
#: rather than resolving to undefined behaviour.
AbortAction = Literal["park"]

#: SPEC section 9.4 names "alarm" explicitly; the two lesser levels are the
#: severities used by notify_operator (SPEC section 8.2).
#: See docs/decisions/0004-closed-vocabularies.md.
NotifySeverity = Literal["info", "warning", "alarm"]

#: SPEC section 8.5: loss of the agent degrades to deterministic autonomous
#: operation. That is the only degradation the specification defines.
DegradationPolicy = Literal["autonomous"]

PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]
Percent = Annotated[float, Field(gt=0.0, le=100.0)]
Degrees = Annotated[float, Field(ge=0.0, le=90.0)]


class MeridianLimits(StrictModel):
    """Meridian and tripod collision limits — SPEC section 9.1.

    The 200PDS strikes the tripod legs near the meridian without a tripod
    extension. These limits are measured by the operator with the procedure in
    docs/meridian-calibration.md, never guessed.

    Hour angle sign convention (SPEC section 7.2): negative east of the
    meridian, positive west of it.
    """

    calibrated: bool
    calibration_date: date | None = None
    hour_angle_east_limit_deg: Annotated[float, Field(ge=-180.0, lt=0.0)] | None = None
    hour_angle_west_limit_deg: Annotated[float, Field(gt=0.0, le=180.0)] | None = None
    safety_margin_deg: Annotated[float, Field(ge=0.0, le=90.0)]
    flip_strategy: FlipStrategy
    flip_settle_s: Annotated[float, Field(ge=0.0)]
    require_solve_after_flip: bool

    @model_validator(mode="after")
    def _calibration_is_all_or_nothing(self) -> Self:
        measured = {
            "calibration_date": self.calibration_date,
            "hour_angle_east_limit_deg": self.hour_angle_east_limit_deg,
            "hour_angle_west_limit_deg": self.hour_angle_west_limit_deg,
        }
        missing = sorted(name for name, value in measured.items() if value is None)
        present = sorted(name for name, value in measured.items() if value is not None)

        if self.calibrated and missing:
            raise ValueError(
                "meridian.calibrated is true but these measured values are "
                f"missing: {', '.join(missing)}. Run the procedure in "
                "docs/meridian-calibration.md."
            )
        if not self.calibrated and present:
            raise ValueError(
                "meridian.calibrated is false but these measured values are still "
                f"present: {', '.join(present)}. Clearing calibrated invalidates "
                "the measurement; clear these fields too (SPEC section 9.1.7)."
            )
        return self

    @model_validator(mode="after")
    def _limits_bracket_the_meridian(self) -> Self:
        east = self.hour_angle_east_limit_deg
        west = self.hour_angle_west_limit_deg
        if east is None or west is None:
            return self
        if east >= west:
            raise ValueError(
                "hour_angle_east_limit_deg must be less than "
                f"hour_angle_west_limit_deg, got {east} and {west}"
            )
        return self

    @model_validator(mode="after")
    def _margin_does_not_swallow_the_measurement(self) -> Self:
        east = self.hour_angle_east_limit_deg
        west = self.hour_angle_west_limit_deg
        if east is None or west is None:
            return self
        if self.safety_margin_deg >= abs(east) or self.safety_margin_deg >= west:
            raise ValueError(
                f"safety_margin_deg ({self.safety_margin_deg}) is not smaller than "
                f"the measured limits ({east}, {west}); applying it would invert "
                "the usable range"
            )
        return self

    def effective_hour_angle_limits_deg(self) -> tuple[float, float]:
        """Enforced limits: the measured values narrowed by the safety margin.

        SPEC section 9.1.4. Raises ``ValueError`` while uncalibrated — there is
        no permissive fallback.
        """
        if (
            not self.calibrated
            or self.hour_angle_east_limit_deg is None
            or self.hour_angle_west_limit_deg is None
        ):
            raise ValueError(
                "meridian limits are not calibrated; no hour angle limit can be "
                "derived. Run docs/meridian-calibration.md."
            )
        return (
            self.hour_angle_east_limit_deg + self.safety_margin_deg,
            self.hour_angle_west_limit_deg - self.safety_margin_deg,
        )


class CoolingLimits(StrictModel):
    """TEC protection — SPEC section 5.2."""

    max_delta_from_ambient_c: PositiveFloat
    abort_if_power_draw_exceeds_percent: Percent


class Limits(StrictModel):
    """Hard limits — SPEC sections 5.2, 9.1 and 9.2."""

    altitude_min_deg: Degrees
    altitude_max_deg: Degrees
    sun_avoidance_deg: Annotated[float, Field(ge=0.0, le=180.0)]
    sun_altitude_max_deg: Annotated[float, Field(ge=-90.0, le=90.0)]
    meridian: MeridianLimits
    cooling: CoolingLimits

    @model_validator(mode="after")
    def _altitude_window_is_ordered(self) -> Self:
        if self.altitude_min_deg >= self.altitude_max_deg:
            raise ValueError(
                f"altitude_min_deg ({self.altitude_min_deg}) must be below "
                f"altitude_max_deg ({self.altitude_max_deg}); as written there is "
                "no altitude at which imaging is permitted"
            )
        return self


class AbortConditions(StrictModel):
    """Conditions that abort a running session — SPEC sections 5.2 and 9.2."""

    guiding_lost_s: PositiveInt
    plate_solve_failures_consecutive: PositiveInt
    cloud_star_count_drop_percent: Percent
    cloud_confirm_frames: PositiveInt
    mount_communication_loss_s: PositiveInt
    disk_free_min_gb: PositiveFloat


class OnAbort(StrictModel):
    """What happens on abort — SPEC sections 5.2 and 9.4."""

    action: AbortAction
    notify: bool
    notify_severity: NotifySeverity


class AgentBudget(StrictModel):
    """Agent budget and degradation policy — SPEC sections 5.2 and 8.5."""

    max_tokens_per_session: PositiveInt
    max_calls_per_hour: PositiveInt
    on_budget_exhausted: DegradationPolicy
    on_api_unreachable: DegradationPolicy


class SafetyConfig(StrictModel):
    """Root model for ``config/safety.yaml``."""

    limits: Limits
    abort_conditions: AbortConditions
    on_abort: OnAbort
    agent: AgentBudget
