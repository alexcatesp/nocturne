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
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager

from dbus_next import BusType, PropertyAccess
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, dbus_property, method

from nocturne.executor.ekos import (
    EKOS_ALIGN_INTERFACE,
    EKOS_ALIGN_PATH,
    EKOS_INTERFACE,
    EKOS_MOUNT_INTERFACE,
    EKOS_MOUNT_PATH,
    EKOS_PATH,
    INDI_INTERFACE,
    INDI_PATH,
    KSTARS_BUS_NAME,
    KSTARS_INTERFACE,
    KSTARS_PATH,
    close_bus,
)
from nocturne.safety.sky import JULIAN_DATE_AT_UNIX_EPOCH, SECONDS_PER_DAY

DEFAULT_DEVICES = (
    "Telescope Simulator",
    "CCD Simulator",
    "Guide Simulator",
    "Focuser Simulator",
    "Filter Simulator",
)


class FakeEkosInterface(ServiceInterface):
    """The subset of org.kde.kstars.Ekos that the bridge calls.

    ``start`` and ``stop`` do not merely record the call: they export and
    withdraw the module objects, because that is the behaviour the recorded
    captures show and it is the whole reason ``attach_modules`` exists. A stub
    that exported ``/KStars/Ekos/Mount`` from the beginning would let the bridge
    attach to a module before Ekos was started, which the real KStars will never
    allow, and the test would prove nothing about the order.
    """

    def __init__(
        self,
        on_start: "Callable[[], None] | None" = None,
        on_stop: "Callable[[], None] | None" = None,
    ) -> None:
        super().__init__(EKOS_INTERFACE)
        self.calls: list[str] = []
        self._on_start = on_start
        self._on_stop = on_stop

    @method()
    def start(self):
        self.calls.append("start")
        if self._on_start is not None:
            self._on_start()

    @method()
    def stop(self):
        self.calls.append("stop")
        if self._on_stop is not None:
            self._on_stop()

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


class FakeMountInterface(ServiceInterface):
    """The subset of org.kde.kstars.Ekos.Mount that the bridge calls.

    Every signature is the recorded one — ``slew(d, d) -> b`` and three
    no-argument calls returning a boolean — and
    ``test_ekos_recorded_interface.py`` asserts as much against
    ``hardware/kstars-dbus-interfaces.xml`` rather than against this file.

    ``slew`` records what it was handed rather than pretending to move, so a
    test can assert the right ascension arrived in hours and was not silently
    multiplied by fifteen somewhere.
    """

    def __init__(self) -> None:
        super().__init__(EKOS_MOUNT_INTERFACE)
        self.calls: list[str] = []
        #: Every (RA hours, DEC degrees) pair this mount has been asked for.
        self.slews: list[tuple[float, float]] = []
        #: The same, for syncs. Kept apart so a test can say which happened.
        self.syncs: list[tuple[float, float]] = []
        #: Names passed to gotoTarget and syncTarget.
        self.named: list[str] = []
        #: What setMeridianFlipValues was last told: (activate, degrees).
        self.flip: tuple[bool, float] | None = None
        #: Where this mount says it is: (RA hours, DEC degrees).
        self.position = (0.0, 0.0)
        #: ISD::ParkStatus. 0 is PARK_UNKNOWN, which is what a driver that has
        #: not spoken yet reports, and it must not read as parked.
        self.park_status = 0
        #: KStars refuses a model reset on a mount that has no model.
        self.has_alignment_model = True
        #: What the next call should return. KStars returns false when it could
        #: not send the command, and nothing above may read that as success.
        self.accepts = True

    @method()
    def slew(self, RA: "d", DEC: "d") -> "b":  # noqa: F821, N803
        self.calls.append("slew")
        self.slews.append((RA, DEC))
        return self.accepts

    @method()
    def park(self) -> "b":  # noqa: F821
        self.calls.append("park")
        return self.accepts

    @method()
    def unpark(self) -> "b":  # noqa: F821
        self.calls.append("unpark")
        return self.accepts

    @method()
    def abort(self) -> "b":  # noqa: F821
        self.calls.append("abort")
        return self.accepts

    @method()
    def sync(self, RA: "d", DEC: "d") -> "b":  # noqa: F821, N803
        self.calls.append("sync")
        self.syncs.append((RA, DEC))
        return self.accepts

    @method()
    def gotoTarget(self, target: "s") -> "b":  # noqa: F821, N802
        self.calls.append("gotoTarget")
        self.named.append(target)
        return self.accepts

    @method()
    def syncTarget(self, target: "s") -> "b":  # noqa: F821, N802
        self.calls.append("syncTarget")
        self.named.append(target)
        return self.accepts

    @method()
    def resetModel(self) -> "b":  # noqa: F821, N802
        self.calls.append("resetModel")
        return self.accepts and self.has_alignment_model

    @method()
    def setMeridianFlipValues(self, activate: "b", hours: "d"):  # noqa: F821, N802
        # The argument is named `hours` because KStars names it that. It is in
        # degrees. That defect is the point of the test built on this stub.
        self.calls.append("setMeridianFlipValues")
        self.flip = (activate, hours)

    @dbus_property(access=PropertyAccess.READ)
    def equatorialCoords(self) -> "ad":  # noqa: F821, N802
        return list(self.position)

    @dbus_property(access=PropertyAccess.READ)
    def parkStatus(self) -> "i":  # noqa: F821, N802
        return self.park_status


