"""Mount bring-up and the standing slew-rate ceiling — SPEC sections 5.1 and 9.

Three things measured on the reference rig (docs/FIELD-NOTES-M1.md) make mount
bring-up more than "set CONNECTION.CONNECT and hope":

* **The driver starts at 9600 baud** (section 2.2). The configured rate has to
  be selected on every connection, and it has to happen before CONNECT, or the
  link comes up at the wrong speed and fails in a way that reads like a cable
  fault.
* **The driver finds its own port** (section 2.3). ``indi_eqmod`` fills
  ``DEVICE_PORT.PORT`` with the correct ``/dev/serial/by-id/`` path unprompted.
  What the operator writes in ``equipment.yaml`` is an override for the case
  where that goes wrong, not the first source of truth.
* **The driver starts at 800x sidereal** (section 3.2). Every time. This is not
  a setting that is made once and stays made: a driver restart brings it back,
  and a reconnection that silently restores 800x is a safety regression, not a
  configuration nicety.

So this module owns the sequence, and it owns it for the life of the session:
:class:`MountLink` watches the transport and re-applies the ceiling every time
the mount transitions from disconnected to connected, whatever caused it.

Nothing here touches the instrument directly. Every write goes through
:class:`~nocturne.executor.executor.Executor`, and therefore through the safety
governor (CLAUDE.md invariant 1). This module holds no transport handle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Final, final

from nocturne.devices import require_configured_port
from nocturne.executor.executor import Executor
from nocturne.executor.indi.client import (
    CONNECT_ELEMENT,
    CONNECTION_PROPERTY,
    DeviceVanished,
    IndiEvent,
    PropertyChanged,
    ServerDisconnected,
    switch_values,
)
from nocturne.executor.indi.protocol import Property
from nocturne.schemas.equipment import Mount

logger = logging.getLogger("nocturne.executor.mount")

#: Sidereal rate, arcseconds of sky per second of time. A property of the
#: Earth's rotation, not a tunable threshold, so it is a constant rather than a
#: configuration value. The reference mount reports exactly this in
#: TELESCOPE_TRACK_RATE.TRACK_RATE_RA — see the recorded property dump at
#: backend/tests/fixtures/hardware/wave150i-properties.txt.
SIDEREAL_ARCSEC_PER_SECOND: Final = 15.04106717867020393

ARCSEC_PER_DEGREE: Final = 3600.0

#: The eqmod properties this module drives. Names as the driver defines them.
BAUD_PROPERTY: Final = "DEVICE_BAUD_RATE"
PORT_PROPERTY: Final = "DEVICE_PORT"
PORT_ELEMENT: Final = "PORT"
SLEW_SPEEDS_PROPERTY: Final = "SLEWSPEEDS"
RA_SLEW_ELEMENT: Final = "RASLEW"
DE_SLEW_ELEMENT: Final = "DESLEW"


class MountBringUpError(Exception):
    """The mount cannot be brought up as configured.

    Raised in preference to connecting anyway and finding out later. Every
    message names what was expected and what the driver actually offered.
    """


def sidereal_multiple(deg_per_second: float) -> int:
    """The largest whole sidereal multiple that stays within ``deg_per_second``.

    ``equipment.yaml`` states the ceiling in degrees per second because that is
    what the operator can reason about with a tape measure and a tripod;
    ``SLEWSPEEDS`` wants a multiple of the sidereal rate. This converts, and it
    rounds *down*, because rounding up would exceed the limit the operator set.

    Raises:
        MountBringUpError: if the limit is not positive, or is so low that no
            whole multiple fits inside it. There is no safe value to write in
            that case, and clamping to 1x would exceed the stated limit.
    """
    if deg_per_second <= 0.0 or not math.isfinite(deg_per_second):
        raise MountBringUpError(
            f"slew_rate_max_deg_s is {deg_per_second}; it must be a positive number"
        )
    multiple = int((deg_per_second * ARCSEC_PER_DEGREE) // SIDEREAL_ARCSEC_PER_SECOND)
    if multiple < 1:
        raise MountBringUpError(
            f"slew_rate_max_deg_s of {deg_per_second} is below one multiple of the "
            f"sidereal rate ({SIDEREAL_ARCSEC_PER_SECOND / ARCSEC_PER_DEGREE:.6f} deg/s), "
            "which is the smallest speed SLEWSPEEDS can express. Raise the limit or "
            "the mount cannot be driven within it."
        )
    return multiple


@final
class MountLink:
    """Brings one mount up as configured, and keeps its slew ceiling applied.

    The INDI device name comes from ``equipment.yaml``
    (``mount.indi_device_name``) and not from a constant here: ``indi_eqmod``
    announces itself as "EQMod Mount", which is the driver's name for the
    device and is equipment-dependent, so it is configuration (CLAUDE.md
    section 6). ``device_label`` is the operator's name for the same object and
    is never used to address it.
    """

    def __init__(self, executor: Executor, config: Mount) -> None:
        self._executor = executor
        self._config = config
        self._device = config.indi_device_name
        # Converted once, at construction, so a limit the driver cannot express
        # fails before anything is connected rather than after.
        self._slew_multiple = sidereal_multiple(config.slew_rate_max_deg_s)
        self._lock = asyncio.Lock()
        self._was_connected = False
        # True while bring_up() is driving. The watcher stands down: bring_up
        # performs the connect and the ceiling itself, in order, and a second
        # application racing it would write the same values twice and log a
        # spurious failure if it lost the race to a vector not yet defined.
        self._bringing_up = False
        self._port_in_use: str | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ state

    @property
    def device(self) -> str:
        """The INDI device name this link drives."""
        return self._device

    @property
    def slew_speed_multiple(self) -> int:
        """The configured ceiling, in multiples of the sidereal rate."""
        return self._slew_multiple

    @property
    def port_in_use(self) -> str | None:
        """The port the mount was last brought up on, driver-reported or configured."""
        return self._port_in_use

    @property
    def is_watching(self) -> bool:
        """Whether the ceiling is being re-applied on reconnection."""
        return self._unsubscribe is not None

    # -------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> MountLink:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Stop watching and wait for any re-application in flight."""
        self.stop_watching()
        tasks, self._tasks = list(self._tasks), set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def bring_up(self, *, timeout: float | None = None) -> None:
        """Configure the link, connect the mount, and apply the slew ceiling.

        Order is the whole point. The baud rate and the port are set while the
        mount is disconnected, because the driver reads them at CONNECT;
        SLEWSPEEDS is written afterwards, because the driver does not define it
        until it has spoken to the controller.

        Watching starts here and stays on afterwards, but the watcher stands
        down for the duration: this method performs the connect and then the
        ceiling, in that order, and a re-application triggered by its own
        connect would be racing it. If the driver dies mid-bring-up, the awaits
        below fail and the caller hears about it, which is the right outcome —
        a half-finished bring-up should not be quietly retried underneath.

        Raises:
            MissingSerialDeviceError: if a configured port is not present.
            MountBringUpError: if the driver does not offer what is configured.
            SafetyViolation: if the governor refuses one of the writes.
        """
        self.watch()
        self._bringing_up = True
        try:
            await self._executor.wait_for_property(
                self._device, CONNECTION_PROPERTY, timeout=timeout
            )
            await self._select_baud_rate(timeout=timeout)
            await self._select_port(timeout=timeout)
            await self._executor.connect_device(self._device, timeout=timeout)
            await self.apply_slew_limit(timeout=timeout)
        finally:
            # Whatever happened, hand the watcher an accurate starting point:
            # it re-applies on a transition, so a wrong baseline here would
            # either fire spuriously or miss the next real reconnection.
            self._was_connected = self._executor.is_device_connected(self._device)
            self._bringing_up = False
        logger.info(
            "mount is up",
            extra={
                "device": self._device,
                "port": self._port_in_use,
                "baud": self._config.baud,
                "slew_multiple": self._slew_multiple,
            },
        )

    # ----------------------------------------------------------------- pieces

    async def _select_baud_rate(self, *, timeout: float | None) -> None:
        """Set the configured baud rate. Must happen before CONNECT (section 2.2)."""
        prop = await self._executor.wait_for_property(
            self._device, BAUD_PROPERTY, timeout=timeout
        )
        wanted = str(self._config.baud)
        if wanted not in prop.elements:
            raise MountBringUpError(
                f"{self._device} does not offer {wanted} baud. "
                f"It offers: {', '.join(sorted(prop.elements))}. "
                "Correct mount.baud in config/equipment.yaml."
            )
        await self._executor.set_property(
            self._device,
            BAUD_PROPERTY,
            switch_values(wanted, tuple(prop.elements)),
            timeout=timeout,
        )

    async def _select_port(self, *, timeout: float | None) -> None:
        """Use the driver's port unless the configuration overrides it (section 2.3)."""
        # An override naming a device that is not there is a hard stop, before
        # the driver is told anything. Falling back to what the driver found
        # would mean using a different mount from the one Nocturne was told to
        # use, which is how a cable in the wrong socket goes unnoticed.
        require_configured_port(self._config.port, label=self._config.device_label)
        prop = await self._executor.wait_for_property(
            self._device, PORT_PROPERTY, timeout=timeout
        )
        reported = prop.get(PORT_ELEMENT)
        reported_port = reported.strip() if isinstance(reported, str) else ""
        configured = self._config.port

        if configured is None:
            if not reported_port:
                raise MountBringUpError(
                    f"{self._device} did not report a port and none is configured. "
                    "The driver normally fills DEVICE_PORT.PORT itself; if it has not, "
                    "check that the mount is plugged in and that the account is in the "
                    "dialout group, or set mount.port in config/equipment.yaml."
                )
            self._port_in_use = reported_port
            logger.info(
                "using the port the driver reports",
                extra={"device": self._device, "port": reported_port},
            )
            return

        self._port_in_use = configured
        if configured == reported_port:
            return
        logger.info(
            "overriding the driver's port with the configured one",
            extra={
                "device": self._device,
                "driver_port": reported_port,
                "configured_port": configured,
            },
        )
        await self._executor.set_property(
            self._device, PORT_PROPERTY, {PORT_ELEMENT: configured}, timeout=timeout
        )

    async def apply_slew_limit(self, *, timeout: float | None = None) -> Property | None:
        """Write the configured ceiling into SLEWSPEEDS (section 3.2).

        Waits for the vector: the driver defines it only once it has read the
        controller, so it is not in the cache at the moment CONNECT returns.
        """
        async with self._lock:
            prop = await self._executor.wait_for_property(
                self._device, SLEW_SPEEDS_PROPERTY, timeout=timeout
            )
            missing = sorted({RA_SLEW_ELEMENT, DE_SLEW_ELEMENT} - set(prop.elements))
            if missing:
                raise MountBringUpError(
                    f"{self._device}.{SLEW_SPEEDS_PROPERTY} has no element(s) "
                    f"{', '.join(missing)}; it has: {', '.join(sorted(prop.elements))}. "
                    "The slew rate ceiling cannot be applied and the mount must not "
                    "be driven."
                )
            result = await self._executor.set_property(
                self._device,
                SLEW_SPEEDS_PROPERTY,
                {
                    RA_SLEW_ELEMENT: float(self._slew_multiple),
                    DE_SLEW_ELEMENT: float(self._slew_multiple),
                },
                timeout=timeout,
            )
            logger.info(
                "slew rate ceiling applied",
                extra={
                    "device": self._device,
                    "limit_deg_s": self._config.slew_rate_max_deg_s,
                    "sidereal_multiple": self._slew_multiple,
                },
            )
            return result

    # --------------------------------------------------------------- watching

    def watch(self) -> None:
        """Re-apply the ceiling whenever the mount reconnects. Idempotent."""
        if self._unsubscribe is not None:
            return
        self._unsubscribe = self._executor.subscribe(self._on_event)

    def stop_watching(self) -> None:
        """Stop re-applying the ceiling."""
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def _on_event(self, event: IndiEvent) -> None:
        """React to the transport. Runs in the reader task, so it must not block.

        The rule is one line long on purpose: every transition of this device
        from not-connected to connected re-applies the ceiling. It does not
        matter whether the cause was a driver restart, a server reconnection or
        the initial bring-up — a freshly started driver is at 800x, and this is
        what puts it back.
        """
        match event:
            case PropertyChanged(property=prop) if (
                prop.device == self._device and prop.name == CONNECTION_PROPERTY
            ):
                if self._bringing_up:
                    return
                connected = prop.get(CONNECT_ELEMENT) is True
                if connected and not self._was_connected:
                    self._spawn(self._reapply())
                self._was_connected = connected
            case DeviceVanished(device=device) if device == self._device:
                self._was_connected = False
            case ServerDisconnected():
                self._was_connected = False
            case _:
                return

    async def _reapply(self) -> None:
        """Re-apply the ceiling, logging rather than raising into a bare task."""
        try:
            await self.apply_slew_limit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Nothing above this can catch it, and a mount left at 800x must be
            # visible in the log. The session layer sees the same reconnection
            # events and is what decides to park (SPEC section 9.4).
            logger.error(
                "could not re-apply the slew rate ceiling after a reconnection; "
                "the mount may be at its default of 800x sidereal",
                extra={"device": self._device, "reason": str(exc)},
            )

    def _spawn(self, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coroutine, name=f"mount-slew-limit-{self._device}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


__all__ = [
    "BAUD_PROPERTY",
    "DE_SLEW_ELEMENT",
    "PORT_ELEMENT",
    "PORT_PROPERTY",
    "RA_SLEW_ELEMENT",
    "SIDEREAL_ARCSEC_PER_SECOND",
    "SLEW_SPEEDS_PROPERTY",
    "MountBringUpError",
    "MountLink",
    "sidereal_multiple",
]
