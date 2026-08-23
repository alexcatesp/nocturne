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

    **The two limits bracket a region the tube may not enter** — they are not
    the edges of a permitted range (ADR 0020). A Newtonian points steeply near
    culmination, and the end of the tube carrying the primary mirror cell swings
    *down* as it does, towards a tripod leg. Culmination is the meridian, so the
    forbidden region is the band around hour angle zero and the sky outside it
    is free. This is why the measured limits are what triggers the flip:
    ``hour_angle_east_limit_deg`` is where you stop or flip on the way in, and
    ``hour_angle_west_limit_deg`` is where you may resume on the way out.

    ``latitude_tolerance_deg`` is not a measurement of the tripod. It says how
    far the rig may travel before a calibration measured somewhere else stops
    applying, which matters because the calibration tool records the site it was
    measured at (ADR 0021).
    """

    calibrated: bool
    calibration_date: date | None = None
    hour_angle_east_limit_deg: Annotated[float, Field(ge=-180.0, lt=0.0)] | None = None
    hour_angle_west_limit_deg: Annotated[float, Field(gt=0.0, le=180.0)] | None = None
    safety_margin_deg: Annotated[float, Field(ge=0.0, le=90.0)]

    #: How far the site may move before a measured profile stops applying. The
    #: mount's polar axis is tilted to the latitude, so the arc the tube sweeps
    #: past the tripod legs is a function of it: a calibration measured at one
    #: latitude is not valid at another (SPEC section 9.1.7 applies the same
    #: reasoning to the counterweight).
    latitude_tolerance_deg: Annotated[float, Field(gt=0.0, le=90.0)]
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
    def _the_widened_band_still_leaves_a_sky(self) -> Self:
        """The margin widens the forbidden band; it must not close the circle.

        Under the keep-out reading of ADR 0020 the margin can never invert the
        limits — a wider band is a stricter one. What it *can* do, with absurd
        numbers, is forbid every hour angle there is, and a configuration that
        refuses the whole sky should say so at load rather than at three in the
        morning.
        """
        east = self.hour_angle_east_limit_deg
        west = self.hour_angle_west_limit_deg
        if east is None or west is None:
            return self
        if east - self.safety_margin_deg <= -180.0 or west + self.safety_margin_deg >= 180.0:
            raise ValueError(
                f"the measured limits ({east}, {west}) widened by "
                f"safety_margin_deg ({self.safety_margin_deg}) span the whole circle; "
                "no hour angle would ever be permitted"
            )
        return self

    def effective_hour_angle_limits_deg(self) -> tuple[float, float]:
        """The enforced **forbidden** band: the measurement widened by the margin.

        SPEC section 9.1.4 says "measured values minus ``safety_margin_deg``
        become the enforced hard limits". Read against the keep-out geometry of
        ADR 0020 that means *more conservative*, which on the eastern edge is
        literally minus and on the western edge is plus: clearance ran out at
        the measured hour angle, so the band the tube may not enter starts
        earlier and ends later than what was measured.

        Returns ``(east, west)`` with ``east < 0 < west``. **A target is refused
        when its hour angle lies strictly between them.**

        Raises ``ValueError`` while uncalibrated — there is no permissive
        fallback.
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
            self.hour_angle_east_limit_deg - self.safety_margin_deg,
            self.hour_angle_west_limit_deg + self.safety_margin_deg,
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
