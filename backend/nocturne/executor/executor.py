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
from collections.abc import Mapping
from types import TracebackType
from typing import final

from nocturne.executor.ekos import EkosBridge
from nocturne.executor.indi.client import IndiClient
from nocturne.executor.indi.protocol import ElementValue, Property
from nocturne.safety import (
    Approval,
    Command,
    ConnectDevice,
    DisconnectDevice,
    SafetyGovernor,
    SafetyViolation,
    SetProperty,
)

logger = logging.getLogger("nocturne.executor")


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

    @property
    def client(self) -> IndiClient:
        """The INDI client, for reads. It has no unvalidated mutating callers."""
        return self._client

    @property
    def ekos(self) -> EkosBridge | None:
        """The Ekos bridge, if one was supplied."""
        return self._ekos

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

    # -------------------------------------------------------------- commands

    async def connect_device(
        self, device: str, *, timeout: float | None = None
    ) -> Property | None:
        """Connect ``device``, if the governor permits it."""
        return await self._execute(ConnectDevice(device=device), timeout=timeout)

    async def disconnect_device(
        self, device: str, *, timeout: float | None = None
    ) -> Property | None:
        """Disconnect ``device``, if the governor permits it."""
        return await self._execute(DisconnectDevice(device=device), timeout=timeout)

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
        return await self._execute(command, timeout=timeout)

    async def _execute(self, command: Command, *, timeout: float | None) -> Property | None:
        """Validate ``command`` and perform it.

        Raises:
            SafetyViolation: if the governor rejects it. Nothing is sent.
        """
        approval = self._governor.approve(command)
        return await self._perform(approval, timeout=timeout)

    async def _perform(self, approval: Approval, *, timeout: float | None) -> Property | None:
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
            case _:
                # The governor rejects unregistered command types, so reaching
                # here means the registry and this dispatch disagree.
                raise SafetyViolation(
                    f"the executor does not know how to perform {type(command).__name__}; "
                    "it is registered with the governor but not implemented here"
                )
