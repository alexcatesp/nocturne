"""Commands that may reach the executor — SPEC section 9.

Layer 2 sits between the agent/API and Ekos. Everything that *changes* the
state of the instrument is expressed as one of these command objects, so that
there is a single, enumerable set of things the governor has to reason about.

Reading a property is not a command: it moves nothing and is not gated. Every
mutating operation is.

M1 defines the executor-level commands needed for instrument bring-up. The
session-level commands (slew, expose, focus, guide, flip) arrive with M2, and
each must be added to the governor's registry — an unregistered command type is
rejected, not passed through (see ``SafetyGovernor.validate``).
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
