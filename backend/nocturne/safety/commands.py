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

from pydantic import ConfigDict

from nocturne.schemas import StrictModel

#: A property element value as carried by the INDI protocol.
PropertyValue = float | str | bool


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
    (ADR 0022). Parking is the terminal safe action of SPEC section 8.5: every
    degradation path ends here, so a rule that could refuse a park would make
    the safe state unreachable exactly when it is needed.
    """

    def describe(self) -> str:
        return "park the mount"


class UnparkMount(Command):
    """Release the mount from its park position.

    Names no destination, so there is nothing for the pointing rules to check;
    ADR 0022 sets out why that is accepted rather than papered over, and what
    the session layer owes on top of it.
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