#: One catalogue entry, as KStars' two lookup documents describe it. Degrees for
#: right ascension, because that is what KStars reports — Mount.slew takes hours,
#: and the conversion between them is the bridge's job and is tested for.
class CatalogueEntry:
    def __init__(
        self, name: str, ra_degrees: float, dec_degrees: float, type_name: str
    ) -> None:
        self.name = name
        self.ra_degrees = ra_degrees
        self.dec_degrees = dec_degrees
        self.type_name = type_name


#: A handful of objects, each chosen for what it lets a test say. M31 and M13 are
#: ordinary deep-sky targets at very different declinations; Jupiter is here to
#: be refused for moving; Polaris is circumpolar.
DEFAULT_CATALOGUE = (
    CatalogueEntry("M 31", 10.6847, 41.269, "Galaxy"),
    CatalogueEntry("M 13", 250.423, 36.4613, "Globular Cluster"),
    CatalogueEntry("M 42", 83.8221, -5.3911, "Nebula"),
    CatalogueEntry("Jupiter", 45.0, 15.0, "Planet"),
    CatalogueEntry("Polaris", 37.9529, 89.2641, "Star"),
)


class FakeKStarsInterface(ServiceInterface):
    """The subset of org.kde.kstars the bridge calls: the catalogue lookups.

    Both methods return XML built the way ``kstarsdbus.cpp`` builds it, including
    the two things that are easy to get wrong and cost a telescope its target:
    right ascension is in **degrees** in these documents while ``Mount.slew``
    takes **hours**, and the clock is reported as a Julian date printed to three
    decimal places, which quantises it to 86.4 seconds.
    """

    def __init__(self, catalogue: Sequence[CatalogueEntry] = DEFAULT_CATALOGUE) -> None:
        super().__init__(KSTARS_INTERFACE)
        self.catalogue = {entry.name.upper(): entry for entry in catalogue}
        #: The Julian date this KStars' clock reads. Defaults to now, as a
        #: running KStars' does, and is settable because a KStars whose
        #: simulated clock has been stopped or moved is exactly what the
        #: resolver's freshness rule exists to catch.
        self.julian_date = JULIAN_DATE_AT_UNIX_EPOCH + time.time() / SECONDS_PER_DAY
        self.lookups: list[str] = []
        #: When set, both lookups return this instead of a real document. For
        #: testing what the bridge does with an answer that is not the one
        #: KStars documents — which is not hypothetical: an answer whose shape
        #: changed under a KStars upgrade looks exactly like this.
        self.broken: str | None = None

    def _find(self, name: str) -> "CatalogueEntry | None":
        self.lookups.append(name)
        return self.catalogue.get(name.upper())

    @method()
    def getObjectPositionInfo(self, objectName: "s") -> "s":  # noqa: F821, N802, N803
        if self.broken is not None:
            return self.broken
        entry = self._find(objectName)
        if entry is None:
            return "<xml></xml>"
        return (
            "<object>"
            f"<Name>{entry.name}</Name>"
            f"<AltAz_JD>{self.julian_date:.3f}</AltAz_JD>"
            f"<RA_Degrees>{entry.ra_degrees}</RA_Degrees>"
            f"<Dec_Degrees>{entry.dec_degrees}</Dec_Degrees>"
            "</object>"
        )

    @method()
    def getObjectDataXML(self, objectName: "s") -> "s":  # noqa: F821, N802, N803
        if self.broken is not None:
            return self.broken
        entry = self._find(objectName)
        if entry is None:
            return "<xml></xml>"
        return (
            "<object>"
            f"<Name>{entry.name}</Name>"
            f"<Type>{entry.type_name}</Type>"
            f"<RA_Degrees>{entry.ra_degrees}</RA_Degrees>"
            f"<Dec_Degrees>{entry.dec_degrees}</Dec_Degrees>"
            "</object>"
        )


