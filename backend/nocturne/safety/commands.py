"""Commands that may reach the executor — SPEC section 9.

Layer 2 sits between the agent/API and Ekos. Everything that *changes* the
state of the instrument is expressed as one of these command objects, so that
there is a single, enumerable set of things the governor has to reason about.

Reading a property is not a command: it moves nothing and is not gated. Every
mutating operation is.

M1 defined the executor-level commands needed for instrument bring-up. M2 adds
the mount commands — slew, park, unpark, abort — as the Ekos mount module comes
into use; expose, focus, guide and flip follow with their own modules. Each must
be added to the governor's registry — an unregistered command type is rejected,
not passed through (see ``SafetyGovernor.validate``).

**A command is a request, not a movement.** Every one of these names something
the instrument should do; whether it is done is the executor's business and
whether it is *permitted* is the governor's. Nothing below reaches a driver
without an ``Approval``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict

from nocturne.schemas import StrictModel

#: A property element value as carried by the INDI protocol.
PropertyValue = float | str | bool


class MountReport(StrictModel):
    """What the mount says about itself, at the instant a command was built.

    Three commands cannot be judged without it — a sync, which is only safe if
    it is *small*, and a model reset, which is only safe if the mount is
    parked — and none of the three can be judged by the governor alone, because
    the governor holds no live state and is not going to start.

    So the state travels **with the command**. The executor reads it from the
    driver when it builds the command, the governor validates the pair, and the
    decision stays exactly what SPEC section 9 asks a decision to be:
    reproducible from its own log line, with nothing behind it that could have
    changed in between (ADR 0023).
    """

    ra_hours: float
    dec_deg: float
    parked: bool


class ResolvedTarget(StrictModel):
    """A target named by the operator or the agent, after KStars resolved it.

    A name is not a place. ``Mount.gotoTarget("M31")`` looks the name up inside
    KStars and slews to whatever comes back, which means the governor is handed
    a string and asked to approve a destination it cannot see. Resolving first
    and validating the coordinates turns it back into an ordinary pointing
    decision.

    ``resolved_at`` is **KStars' clock**, not Nocturne's, and it is carried
    because they can differ: KStars precesses to the instant its own simulated
    clock reads, and that clock can be stopped or moved with nothing looking
    wrong. ``object_type`` is carried because a name may resolve to something
    that does not hold still, and a position validated now for a body that moves
    is a position validated for the wrong time (SPEC section 1.2 puts those out
    of scope anyway).
    """

    name: str
    ra_hours: float
    dec_deg: float
    object_type: str
    resolved_at: datetime


class Command(StrictModel):
    """Base class for anything that changes instrument state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def describe(self) -> str:
        """Short human-readable description, used in logs and rejection reasons."""
        return type(self).__name__


class ConnectDevice(Command):
    """Connect an INDI device (set its CONNECTION property to CONNECT)."""

    device: str

    def describe(self) -> str:
        return f"connect {self.device}"


class DisconnectDevice(Command):
    """Disconnect an INDI device."""

    device: str

    def describe(self) -> str:
        return f"disconnect {self.device}"


class SetProperty(Command):
    """Write one or more elements of a device property vector."""

    device: str
    property: str
    values: dict[str, PropertyValue]

    def describe(self) -> str:
        elements = ", ".join(f"{name}={value!r}" for name, value in sorted(self.values.items()))
        return f"set {self.device}.{self.property} [{elements}]"


class SlewToCoordinates(Command):
    """Point the mount at an equatorial position, equinox of date.

    Right ascension in hours and declination in degrees, which is what both
    INDI's ``EQUATORIAL_EOD_COORD`` and Ekos' ``Mount.slew`` carry — the latter
    recorded verbatim from the pinned KStars source in
    ``backend/tests/fixtures/hardware/kstars-mount-dbus-units.txt``.

    The coordinates are plain floats and are **not** range-checked here. A right
    ascension of 99 hours is refused by the governor, with a reason, rather than
    raising a validation error before the governor ever sees it: a command that
    never reaches the gate is a command that never appears in the decision log.
    """

    ra_hours: float
    dec_deg: float

    def describe(self) -> str:
        return f"slew to RA {self.ra_hours:.5f} h, DEC {self.dec_deg:+.4f} deg"


class ParkMount(Command):
    """Drive the mount to its park position and hold it there.

    Carries no pointing rules, and that is a decision rather than an omission
    (ADR 0022, kept by ADR 0023). Parking is the terminal safe action of SPEC
    section 8.5: every
    degradation path ends here, so a rule that could refuse a park would make
    the safe state unreachable exactly when it is needed.
    """

    def describe(self) -> str:
        return "park the mount"


class UnparkMount(Command):
    """Release the mount from its park position.

    Names no destination, so there is nothing for the pointing rules to check;
    ADR 0022 sets out why that is accepted rather than papered over, and what
    the session layer owes on top of it; ADR 0023 kept it when it gave every
    other call on this interface a rule.
    """

    def describe(self) -> str:
        return "unpark the mount"


class AbortMotion(Command):
    """Stop the mount moving, whatever it is doing.

    Never refused, for the same reason as :class:`ParkMount`: this is the
    command an operator or a watchdog reaches for when something is wrong, and
    a safety layer that can refuse to stop the instrument is not a safety layer.
    """

    def describe(self) -> str:
        return "abort mount motion"


