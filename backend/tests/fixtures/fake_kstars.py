"""A stub KStars/Ekos DBus service, and a private session bus to run it on.

The bridge is verified against this rather than against a real KStars: a
headless KStars is a Qt application with an X or offscreen platform plugin, and
starting one in CI would test Qt rather than Nocturne. What has to be right in
Nocturne's code — attaching, introspecting, refusing an interface that is not
what it expects, and reattaching when KStars restarts — is all exercised here.

**Every signature below is copied from the recorded interface**, not chosen.
``ekos_interfaces.py`` parses
``hardware/kstars-dbus-interfaces.xml``, and ``test_ekos_recorded_interface.py``
asserts that each method this stub exports has the arity and signature KStars
declares. That test exists because this file got it wrong once and could not
have known: it implemented ``INDI.connect(device: s)`` while KStars declares
``connect(host: s, port: i)``, agreeing with the bridge's misconception because
the same author wrote both. A stub that shares the assumptions of the code it
verifies proves the two are consistent and nothing more.

This module deliberately does not use ``from __future__ import annotations``:
dbus-next reads the method annotations as DBus signature strings, and the
member names are KStars', so they are camelCase rather than PEP 8.
"""

import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from dbus_next import BusType
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

from nocturne.executor.ekos import (
    EKOS_INTERFACE,
    EKOS_PATH,
    INDI_INTERFACE,
    INDI_PATH,
    KSTARS_BUS_NAME,
    close_bus,
)

DEFAULT_DEVICES = (
    "Telescope Simulator",
    "CCD Simulator",
    "Guide Simulator",
    "Focuser Simulator",
    "Filter Simulator",
)


class FakeEkosInterface(ServiceInterface):
    """The subset of org.kde.kstars.Ekos that the bridge calls."""

    def __init__(self) -> None:
        super().__init__(EKOS_INTERFACE)
        self.calls: list[str] = []

    @method()
    def start(self):
        self.calls.append("start")

    @method()
    def stop(self):
        self.calls.append("stop")

    @method()
    def connectDevices(self):  # noqa: N802
        self.calls.append("connectDevices")

    @method()
    def disconnectDevices(self):  # noqa: N802
        self.calls.append("disconnectDevices")


class FakeIndiInterface(ServiceInterface):
    """The subset of org.kde.kstars.INDI that the bridge calls.

    ``connect`` and ``disconnect`` take a **host and a port**, not a device
    name. They attach KStars' INDI client to an indiserver. This stub declared
    ``connect(device: s)`` until the interface was recorded, which made the
    bridge's ``connect_device()`` look correct in every test.
    """

    def __init__(self, devices: Sequence[str] = DEFAULT_DEVICES) -> None:
        super().__init__(INDI_INTERFACE)
        self.devices = list(devices)
        #: (host, port) pairs this INDI client has been told to attach to.
        self.servers: set[tuple[str, int]] = set()

    @method()
    def getDevices(self) -> "as":  # noqa: F722, N802
        return self.devices

    @method()
    def connect(self, host: "s", port: "i") -> "b":  # noqa: F821
        self.servers.add((host, port))
        return True

    @method()
    def disconnect(self, host: "s", port: "i") -> "b":  # noqa: F821
        self.servers.discard((host, port))
        return True


class MinimalEkosInterface(ServiceInterface):
    """An Ekos interface missing methods the bridge needs, to test the refusal."""

    def __init__(self) -> None:
        super().__init__(EKOS_INTERFACE)

    @method()
    def start(self):
        return None


class FakeKStars:
    """Owns the org.kde.kstars bus name and exports the two stub objects."""

    def __init__(self, bus_address: str, *, minimal: bool = False) -> None:
        self._bus_address = bus_address
        self._minimal = minimal
        self._bus: MessageBus | None = None
        self.ekos: ServiceInterface = MinimalEkosInterface() if minimal else FakeEkosInterface()
        self.indi = FakeIndiInterface()

    async def start(self) -> None:
        self._bus = await MessageBus(
            bus_type=BusType.SESSION, bus_address=self._bus_address
        ).connect()
        self._bus.export(EKOS_PATH, self.ekos)
        if not self._minimal:
            self._bus.export(INDI_PATH, self.indi)
        await self._bus.request_name(KSTARS_BUS_NAME)

    async def stop(self) -> None:
        """Leave the bus, as KStars does when it is killed."""
        if self._bus is not None:
            await close_bus(self._bus)
            await asyncio.sleep(0)
            self._bus = None


@asynccontextmanager
async def session_bus() -> AsyncIterator[str]:
    """Run a private dbus-daemon and yield its address."""
    if shutil.which("dbus-daemon") is None:  # pragma: no cover - environment guard
        raise RuntimeError("dbus-daemon is not installed")
    process = await asyncio.create_subprocess_exec(
        "dbus-daemon",
        "--session",
        "--print-address",
        "--nofork",
        "--nopidfile",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        address = line.decode().strip()
        if not address:
            raise RuntimeError("dbus-daemon did not print an address")
        yield address
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:  # pragma: no cover - defensive
            process.kill()
            await process.wait()


def dbus_available() -> bool:
    """Whether a private session bus can be started in this environment."""
    return shutil.which("dbus-daemon") is not None and os.name == "posix"