class FakeAlignInterface(ServiceInterface):
    """The subset of org.kde.kstars.Ekos.Align that the bridge calls.

    Two things about it are copied from KStars' implementation rather than from
    its documentation, because the documentation is wrong about both:

    ``getSolutionResult`` returns **three** doubles — orientation, then right
    ascension, then declination — where the doxygen comment on the declaration
    says the first is the right ascension. A stub that returned two would let
    the bridge read them in the wrong order and pass.

    The solution is in **degrees** and in **J2000**, while ``setTargetCoords``
    takes hours and J2000 and the mount takes hours and the equinox of date.
    Three conventions on two interfaces, so this stub speaks the ones KStars
    speaks and leaves the converting to the code under test.
    """

    def __init__(self) -> None:
        super().__init__(EKOS_ALIGN_INTERFACE)
        self.calls: list[str] = []
        #: Every goto mode the bridge has set. The whole safety argument for the
        #: align module is that this is only ever GOTO_NOTHING, so a test needs
        #: to be able to read the sequence rather than the last value.
        self.solver_actions: list[int] = []
        self.solver_modes: list[int] = []
        self.binning_indexes: list[int] = []
        #: (RA hours J2000, DEC degrees J2000) pairs handed to setTargetCoords.
        self.targets: list[tuple[float, float]] = []
        self.position_angles: list[float] = []
        #: What the solver last found: orientation, RA degrees, DEC degrees.
        #: INVALID_VALUE-ish by default, as the fields are before a solve.
        self.solution = [0.0, float("nan"), float("nan")]
        self.target = [0.0, 0.0]
        self.align_state = 0
        self.load_and_slew_state = 0
        self.camera = [3008.0, 3008.0, 3.76, 3.76]
        self.telescope = [1000.0, 200.0]
        self.accepts = True

    @method()
    def captureAndSolve(self) -> "b":  # noqa: F821, N802
        self.calls.append("captureAndSolve")
        return self.accepts

    @method()
    def abort(self):
        self.calls.append("abort")

    @method()
    def setSolverAction(self, mode: "i"):  # noqa: F821, N802
        self.calls.append("setSolverAction")
        self.solver_actions.append(mode)

    @method()
    def setSolverMode(self, mode: "u"):  # noqa: F821, N802
        self.calls.append("setSolverMode")
        self.solver_modes.append(mode)

    @method()
    def setTargetCoords(self, ra: "d", de: "d"):  # noqa: F821, N802
        self.calls.append("setTargetCoords")
        self.targets.append((ra, de))

    @method()
    def setTargetPositionAngle(self, value: "d"):  # noqa: F821, N802
        self.calls.append("setTargetPositionAngle")
        self.position_angles.append(value)

    @method()
    def setBinningIndex(self, binningIndex: "i"):  # noqa: F821, N802, N803
        self.calls.append("setBinningIndex")
        self.binning_indexes.append(binningIndex)

    @method()
    def getSolutionResult(self) -> "ad":  # noqa: F821, N802
        return list(self.solution)

    @method()
    def getTargetCoords(self) -> "ad":  # noqa: F821, N802
        return list(self.target)

    @method()
    def getLoadAndSlewStatus(self) -> "i":  # noqa: F821, N802
        return self.load_and_slew_state

    @method()
    def cameraInfo(self) -> "ad":  # noqa: F821, N802
        return list(self.camera)

    @method()
    def telescopeInfo(self) -> "ad":  # noqa: F821, N802
        return list(self.telescope)

    @dbus_property(access=PropertyAccess.READ)
    def status(self) -> "i":  # noqa: F821
        return self.align_state


