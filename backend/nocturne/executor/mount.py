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

So this module owns the mount's *steps*. The machinery that runs them — connect,
re-apply on every reconnection, stop cleanly — lives in
:mod:`nocturne.executor.link`, because the mount turned out not to be special:
the cameras and the filter wheel come up on their driver's defaults in exactly
the same way (docs/FIELD-NOTES-M1.md section 11). What is here is what is true of
this mount and no other device.

Nothing here touches the instrument directly. Every write goes through
:class:`~nocturne.executor.executor.Executor`, and therefore through the safety
governor (CLAUDE.md invariant 1). This module holds no transport handle.
"""

from __future__ import annotations

import logging
import math
from types import TracebackType
from typing import Final, final

from nocturne.devices import require_configured_port
from nocturne.executor.executor import Executor
from nocturne.executor.indi.client import (
    switch_values,
)
from nocturne.executor.link import DeviceBringUpError, DeviceLink
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


class MountBringUpError(DeviceBringUpError):
    """The mount cannot be brought up as configured.

    A :class:`~nocturne.executor.link.DeviceBringUpError`, so a caller handling
    bring-up failure in general catches this too. It keeps its own name because
    the mount's messages are the ones docs/hardware-setup.md quotes.
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
    """Brings the mount up as configured, and keeps its slew ceiling applied.

    A thin arrangement of mount-specific steps around a
    :class:`~nocturne.executor.link.DeviceLink`, which is what actually connects,
    watches and re-applies. The mount contributes three things no other device
    needs: a baud rate that must be set *before* CONNECT, a port that the driver
    usually knows better than we do, and a slew ceiling converted from degrees
    per second into multiples of the sidereal rate.

    The INDI device name comes from ``equipment.yaml``; ``device_label`` is the
    operator's name for the same object and is never used to address it.
    """

    def __init__(self, executor: Executor, config: Mount) -> None:
        self._executor = executor
        self._config = config
        # Converted once, at construction, so a limit the driver cannot express
        # fails before anything is connected rather than after.
        self._slew_multiple = sidereal_multiple(config.slew_rate_max_deg_s)
        self._port_in_use: str | None = None
        self._link = DeviceLink(
            executor,
            device=config.indi_device_name,
            before_connect=(self._select_baud_rate, self._select_port),
            after_connect=(self._apply_slew_limit,),
            label=config.device_label,
        )

    # ------------------------------------------------------------------ state

    @property
    def device(self) -> str:
        """The INDI device name this link drives."""
        return self._link.device

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
        return self._link.is_watching

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
        await self._link.aclose()

    def watch(self) -> None:
        """Re-apply the ceiling whenever the mount reconnects. Idempotent."""
        self._link.watch()

    def stop_watching(self) -> None:
        """Stop re-applying the ceiling."""
        self._link.stop_watching()

    async def bring_up(self, *, timeout: float | None = None) -> None:
        """Set the baud rate and port, connect, and apply the slew ceiling.

        Order is the whole point. The baud rate and the port are set while the
        mount is disconnected, because the driver reads them at CONNECT;
        SLEWSPEEDS is written afterwards, because the driver does not define it
        until it has spoken to the controller.

        Raises:
            MissingSerialDeviceError: if a configured port is not present.
            MountBringUpError: if the driver does not offer what is configured.
            SafetyViolation: if the governor refuses one of the writes.
        """
        await self._link.bring_up(timeout=timeout)
        logger.info(
            "mount is up",
            extra={
                "device": self.device,
                "port": self._port_in_use,
                "baud": self._config.baud,
                "slew_multiple": self._slew_multiple,
            },
        )

    async def apply_slew_limit(self, *, timeout: float | None = None) -> None:
        """Write the configured ceiling into SLEWSPEEDS (section 3.2)."""
        await self._link.apply(timeout=timeout)

    # ----------------------------------------------------------------- steps

    async def _select_baud_rate(
        self, executor: Executor, device: str, *, timeout: float | None
    ) -> None:
        """Set the configured baud rate. Must happen before CONNECT (section 2.2)."""
        prop = await executor.wait_for_property(device, BAUD_PROPERTY, timeout=timeout)
        wanted = str(self._config.baud)
        if wanted not in prop.elements:
            raise MountBringUpError(
                f"{device} does not offer {wanted} baud. "
                f"It offers: {', '.join(sorted(prop.elements))}. "
                "Correct mount.baud in config/equipment.yaml."
            )
        await executor.set_property(
            device, BAUD_PROPERTY, switch_values(wanted, tuple(prop.elements)), timeout=timeout
        )

    async def _select_port(
        self, executor: Executor, device: str, *, timeout: float | None
    ) -> None:
        """Use the driver's port unless the configuration overrides it (section 2.3)."""
        # An override naming a device that is not there is a hard stop, before
        # the driver is told anything. Falling back to what the driver found
        # would mean using a different mount from the one Nocturne was told to
        # use, which is how a cable in the wrong socket goes unnoticed.
        require_configured_port(self._config.port, label=self._config.device_label)

        prop = await executor.wait_for_property(device, PORT_PROPERTY, timeout=timeout)
        reported = prop.get(PORT_ELEMENT)
        reported_port = reported.strip() if isinstance(reported, str) else ""
        configured = self._config.port

        if configured is None:
            if not reported_port:
                raise MountBringUpError(
                    f"{device} did not report a port and none is configured. "
                    "The driver normally fills DEVICE_PORT.PORT itself; if it has not, "
                    "check that the mount is plugged in and that the account is in the "
                    "dialout group, or set mount.port in config/equipment.yaml."
                )
            self._port_in_use = reported_port
            logger.info(
                "using the port the driver reports",
                extra={"device": device, "port": reported_port},
            )
            return

        self._port_in_use = configured
        if configured == reported_port:
            return
        logger.info(
            "overriding the driver's port with the configured one",
            extra={
                "device": device,
                "driver_port": reported_port,
                "configured_port": configured,
            },
        )
        await executor.set_property(
            device, PORT_PROPERTY, {PORT_ELEMENT: configured}, timeout=timeout
        )

    async def _apply_slew_limit(
        self, executor: Executor, device: str, *, timeout: float | None
    ) -> None:
        """Write the configured ceiling into SLEWSPEEDS.

        Waits for the vector: the driver defines it only once it has read the
        controller, so it is not in the cache at the moment CONNECT returns.
        """
        prop = await executor.wait_for_property(device, SLEW_SPEEDS_PROPERTY, timeout=timeout)
        missing = sorted({RA_SLEW_ELEMENT, DE_SLEW_ELEMENT} - set(prop.elements))
        if missing:
            raise MountBringUpError(
                f"{device}.{SLEW_SPEEDS_PROPERTY} has no element(s) "
                f"{', '.join(missing)}; it has: {', '.join(sorted(prop.elements))}. "
                "The slew rate ceiling cannot be applied and the mount must not "
                "be driven."
            )
        await executor.set_property(
            device,
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
                "device": device,
                "limit_deg_s": self._config.slew_rate_max_deg_s,
                "sidereal_multiple": self._slew_multiple,
            },
        )


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
