"""What an INDI property write actually asks the instrument to do — SPEC section 9.

A command is only as safe as the governor's understanding of it. ``SetProperty``
carries a device, a vector name and a handful of values; whether that is a slew,
an open-loop jog, a park, or the camera's set point is a question about the
name, and until this module existed the governor did not ask it.

**The lists below are evidence, not recollection.** They are the vectors
``indi_eqmod`` exported on the reference rig, recorded in
``backend/tests/fixtures/hardware/wave150i-properties.txt`` during M1.
``test_safety_pointing.py`` walks that dump and fails if a vector whose name
denotes motion appears in none of the three sets — because anything unlisted
falls through to "not pointing" and is permitted.

**Why the fall-through is permissive at all**, when the rest of the safety layer
fails closed: M1 exists to read and write ordinary device properties — cooling
set points, filter slots, focuser positions — and a default-deny here would
refuse the milestone that is already built and working. The default-deny lives
one level up, on the *command type* (``SafetyGovernor.validate``). What makes
the permissive fall-through defensible is that the list is checked against a
recording of the real driver rather than against memory.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, final

from nocturne.safety.commands import (
    AbortMotion,
    Command,
    MountReport,
    ParkMount,
    ResetMountModel,
    ResolvedTarget,
    SetMeridianFlipOffset,
    SetProperty,
    SlewToCoordinates,
    SlewToTarget,
    SyncToCoordinates,
    SyncToTarget,
    UnparkMount,
)
from nocturne.safety.sky import SkyPosition

#: Vectors that command the mount to a position on the sky. A write to one of
#: these is a slew, and is validated as one.
COORDINATE_PROPERTIES: Final[frozenset[str]] = frozenset(
    {
        # indi_eqmod and most mounts: equinox of date, RA in hours, DEC in degrees.
        "EQUATORIAL_EOD_COORD",
        # The J2000 spelling, used by some drivers including the simulator.
        "EQUATORIAL_COORD",
        # Writing the target is what a goto does on drivers that separate the two.
        "TARGET_EOD_COORD",
    }
)

#: Elements of a coordinate vector. Both are required: INDI permits writing one
#: element of a vector, and a mount handed only an RA supplies the declination
#: from wherever it is currently pointing — which the governor cannot see.
RIGHT_ASCENSION_ELEMENT: Final = "RA"
DECLINATION_ELEMENT: Final = "DEC"

#: Vectors that move the mount without naming where it is going. There is no
#: coordinate to check against the limits, so there is no safe form of these in
#: M2's pointing gate. Each arrives, if it ever does, with the code that needs
#: it and a rule of its own: guide pulses bounded by duration, a home command
#: bounded by the driver's home position, alt/az goto with the arithmetic to
#: convert it. Refusing them is the default-deny working, not a gap.
UNVALIDATABLE_MOTION_PROPERTIES: Final[frozenset[str]] = frozenset(
    {
        "TELESCOPE_MOTION_NS",
        "TELESCOPE_MOTION_WE",
        "TELESCOPE_TIMED_GUIDE_NS",
        "TELESCOPE_TIMED_GUIDE_WE",
        # A goto in horizontal coordinates. Nocturne points in equatorial; the
        # conversion is arithmetic nothing needs yet, and half of it is worse
        # than none.
        "HORIZONTAL_COORD",
        # Decides whether the *next* coordinate write slews, tracks or syncs, so
        # it is part of the pointing decision rather than a separate one. It is
        # validated together with the slew command that performs a goto.
        "ON_COORD_SET",
        # Drives the mount to its home index (INDI 2.x home indexer).
        "TELESCOPE_HOME",
        # A sync does not move the mount; it moves the mount's model of where it
        # is, which is the frame every limit above is checked in. Plate-solve
        # syncs arrive through Ekos with M2's align module.
        "SYNCMANAGE",
        "STANDARDSYNCPOINT",
        "SYNCPOLARALIGN",
        "ALIGNSYNCMODE",
    }
)

#: The driver's own horizon-limit subsystem. It is a second enforcement layer
#: (ADR 0010) and Nocturne does not write it yet; a write from anywhere else
#: could clear it, and a cleared backstop that looks present is worse than a
#: backstop that was never claimed.
DRIVER_LIMIT_PROPERTIES: Final[frozenset[str]] = frozenset(
    {
        "HORIZONLIMITSDATAFILE",
        "HORIZONLIMITSFILEOPERATION",
        "HORIZONLIMITSLIMITGOTO",
        "HORIZONLIMITSMANAGE",
        "HORIZONLIMITSONLIMIT",
        "HORIZONLIMITSPOINT",
        "HORIZONLIMITSTRAVERSE",
    }
)


@final
@dataclass(frozen=True, slots=True)
class PointingIntent:
    """A command that puts the instrument at a readable place on the sky."""

    position: SkyPosition


@final
@dataclass(frozen=True, slots=True)
class UnreadablePointing:
    """A pointing command whose destination cannot be determined."""

    detail: str


@final
@dataclass(frozen=True, slots=True)
class ForbiddenMotion:
    """A command that moves something with no destination to validate."""

    rule: str
    detail: str


Classification = PointingIntent | UnreadablePointing | ForbiddenMotion


@final
@dataclass(frozen=True, slots=True)
class SyncIntent:
    """A command that moves the mount's model of where it is.

    Both positions, because a sync is only judgeable as a *pair*: how far it
    moves the model is the whole question, and neither half answers it alone.
    """

    #: Where the command says the instrument actually is.
    to: SkyPosition
    #: Where the mount believed it was when the command was built.
    reported: SkyPosition


SyncClassification = SyncIntent | UnreadablePointing


@final
@dataclass(frozen=True, slots=True)
class ResolutionIntent:
    """A command whose destination came from a name somebody else looked up."""

    target: ResolvedTarget


def pointing_intent(command: Command) -> Classification | None:
    """What ``command`` asks the mount to do, or ``None`` if it moves nothing.

    Every command type the governor knows about is answered here explicitly.
    ``test_safety_pointing.py`` asserts that, so a command type added to the
    registry without a decision about what it means fails the suite rather than
    falling through to "moves nothing".
    """
    match command:
        case SetProperty(property=name, values=values):
            return _classify_property(name, values)
        case SlewToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg):
            return _read_coordinates(ra_hours, dec_deg)
        case SlewToTarget(target=target):
            return _read_coordinates(target.ra_hours, target.dec_deg)
        case ParkMount() | UnparkMount() | AbortMotion():
            # None of the three names a place on the sky, so there is nothing
            # for the limits to be checked against. Why that is accepted rather
            # than treated as a gap is ADR 0022, kept by ADR 0023; the short
            # form is that park and abort are the safe state itself, and unpark
            # commands no motion of its own.
            return None
        case SyncToCoordinates() | SyncToTarget():
            # A sync is an *observation*, not a destination: it says where the
            # instrument already is. Running it through the pointing rules would
            # refuse a sync precisely when the tube is somewhere it should not
            # be — leaving the governor with a model it knows to be wrong, which
            # is the worse of the two states. It is bounded by SYNC_RULES
            # instead, on how far it moves the model (ADR 0023).
            return None
        case ResetMountModel() | SetMeridianFlipOffset():
            # Neither names a place either. One discards the frame, the other
            # configures when Ekos flips; MODEL_RULES and FLIP_RULES.
            return None
        case _:
            # ConnectDevice and DisconnectDevice. Connecting a driver does not
            # command motion; what the mount does on connect is the driver's
            # business and is bounded by MountLink's bring-up sequence.
            return None


def sync_intent(command: Command) -> SyncClassification | None:
    """How far ``command`` moves the mount's model, or ``None`` if it does not.

    Answered explicitly for every command type the governor knows about, and
    ``test_safety_sync_and_model.py`` asserts that, on the same grounds as
    :func:`pointing_intent`: a command that quietly falls through to "moves no
    model" is a command nobody decided about.
    """
    match command:
        case SyncToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg, reported=reported):
            return _read_sync(ra_hours, dec_deg, reported)
        case SyncToTarget(target=target, reported=reported):
            return _read_sync(target.ra_hours, target.dec_deg, reported)
        case _:
            return None


def resolution_intent(command: Command) -> ResolutionIntent | None:
    """The name lookup behind ``command``, or ``None`` if it names no target."""
    match command:
        case SlewToTarget(target=target) | SyncToTarget(target=target):
            return ResolutionIntent(target=target)
        case _:
            return None


def mount_report(command: Command) -> MountReport | None:
    """What the mount said about itself when ``command`` was built, if anything."""
    match command:
        case SyncToCoordinates(reported=reported):
            return reported
        case SyncToTarget(reported=reported):
            return reported
        case ResetMountModel(reported=reported):
            return reported
        case _:
            return None


def _read_sync(ra_hours: float, dec_deg: float, reported: MountReport) -> SyncClassification:
    """Both halves of a sync as positions, or why one of them is not one."""
    destination = _read_coordinates(ra_hours, dec_deg)
    if not isinstance(destination, PointingIntent):
        return (
            destination
            if isinstance(destination, UnreadablePointing)
            else UnreadablePointing(detail="the sync destination is not a position on the sky")
        )
    origin = _read_coordinates(reported.ra_hours, reported.dec_deg)
    if not isinstance(origin, PointingIntent):
        return UnreadablePointing(
            detail=(
                "the mount reported a position that is not on the sky "
                f"(RA {reported.ra_hours}, DEC {reported.dec_deg}), so how far this "
                "sync would move its model cannot be computed. A sync is only safe "
                "when it is small, and nothing here can say whether this one is"
            )
        )
    return SyncIntent(to=destination.position, reported=origin.position)


def _classify_property(name: str, values: Mapping[str, object]) -> Classification | None:
    if name in DRIVER_LIMIT_PROPERTIES:
        return ForbiddenMotion(
            rule="driver_limits",
            detail=(
                f"{name} is the driver's own horizon-limit subsystem. Nocturne derives "
                "and writes those limits as a second enforcement layer, and that path "
                "is specified in docs/decisions/0010-dual-meridian-enforcement.md and "
                "not yet implemented. Until it is, nothing writes them: a backstop in "
                "an unknown state is worse than one that was never claimed"
            ),
        )
    if name in UNVALIDATABLE_MOTION_PROPERTIES:
        return ForbiddenMotion(
            rule="unvalidatable_motion",
            detail=(
                f"{name} moves the mount, or changes what the next coordinate write "
                "does, without naming a position the governor can check against the "
                "altitude, Sun and meridian limits (SPEC sections 9.1 and 9.2)"
            ),
        )
    if name not in COORDINATE_PROPERTIES:
        return None
    return _read_position(name, values)


def _read_position(name: str, values: Mapping[str, object]) -> Classification:
    missing = sorted(
        {RIGHT_ASCENSION_ELEMENT, DECLINATION_ELEMENT} - {key.upper() for key in values}
    )
    if missing:
        return UnreadablePointing(
            detail=(
                f"a write to {name} without {' and '.join(missing)} leaves the mount to "
                "supply the rest from where it is currently pointing, which the governor "
                f"cannot see. Send both {RIGHT_ASCENSION_ELEMENT} and "
                f"{DECLINATION_ELEMENT}"
            )
        )
    raw = {key.upper(): value for key, value in values.items()}
    try:
        return PointingIntent(
            position=SkyPosition(
                ra_hours=_as_float(raw[RIGHT_ASCENSION_ELEMENT]),
                dec_deg=_as_float(raw[DECLINATION_ELEMENT]),
            )
        )
    except (TypeError, ValueError) as exc:
        return UnreadablePointing(
            detail=f"the coordinates written to {name} are not a position on the sky: {exc}"
        )


def _read_coordinates(ra_hours: float, dec_deg: float) -> Classification:
    """A slew command's destination, or why it cannot be read as one.

    Out-of-range coordinates are :class:`UnreadablePointing` rather than an
    exception, so that nonsense is refused by the governor — with a reason, in
    the decision log — instead of raising somewhere above it. The check is
    :class:`~nocturne.safety.sky.SkyPosition`'s own, so there is one definition
    of what a position is.
    """
    try:
        return PointingIntent(
            position=SkyPosition(ra_hours=_as_float(ra_hours), dec_deg=_as_float(dec_deg))
        )
    except (TypeError, ValueError) as exc:
        return UnreadablePointing(
            detail=f"the slew coordinates are not a position on the sky: {exc}"
        )


def _as_float(value: object) -> float:
    """A coordinate element as a number, refusing anything that is not one.

    ``bool`` is excluded deliberately: it is a subclass of ``int`` in Python, so
    ``float(True)`` is 1.0 and a switch value would silently become an hour of
    right ascension.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"expected a number, got {value!r}")
    return float(value)


__all__ = [
    "COORDINATE_PROPERTIES",
    "DECLINATION_ELEMENT",
    "DRIVER_LIMIT_PROPERTIES",
    "RIGHT_ASCENSION_ELEMENT",
    "UNVALIDATABLE_MOTION_PROPERTIES",
    "Classification",
    "ForbiddenMotion",
    "PointingIntent",
    "ResolutionIntent",
    "SyncClassification",
    "SyncIntent",
    "UnreadablePointing",
    "mount_report",
    "pointing_intent",
    "resolution_intent",
    "sync_intent",
]