class MinimalMountInterface(ServiceInterface):
    """A mount module missing methods the bridge needs, to test the refusal."""

    def __init__(self) -> None:
        super().__init__(EKOS_MOUNT_INTERFACE)

    @method()
    def park(self) -> "b":  # noqa: F821
        return True


class MinimalEkosInterface(ServiceInterface):
    """An Ekos interface missing methods the bridge needs, to test the refusal."""

    def __init__(self) -> None:
        super().__init__(EKOS_INTERFACE)

    @method()
    def start(self):
        return None


class FakeKStars:
    """Owns the org.kde.kstars bus name and exports the stub objects.

    The module objects are **not** exported when the process starts. They appear
    only once ``Ekos.start`` is called and disappear again on ``Ekos.stop``,
    which is what the reference rig does: five exported paths at rest,
    twenty-four with Ekos running.
    """

    def __init__(
        self,
        bus_address: str,
        *,
        minimal: bool = False,
        minimal_mount: bool = False,
    ) -> None:
        self._bus_address = bus_address
        self._minimal = minimal
        self._minimal_mount = minimal_mount
        self._bus: MessageBus | None = None
        self.ekos: ServiceInterface = (
            MinimalEkosInterface()
            if minimal
            else FakeEkosInterface(
                on_start=self._export_modules, on_stop=self._withdraw_modules
            )
        )
        self.indi = FakeIndiInterface()
        self.kstars = FakeKStarsInterface()
        self.mount: ServiceInterface = (
            MinimalMountInterface() if minimal_mount else FakeMountInterface()
        )
        self.align = FakeAlignInterface()
        self._modules_exported = False

    async def start(self) -> None:
        self._bus = await MessageBus(
            bus_type=BusType.SESSION, bus_address=self._bus_address
        ).connect()
        self._bus.export(EKOS_PATH, self.ekos)
        if not self._minimal:
            self._bus.export(INDI_PATH, self.indi)
            self._bus.export(KSTARS_PATH, self.kstars)
        await self._bus.request_name(KSTARS_BUS_NAME)

    def _export_modules(self) -> None:
        """What Ekos does when it starts: create the module objects."""
        if self._bus is None or self._modules_exported:
            return
        self._bus.export(EKOS_MOUNT_PATH, self.mount)
        self._bus.export(EKOS_ALIGN_PATH, self.align)
        self._modules_exported = True

    def _withdraw_modules(self) -> None:
        """And what it does when it stops: take them away again."""
        if self._bus is None or not self._modules_exported:
            return
        self._bus.unexport(EKOS_MOUNT_PATH, self.mount)
        self._bus.unexport(EKOS_ALIGN_PATH, self.align)
        self._modules_exported = False

    @property
    def modules_exported(self) -> bool:
        """Whether the module objects are currently on the bus."""
        return self._modules_exported

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
