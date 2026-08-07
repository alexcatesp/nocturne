"""Keeping a device configured — docs/FIELD-NOTES-M1.md sections 3.2 and 11.

Every driver on this rig comes up holding its own defaults, and none of them are
ours:

===================== ================================= =====================
Device                Driver default                    Configured
===================== ================================= =====================
``EQMod Mount``       ``SLEWSPEEDS`` 800x sidereal       3.0 deg/s (718x)
``ASI533MM Pro``      ``CCD_CONTROLS`` Gain 200 Off 1    gain profile, offset 1
``ASI120MM Mini``     ``CCD_CONTROLS`` Gain 50 Off 0     gain 50, offset 0
``ZWO EFW``           ``FILTER_NAME`` ZWO factory names  the wheel's real order
===================== ================================= =====================

It is one behaviour, not four. A driver restart brings the defaults back, so
applying them once at startup is not enough: the values have to be re-applied on
every transition from disconnected to connected, whatever caused it.

So the mechanism lives here once, and a device is described by *what to write
and when* rather than by a class of its own. A device added later gets the
protection by construction — the alternative is four copies of the same watcher
and a fifth device that quietly misses out.

Two phases, because the ordering matters and differs. ``before_connect`` runs
while the device is disconnected: the mount's baud rate belongs there, since the
driver reads it at CONNECT and a rate set afterwards arrives too late.
``after_connect`` runs once connected and is what gets re-applied — a driver
does not define ``SLEWSPEEDS`` or ``CCD_CONTROLS`` until it has spoken to the
hardware.

Nothing here touches the instrument directly. Every write goes through
:class:`~nocturne.executor.executor.Executor`, and therefore through the safety
governor (CLAUDE.md invariant 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine, Mapping, Sequence
from types import TracebackType
from typing import Protocol, final

from nocturne.executor.executor import Executor
from nocturne.executor.indi.client import (
    CONNECT_ELEMENT,
    CONNECTION_PROPERTY,
    DeviceVanished,
    IndiEvent,
    PropertyChanged,
    ServerDisconnected,
)
from nocturne.executor.indi.protocol import ElementValue

logger = logging.getLogger("nocturne.executor.link")


class DeviceBringUpError(Exception):
    """The device cannot be brought up as configured.

    Raised in preference to connecting anyway and finding out later. Every
    message names what was expected and what the driver actually offered.
    """


class Step(Protocol):
    """One thing to do to a device, in order.

    A callable rather than a data structure because the interesting steps are
    not static: the mount's baud step has to read the element names the driver
    offers before it can write one, and the port step decides between the
    configured value and the driver's own.
    """

    def __call__(
        self, executor: Executor, device: str, *, timeout: float | None
    ) -> Coroutine[object, object, None]:
        """Perform the step against ``device``."""


def write_values(
    property_name: str,
    values: Mapping[str, ElementValue],
    *,
    what: str,
) -> Step:
    """A step that writes fixed values into one property vector.

    Refuses rather than writes a subset when the driver does not offer an
    element: a partial write leaves the device in a state nobody configured, and
    is exactly the sort of thing that is only noticed in the data.
    """

    async def step(executor: Executor, device: str, *, timeout: float | None) -> None:
        prop = await executor.wait_for_property(device, property_name, timeout=timeout)
        missing = sorted(set(values) - set(prop.elements))
        if missing:
            raise DeviceBringUpError(
                f"{device}.{property_name} has no element(s) {', '.join(missing)}; "
                f"it has: {', '.join(sorted(prop.elements))}. "
                f"{what} cannot be applied and the device must not be used."
            )
        await executor.set_property(device, property_name, values, timeout=timeout)
        logger.info(
            "applied configured values",
            extra={"device": device, "property": property_name, "what": what},
        )

    return step


@final
class DeviceLink:
    """Brings one device up as configured, and keeps it that way.

    The rule the watcher follows is one line long on purpose: every transition
    of this device from not-connected to connected re-runs the ``after_connect``
    steps. It does not matter whether the cause was a driver restart, a server
    reconnection or the initial bring-up — a freshly started driver holds its
    own defaults, and this is what replaces them.
    """

    def __init__(
        self,
        executor: Executor,
        *,
        device: str,
        before_connect: Sequence[Step] = (),
        after_connect: Sequence[Step] = (),
        label: str | None = None,
    ) -> None:
        self._executor = executor
        self._device = device
        self._before_connect = tuple(before_connect)
        self._after_connect = tuple(after_connect)
        self._label = label or device
        self._lock = asyncio.Lock()
        self._was_connected = False
        self._bringing_up = False
        self._unsubscribe: Callable[[], None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ state

    @property
    def device(self) -> str:
        """The INDI device name this link drives."""
        return self._device

    @property
    def label(self) -> str:
        """What to call it in a message a person reads."""
        return self._label

    @property
    def is_watching(self) -> bool:
        """Whether the configured values are being re-applied on reconnection."""
        return self._unsubscribe is not None

    # -------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> DeviceLink:
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
        """Configure the device, connect it, and apply what needs a connection.

        Watching starts here and stays on afterwards, but the watcher stands
        down for the duration: this method performs the connect and then the
        after-connect steps, in that order, and a re-application triggered by
        its own connect would be racing it. If the driver dies mid-bring-up, the
        awaits below fail and the caller hears about it — a half-finished
        bring-up should not be quietly retried underneath.

        Raises:
            DeviceBringUpError: if the driver does not offer what is configured.
            SafetyViolation: if the governor refuses one of the writes.
        """
        self.watch()
        self._bringing_up = True
        try:
            await self._executor.wait_for_property(
                self._device, CONNECTION_PROPERTY, timeout=timeout
            )
            for step in self._before_connect:
                await step(self._executor, self._device, timeout=timeout)
            await self._executor.connect_device(self._device, timeout=timeout)
            await self.apply(timeout=timeout)
        finally:
            # Whatever happened, hand the watcher an accurate starting point: it
            # re-applies on a transition, so a wrong baseline here would either
            # fire spuriously or miss the next real reconnection.
            self._was_connected = self._executor.is_device_connected(self._device)
            self._bringing_up = False
        logger.info("device is up", extra={"device": self._device, "label": self._label})

    async def apply(self, *, timeout: float | None = None) -> None:
        """Run the after-connect steps. Serialised against the watcher."""
        async with self._lock:
            for step in self._after_connect:
                await step(self._executor, self._device, timeout=timeout)

    # --------------------------------------------------------------- watching

    def watch(self) -> None:
        """Re-apply the configuration whenever the device reconnects. Idempotent."""
        if self._unsubscribe is not None:
            return
        self._unsubscribe = self._executor.subscribe(self._on_event)

    def stop_watching(self) -> None:
        """Stop re-applying."""
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def _on_event(self, event: IndiEvent) -> None:
        """React to the transport. Runs in the reader task, so it must not block."""
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
        """Re-apply, logging rather than raising into a bare task."""
        try:
            await self.apply()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Nothing above this can catch it, and a device left on its driver's
            # defaults must be visible in the log. The session layer sees the
            # same reconnection events and is what decides to park (SPEC 9.4).
            logger.error(
                "could not re-apply the configured values after a reconnection; "
                "the device may be on its driver's defaults",
                extra={"device": self._device, "reason": str(exc)},
            )

    def _spawn(self, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coroutine, name=f"device-reapply-{self._device}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


__all__ = [
    "DeviceBringUpError",
    "DeviceLink",
    "Step",
    "write_values",
]
