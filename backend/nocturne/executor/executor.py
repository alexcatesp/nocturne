"""The executor facade — SPEC section 3 (layers 1 and 2) and section 9.

Everything above this layer talks to the instrument through this object, and
this object performs nothing that the safety governor has not approved.

The invariant, in code rather than in prose: the only method that touches the
instrument is :meth:`Executor._perform`, it accepts an
:class:`~nocturne.safety.Approval` and nothing else, and only
:meth:`~nocturne.safety.SafetyGovernor.approve` can mint one. Every public
mutating method builds a command, hands it to the governor and passes on what
comes back. There is no argument, no keyword, and no subclass hook that skips
that step (CLAUDE.md invariant 1).

Reads are not commands. Looking at a cached property moves nothing, so it is
not gated; SPEC section 9 gates commands.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import final

from nocturne.executor.ekos import EkosBridge, EkosError
from nocturne.executor.indi.client import EventHandler, IndiClient
from nocturne.executor.indi.protocol import ElementValue, Property
from nocturne.safety import (
    AbortMotion,
    Approval,
    Command,
    ConnectDevice,
    DisconnectDevice,
    ParkMount,
    SafetyGovernor,
    SafetyViolation,
    SetProperty,
    SlewToCoordinates,
    UnparkMount,
)

logger = logging.getLogger("nocturne.executor")

#: What performing a command gives back. A property write returns the vector as
#: the driver echoed it; a mount command returns whether Ekos says it sent the
#: command on — which is not that the mount accepted it, and not that the motion
#: finished. Anything that waits for a slew to complete watches the mount's
#: coordinates, and that belongs to the session layer.
CommandResult = Property | bool | None


@final
class Executor:
    """The single door between Nocturne and the instrument."""

    def __init__(
        self,
        client: IndiClient,
        governor: SafetyGovernor,
        ekos: EkosBridge | None = None,
    ) -> None:
        self._client = client
        self._governor = governor
        self._ekos = ekos

    # ------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> Executor:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Connect to indiserver, and to Ekos if a bridge was supplied."""
        await self._client.connect()
        if self._ekos is not None:
            await self._ekos.connect()

    async def aclose(self) -> None:
        """Close every connection."""
        if self._ekos is not None:
            await self._ekos.aclose()
        await self._client.aclose()

    @property
    def governor(self) -> SafetyGovernor:
        """The governor every command passes through."""
        return self._governor

    # There is deliberately no accessor returning the INDI client or the Ekos
    # bridge. Both carry mutating methods that the governor does not see, so
    # handing one to a caller would be the second door CLAUDE.md invariant 1
    # says must not exist. Reads are proxied below; writes go through
    # _execute(). Enforced by tests/unit/test_safety_boundaries.py.

    def _mount(self) -> EkosBridge:
        """The bridge, when there is one to command the mount through.

        Deliberately not a property and deliberately private: it returns the
        object with the mutating calls on it, and the only caller is
        :meth:`_perform`, which has an approval in hand. Handing this to anyone
        else would be the second door.
        """
        if self._ekos is None:
            raise EkosError(
                "this executor was built without an Ekos bridge, so it cannot command "
                "the mount. Construct Executor(client, governor, ekos=EkosBridge(...)) "
                "and attach the module objects after Ekos is started."
            )
        return self._ekos

    @property
    def is_ekos_connected(self) -> bool:
        """Whether the Ekos bridge, if one was supplied, is attached to KStars."""
        return self._ekos is not None and self._ekos.is_connected

    # ----------------------------------------------------------------- reads

    def devices(self) -> tuple[str, ...]:
        """Devices that have defined at least one property."""
        return self._client.devices()

    def get_property(self, device: str, name: str) -> Property | None:
        """A property from the cache, or ``None``."""
        return self._client.get(device, name)

    def properties(self, device: str | None = None) -> Mapping[tuple[str, str], Property]:
        """The property cache, optionally narrowed to one device."""
        return self._client.properties(device)

    def is_device_connected(self, device: str) -> bool:
        """Whether ``device`` reports itself connected."""
        return self._client.is_device_connected(device)

    async def wait_for_property(
        self, device: str, name: str, *, timeout: float | None = None
    ) -> Property:
        """Wait until ``device`` has defined ``name``."""
        return await self._client.wait_for_property(device, name, timeout=timeout)

    async def wait_for_device(self, device: str, *, timeout: float | None = None) -> None:
        """Wait until ``device`` exists."""
        await self._client.wait_for_device(device, timeout=timeout)

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Watch transport events. Returns an unsubscribe callable.

        Listening is a read: an event says what happened, and a subscriber that
        wants to act on one comes back through the command methods above like
        everyone else. The callable returned unsubscribes; it carries no other
        capability.
        """
        return self._client.subscribe(handler)

    # -------------------------------------------------------------- commands

    async def connect_device(
        self, device: str, *, timeout: float | None = None
    ) -> Property | None:
        """Connect ``device``, if the governor permits it."""
        return await self._execute_property(ConnectDevice(device=device), timeout=timeout)

    async def disconnect_device(
        self, device: str, *, timeout: float | None = None
    ) -> Property | None:
        """Disconnect ``device``, if the governor permits it."""
        return await self._execute_property(DisconnectDevice(device=device), timeout=timeout)

    async def set_property(
        self,
        device: str,
        name: str,
        values: Mapping[str, ElementValue],
        *,
        timeout: float | None = None,
    ) -> Property | None:
        """Write ``values`` into ``device``'s ``name`` vector, if permitted."""
        command = SetProperty(device=device, property=name, values=dict(values))
        return await self._execute_property(command, timeout=timeout)

    # ---------------------------------------------------------- the mount

    # These four are the whole of what Nocturne asks the mount to do, and each
    # one is a command object before it is a DBus call: the governor sees a
    # SlewToCoordinates, not a pair of floats already on their way to KStars.
    # ADR 0022 records which of the mount module's other methods are refused.

    async def slew(
        self, ra_hours: float, dec_deg: float, *, timeout: float | None = None
    ) -> bool:
        """Slew the mount to an equatorial position, if the governor permits it.

        Right ascension in hours, declination in degrees, equinox of date — the
        units both the safety limits and Ekos are stated in.

        Returns whether Ekos reports the command was sent. It is not a promise
        that the mount arrived.

        Raises:
            SafetyViolation: if the altitude, Sun or meridian limits refuse this
                target, or the meridian limits are uncalibrated. Nothing is sent.
        """
        command = SlewToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg)
        return await self._execute_mount(command, timeout=timeout)

    async def park(self, *, timeout: float | None = None) -> bool:
        """Drive the mount to its park position."""
        return await self._execute_mount(ParkMount(), timeout=timeout)

    async def unpark(self, *, timeout: float | None = None) -> bool:
        """Release the mount from its park position."""
        return await self._execute_mount(UnparkMount(), timeout=timeout)

    async def abort_motion(self, *, timeout: float | None = None) -> bool:
        """Stop the mount moving."""
        return await self._execute_mount(AbortMotion(), timeout=timeout)

    async def _execute_property(
        self, command: Command, *, timeout: float | None
    ) -> Property | None:
        """Perform a command whose result is the driver's echo of a vector.

        The narrowing is a runtime check rather than a cast because the thing it
        would be asserting is that :data:`COMMAND_RULES` and :meth:`_perform`
        agree about what kind of command this is. If they ever stop agreeing,
        saying so is worth more than a silent reinterpretation.
        """
        result = await self._execute(command, timeout=timeout)
        if isinstance(result, bool):
            raise SafetyViolation(
                f"{command.describe()} was performed as a mount command, which "
                "acknowledges rather than returning a property vector. The command "
                "registry and Executor._perform disagree about what this command is."
            )
        return result

    async def _execute_mount(self, command: Command, *, timeout: float | None) -> bool:
        """Perform a mount command and report whether Ekos says it was sent."""
        result = await self._execute(command, timeout=timeout)
        if not isinstance(result, bool):
            raise SafetyViolation(
                f"{command.describe()} was performed as a property write rather than "
                "through the Ekos mount module. The command registry and "
                "Executor._perform disagree about what this command is."
            )
        return result

    async def _execute(self, command: Command, *, timeout: float | None) -> CommandResult:
        """Validate ``command`` and perform it.

        Raises:
            SafetyViolation: if the governor rejects it. Nothing is sent.
        """
        approval = self._governor.approve(command)
        return await self._perform(approval, timeout=timeout)

    async def _perform(self, approval: Approval, *, timeout: float | None) -> CommandResult:
        """Carry out an approved command. The only method that moves anything.

        Raises:
            SafetyViolation: if handed anything but a governor-issued approval.
        """
        if not isinstance(approval, Approval):
            raise SafetyViolation(
                "the executor performs Approval objects only; a raw command has not "
                "been through the safety governor (SPEC section 9)"
            )
        command = approval.command
        logger.info(
            "executing approved command",
            extra={"command_type": type(command).__name__, "command": command.describe()},
        )
        match command:
            case ConnectDevice(device=device):
                await self._client.connect_device(device, timeout=timeout)
                return self._client.get(device, "CONNECTION")
            case DisconnectDevice(device=device):
                await self._client.disconnect_device(device, timeout=timeout)
                return self._client.get(device, "CONNECTION")
            case SetProperty(device=device, property=name, values=values):
                return await self._client.write(device, name, values, timeout=timeout)
            case SlewToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg):
                return await self._mount().mount_slew(ra_hours, dec_deg)
            case ParkMount():
                return await self._mount().mount_park()
            case UnparkMount():
                return await self._mount().mount_unpark()
            case AbortMotion():
                return await self._mount().mount_abort()
            case _:
                # The governor rejects unregistered command types, so reaching
                # here means the registry and this dispatch disagree.
                raise SafetyViolation(
                    f"the executor does not know how to perform {type(command).__name__}; "
                    "it is registered with the governor but not implemented here"
                )