class SyncToCoordinates(Command):
    """Tell the mount it is at this equatorial position, equinox of date.

    A sync moves nothing. It moves the mount's *model* of where it is, which is
    the frame the altitude, Sun and meridian limits are all measured in — so
    the danger is not motion now, it is every slew afterwards being computed
    against a rotated sky.

    It is bounded rather than refused (ADR 0023): the position synced to must be
    within ``limits.targeting.sync_tolerance_deg`` of the position the mount
    reports. That is the check that separates a plate solve correcting a cold
    mount's pointing error from a solve that landed on the wrong field.
    """

    ra_hours: float
    dec_deg: float
    reported: MountReport

    def describe(self) -> str:
        return (
            f"sync to RA {self.ra_hours:.5f} h, DEC {self.dec_deg:+.4f} deg "
            f"(mount reports RA {self.reported.ra_hours:.5f} h, "
            f"DEC {self.reported.dec_deg:+.4f} deg)"
        )


class SlewToTarget(Command):
    """Slew to a target named rather than pointed at.

    Carries the resolved coordinates, so the governor validates a place on the
    sky exactly as it validates :class:`SlewToCoordinates` — and additionally
    checks that the resolution is fresh and that the object holds still.
    """

    target: ResolvedTarget

    def describe(self) -> str:
        return (
            f"slew to {self.target.name} at RA {self.target.ra_hours:.5f} h, "
            f"DEC {self.target.dec_deg:+.4f} deg"
        )


class SyncToTarget(Command):
    """Tell the mount it is pointing at a named target.

    The startup alignment a rough polar align ends with. It is a sync and a
    resolution at once, and carries the rules of both.
    """

    target: ResolvedTarget
    reported: MountReport

    def describe(self) -> str:
        return (
            f"sync to {self.target.name} at RA {self.target.ra_hours:.5f} h, "
            f"DEC {self.target.dec_deg:+.4f} deg (mount reports RA "
            f"{self.reported.ra_hours:.5f} h, DEC {self.reported.dec_deg:+.4f} deg)"
        )


class ResetMountModel(Command):
    """Discard the mount's alignment model.

    The most complete way to move the frame: not "you are somewhere else" but
    "forget everything you knew about where you are". Permitted only with the
    mount parked, because a parked mount is at a position the driver knows
    independently of the model being discarded — so there is a defined state on
    the other side of the operation rather than an unknown one (ADR 0023).
    """

    reported: MountReport

    def describe(self) -> str:
        return "reset the mount's alignment model"


class SetMeridianFlipOffset(Command):
    """Configure Ekos' own automatic meridian flip.

    ``offset_deg`` is the hour angle, **in degrees**, at which Ekos flips —
    positive west of the meridian, negative east of it. Degrees despite the
    argument being named ``hours`` in KStars' own DBus definition; that defect
    is recorded in ``kstars-mount-dbus-units.txt`` and is why this field is
    named for what it is.

    Enabling it makes Ekos a second thing deciding when to flip, beside the
    governor. The rule that makes that safe is that Ekos must flip **before**
    the tube reaches the governor's keep-out band, never after (ADR 0023).
    """

    enabled: bool
    offset_deg: float

    def describe(self) -> str:
        if not self.enabled:
            return "disable the Ekos meridian flip"
        return f"flip at hour angle {self.offset_deg:+.1f} deg, in Ekos"


class SolveHere(Command):
    """Expose a frame and plate-solve it, without acting on the answer.

    Carries no rules, and that is the substantive decision (ADR 0024) rather
    than an omission: this command *is* a read of the sky, and it is one only
    because it also sets Ekos' goto mode to the value at which
    ``Align::executeGOTO()`` does nothing. The two go together in one command
    for that reason — set the mode once at startup and Ekos could be put back
    into a slewing mode by its own polar-alignment path, or by a
    ``loadAndSlew`` nobody meant to leave behind.

    What happens to the mount afterwards is a :class:`SyncToCoordinates` or a
    :class:`SlewToCoordinates`, decided by Nocturne and seen by the governor.
    """

    #: Seconds of exposure. ``None`` leaves the align module on whatever it was
    #: configured with, which is what an operator adjusting it in the Ekos UI
    #: expects.
    binning_index: int | None = None

    def describe(self) -> str:
        return "capture and plate-solve, without moving the mount"


class SetAlignTarget(Command):
    """Set the coordinates the solver measures its solution against.

    Moves nothing by itself. It carries the pointing rules anyway, and cheaply:
    this is the coordinate Ekos would slew to if its goto mode were ever
    something other than the one :class:`SolveHere` sets, so validating it means
    that a regression in *that* rule still could not put the instrument
    somewhere the limits forbid. Two locks on one door, and the second one costs
    an arithmetic comparison.

    **J2000**, because that is the frame the align module's ``setTargetCoords``
    takes. The governor works in the equinox of date, so the executor precesses
    before building the command and the command carries the result — one
    conversion, at the boundary, where it can be seen.
    """

    ra_hours: float
    dec_deg: float

    def describe(self) -> str:
        return (
            f"set the align target to RA {self.ra_hours:.5f} h, "
            f"DEC {self.dec_deg:+.4f} deg (equinox of date)"
        )


class AbortAlign(Command):
    """Stop the plate solver.

    Never refused, for the reason :class:`AbortMotion` is never refused: this is
    what an operator reaches for when something is wrong.
    """

    def describe(self) -> str:
        return "abort the plate solve"
