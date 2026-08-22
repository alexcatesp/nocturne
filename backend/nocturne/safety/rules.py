"""The pointing limits, as rules — SPEC sections 9.1 and 9.2.

Each rule is a pure function of the command and a :class:`RuleContext`: the
configuration, the site, and **one** instant, read from the governor's clock
once per decision. A rule that read the clock itself would make two rules in
the same decision disagree about where the sky is, and would make the decision
impossible to replay from its log line. ``test_safety_pointing.py`` enforces
that structurally.

Order matters, and it is the order below. A target below the horizon is
refused for altitude even in daylight, because "it is below your horizon" is
the more useful thing to be told; a target near the Sun is refused for the Sun
even though the twilight gate would also have caught it, because the Sun is the
one that burns a mirror.

**On checking the destination and not the path.** These rules validate where a
command sends the instrument, not the arc it takes to get there. That is sound
for the altitude and meridian limits, whose regions the mount cannot enter
without entering them at the destination too — but it would not be sound for
the Sun, because a slew across the sky in daylight can sweep the aperture past
it. This is why ``sun_altitude_max_deg`` is enforced here as a gate on pointing
at all, rather than only as a condition for imaging: with the Sun more than
twelve degrees below the horizon and the altitude floor at twenty-five, no arc
between two permitted points can reach it (ADR 0019).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, final

from nocturne.safety.commands import Command
from nocturne.safety.decisions import Rejected
from nocturne.safety.properties import (
    ForbiddenMotion,
    PointingIntent,
    UnreadablePointing,
    pointing_intent,
)
from nocturne.safety.sky import (
    SkyPosition,
    altitude_azimuth_deg,
    angular_separation_deg,
    hour_angle_deg,
    sun_position,
)
from nocturne.schemas import SafetyConfig, Site

#: Where the operator changes any number named in a rejection. Repeated in the
#: messages because a rejection read on a phone, at night, is the only place
#: some of these numbers are ever seen.
CONFIG_FILES: Final = "config/safety.yaml (or your config/safety.local.yaml)"


@final
@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything a rule may consult. Immutable, and built once per decision."""

    config: SafetyConfig
    site: Site | None
    when: datetime


#: A rule returns a rejection, or ``None`` if it permits the command.
Rule = Callable[[Command, RuleContext], Rejected | None]


def refuse_motion_that_cannot_be_validated(
    command: Command, context: RuleContext
) -> Rejected | None:
    """Anything that moves the mount without a destination the governor can read."""
    intent = pointing_intent(command)
    match intent:
        case ForbiddenMotion(rule=rule, detail=detail):
            return Rejected(rule=rule, reason=detail)
        case UnreadablePointing(detail=detail):
            return Rejected(rule="pointing_unreadable", reason=detail)
        case _:
            return None


def require_a_configured_site(command: Command, context: RuleContext) -> Rejected | None:
    """Altitude, hour angle and the Sun do not exist without a place to stand."""
    if not isinstance(pointing_intent(command), PointingIntent):
        return None
    if context.site is None:
        return Rejected(
            rule="site",
            reason=(
                "no observing site was given to the safety governor, so the altitude, "
                "hour angle and Sun distance of this target cannot be computed. Nothing "
                "is pointed on an unknown horizon"
            ),
        )
    if context.site.is_placeholder:
        return Rejected(
            rule="site",
            reason=(
                "the site coordinates are still the shipped placeholder, so every "
                "altitude and hour angle below would be computed for somewhere else — "
                "silently, and wrongly. Put your own coordinates in "
                "config/equipment.local.yaml (copy config/equipment.local.yaml.example) "
                "before anything is pointed"
            ),
        )
    return None


def enforce_altitude_limits(command: Command, context: RuleContext) -> Rejected | None:
    """SPEC section 9.2: altitude floor and ceiling."""
    resolved = _resolve(command, context)
    if resolved is None:
        return None
    position, site = resolved
    limits = context.config.limits
    altitude, _ = altitude_azimuth_deg(
        hour_angle_deg(position.ra_hours, context.when, site.longitude),
        position.dec_deg,
        site.latitude,
    )
    if altitude < limits.altitude_min_deg:
        return Rejected(
            rule="altitude",
            reason=(
                f"the target is at altitude {altitude:.1f} degrees, below the floor of "
                f"{limits.altitude_min_deg} degrees set in {CONFIG_FILES}"
            ),
        )
    if altitude > limits.altitude_max_deg:
        return Rejected(
            rule="altitude",
            reason=(
                f"the target is at altitude {altitude:.1f} degrees, above the ceiling of "
                f"{limits.altitude_max_deg} degrees set in {CONFIG_FILES}"
            ),
        )
    return None


def enforce_sun_avoidance(command: Command, context: RuleContext) -> Rejected | None:
    """SPEC section 9.2: keep the aperture away from the Sun, day or night."""
    resolved = _resolve(command, context)
    if resolved is None:
        return None
    position, _ = resolved
    limit = context.config.limits.sun_avoidance_deg
    separation = angular_separation_deg(position, sun_position(context.when))
    if separation < limit:
        return Rejected(
            rule="sun_avoidance",
            reason=(
                f"the target is {separation:.1f} degrees from the Sun, inside the "
                f"{limit}-degree avoidance circle set in {CONFIG_FILES}. A 200 mm "
                "aperture pointed near the Sun destroys the imaging train and anything "
                "in front of it"
            ),
        )
    return None


def enforce_sun_altitude_gate(command: Command, context: RuleContext) -> Rejected | None:
    """SPEC section 9.2: nothing is pointed while the Sun is up.

    This is also what makes checking the destination rather than the path sound;
    see the module docstring.
    """
    resolved = _resolve(command, context)
    if resolved is None:
        return None
    _, site = resolved
    limit = context.config.limits.sun_altitude_max_deg
    sun = sun_position(context.when)
    altitude, _ = altitude_azimuth_deg(
        hour_angle_deg(sun.ra_hours, context.when, site.longitude), sun.dec_deg, site.latitude
    )
    if altitude > limit:
        return Rejected(
            rule="sun_altitude",
            reason=(
                f"the Sun is at altitude {altitude:.1f} degrees, above the "
                f"{limit}-degree gate set in {CONFIG_FILES}. Nothing is pointed until "
                "it is below: a slew sweeps an arc, and only this rule keeps that arc "
                "away from the Sun"
            ),
        )
    return None


def enforce_meridian_limits(command: Command, context: RuleContext) -> Rejected | None:
    """SPEC section 9.1 — the tripod collision. The highest-severity limit here.

    While ``meridian.calibrated`` is false there is no limit to enforce and no
    safe subset to fall back on, so every pointing command is refused (ADR 0019).
    """
    resolved = _resolve(command, context)
    if resolved is None:
        return None
    position, site = resolved
    meridian = context.config.limits.meridian
    if not meridian.calibrated:
        return Rejected(
            rule="meridian",
            reason=(
                "the meridian limits for this mount and tripod have not been "
                "calibrated, so nothing knows at which hour angle the tube reaches a "
                "tripod leg. Every pointing command is refused until they are. Complete "
                "the procedure in docs/meridian-calibration.md and record the measured "
                "limits in config/safety.local.yaml (SPEC section 9.1)"
            ),
        )
    east, west = meridian.effective_hour_angle_limits_deg()
    angle = hour_angle_deg(position.ra_hours, context.when, site.longitude)
    if angle < east or angle > west:
        side = "east" if angle < east else "west"
        return Rejected(
            rule="meridian",
            reason=(
                f"the target is at hour angle {angle:+.1f} degrees, past the enforced "
                f"{side} limit of {east:+.1f} to {west:+.1f} degrees. Those are the "
                f"measured limits narrowed by the {meridian.safety_margin_deg}-degree "
                f"safety margin from {CONFIG_FILES} (SPEC section 9.1)"
            ),
        )
    return None


#: The rules that constrain a pointing command, in the order they are applied.
#: Every one of them is bound to any future command that names a place on the
#: sky; the registry in the governor is what binds them.
POINTING_RULES: Final[tuple[Rule, ...]] = (
    refuse_motion_that_cannot_be_validated,
    require_a_configured_site,
    enforce_altitude_limits,
    enforce_sun_avoidance,
    enforce_sun_altitude_gate,
    enforce_meridian_limits,
)


def _resolve(command: Command, context: RuleContext) -> tuple[SkyPosition, Site] | None:
    """The target and the site, when both are known and the command points.

    Returning ``None`` is not permission: it means this rule has nothing to say,
    because the command moves nothing or because an earlier rule in
    :data:`POINTING_RULES` has already refused it. The order is what makes that
    true, and it is asserted in the tests.
    """
    intent = pointing_intent(command)
    if not isinstance(intent, PointingIntent) or context.site is None:
        return None
    if context.site.is_placeholder:
        return None
    return intent.position, context.site


__all__ = [
    "POINTING_RULES",
    "Rule",
    "RuleContext",
    "enforce_altitude_limits",
    "enforce_meridian_limits",
    "enforce_sun_altitude_gate",
    "enforce_sun_avoidance",
    "refuse_motion_that_cannot_be_validated",
    "require_a_configured_site",
]
