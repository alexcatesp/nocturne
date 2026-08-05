"""M1 AUTO acceptance — SPEC section 14, M1.

    "AUTO: orchestrator connects to all five simulator drivers, reads and
    writes properties, survives driver restart, reconnects automatically."

These run against real ``indiserver`` and the real INDI simulator drivers, with
real processes killed underneath them (SPEC section 13.1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from nocturne.executor import Executor, IndiClient, IndiSettings
from nocturne.executor.indi.client import DeviceAppeared, DeviceVanished, IndiEvent
from nocturne.executor.indi.protocol import PropertyPermission, PropertyState
from nocturne.safety import SafetyGovernor, SafetyViolation
from nocturne.schemas import load_config_bundle
from tests.fixtures.indi_server import (
    SIMULATOR_DEVICES,
    SIMULATOR_DRIVERS,
    IndiServerProcess,
    simulators_available,
)

pytestmark = [
    pytest.mark.indi,
    pytest.mark.skipif(
        not simulators_available(),
        reason="indiserver and the INDI simulator drivers are not installed",
    ),
]

#: Real drivers are real processes: these timeouts are generous on purpose.
SETTINGS = IndiSettings(
    connect_timeout_s=15.0,
    property_timeout_s=30.0,
    write_timeout_s=45.0,
    device_connect_timeout_s=45.0,
    reconnect_initial_delay_s=0.25,
    reconnect_max_delay_s=2.0,
)

ALL_DEVICES = tuple(SIMULATOR_DEVICES.values())

#: A writable property per device class, with a value to write and read back.
WRITE_CASES: dict[str, tuple[str, dict[str, float | bool], str, float | bool]] = {
    "Focuser Simulator": (
        "ABS_FOCUS_POSITION",
        {"FOCUS_ABSOLUTE_POSITION": 40000.0},
        "FOCUS_ABSOLUTE_POSITION",
        40000.0,
    ),
    "Filter Simulator": (
        "FILTER_SLOT",
        {"FILTER_SLOT_VALUE": 3.0},
        "FILTER_SLOT_VALUE",
        3.0,
    ),
    "Telescope Simulator": (
        "TELESCOPE_SLEW_RATE",
        {"1x": False, "2x": False, "3x": True, "4x": False},
        "3x",
        True,
    ),
    "CCD Simulator": (
        "CCD_BINNING",
        {"HOR_BIN": 2.0, "VER_BIN": 2.0},
        "HOR_BIN",
        2.0,
    ),
    "Guide Simulator": (
        "CCD_BINNING",
        {"HOR_BIN": 2.0, "VER_BIN": 2.0},
        "VER_BIN",
        2.0,
    ),
}

#: A property each device class defines before it is connected.
IDENTITY_PROPERTY = "DRIVER_INFO"


@pytest.fixture(scope="module")
def indi_server() -> Iterator[IndiServerProcess]:
    with IndiServerProcess() as server:
        yield server


@pytest.fixture
async def client(indi_server: IndiServerProcess) -> AsyncIterator[IndiClient]:
    connected = IndiClient(SETTINGS.model_copy(update={"port": indi_server.port}))
    await connected.connect()
    try:
        yield connected
    finally:
        await connected.aclose()


@pytest.fixture
async def connected_client(client: IndiClient) -> IndiClient:
    for device in ALL_DEVICES:
        await client.wait_for_device(device)
        if not client.is_device_connected(device):
            await client.connect_device(device)
    return client


async def wait_for(condition: object, timeout: float = 45.0) -> None:
    """Poll ``condition`` until true, or fail the test."""
    assert callable(condition)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not met in time")


class TestAllFiveDeviceClasses:
    """SPEC section 14, M1 AUTO: connects to all five simulator drivers."""

    async def test_the_five_drivers_are_running(self, indi_server: IndiServerProcess) -> None:
        running = set(indi_server.driver_pids())
        assert set(SIMULATOR_DRIVERS.values()) <= running

    async def test_every_device_announces_itself(self, client: IndiClient) -> None:
        for device in ALL_DEVICES:
            await client.wait_for_device(device)
        assert set(ALL_DEVICES) <= set(client.devices())

    @pytest.mark.parametrize("device", ALL_DEVICES)
    async def test_every_device_connects(self, client: IndiClient, device: str) -> None:
        await client.wait_for_device(device)
        await client.connect_device(device)
        assert client.is_device_connected(device)

    @pytest.mark.parametrize("device", ALL_DEVICES)
    async def test_every_device_disconnects_and_reconnects(
        self, connected_client: IndiClient, device: str
    ) -> None:
        await connected_client.disconnect_device(device)
        assert not connected_client.is_device_connected(device)
        await connected_client.connect_device(device)
        assert connected_client.is_device_connected(device)


class TestReadProperties:
    """SPEC section 14, M1 AUTO: reads properties."""

    @pytest.mark.parametrize("device", ALL_DEVICES)
    async def test_reads_the_driver_identity(self, client: IndiClient, device: str) -> None:
        prop = await client.wait_for_property(device, IDENTITY_PROPERTY)
        assert prop.permission is PropertyPermission.READ_ONLY
        assert str(prop["DRIVER_EXEC"]).startswith("indi_simulator")

    @pytest.mark.parametrize("device", ALL_DEVICES)
    async def test_connecting_defines_the_operational_properties(
        self, connected_client: IndiClient, device: str
    ) -> None:
        name = WRITE_CASES[device][0]
        prop = await connected_client.wait_for_property(device, name)
        assert prop.is_writable
        assert prop.elements

    async def test_the_camera_reports_its_geometry(self, connected_client: IndiClient) -> None:
        info = await connected_client.wait_for_property("CCD Simulator", "CCD_INFO")
        assert float(info["CCD_MAX_X"]) > 0
        assert float(info["CCD_PIXEL_SIZE"]) > 0

    async def test_the_mount_reports_coordinates(self, connected_client: IndiClient) -> None:
        coords = await connected_client.wait_for_property(
            "Telescope Simulator", "EQUATORIAL_EOD_COORD"
        )
        assert set(coords.elements) == {"RA", "DEC"}

    async def test_the_focuser_reports_temperature(self, connected_client: IndiClient) -> None:
        """SPEC section 5.1: the EAF has a temperature sensor; M2 uses it."""
        temperature = await connected_client.wait_for_property(
            "Focuser Simulator", "FOCUS_TEMPERATURE"
        )
        assert "TEMPERATURE" in temperature.elements

    async def test_the_filter_wheel_reports_its_slot_range(
        self, connected_client: IndiClient
    ) -> None:
        slot = await connected_client.wait_for_property("Filter Simulator", "FILTER_SLOT")
        element = slot.elements["FILTER_SLOT_VALUE"]
        assert element.minimum == pytest.approx(1.0)
        assert element.maximum is not None
        assert element.maximum >= 5.0


class TestWriteProperties:
    """SPEC section 14, M1 AUTO: writes properties."""

    @pytest.mark.parametrize("device", ALL_DEVICES)
    async def test_writing_takes_effect(
        self, connected_client: IndiClient, device: str
    ) -> None:
        name, values, element, expected = WRITE_CASES[device]
        result = await connected_client.write(device, name, values)
        assert result.state is not PropertyState.ALERT
        if isinstance(expected, bool):
            assert result[element] is expected
        else:
            assert float(result[element]) == pytest.approx(expected)

    async def test_the_write_is_visible_in_the_cache_afterwards(
        self, connected_client: IndiClient
    ) -> None:
        await connected_client.write(
            "Filter Simulator", "FILTER_SLOT", {"FILTER_SLOT_VALUE": 5.0}
        )
        cached = connected_client.get("Filter Simulator", "FILTER_SLOT")
        assert cached is not None
        assert float(cached["FILTER_SLOT_VALUE"]) == pytest.approx(5.0)

    async def test_the_focuser_actually_moves(self, connected_client: IndiClient) -> None:
        """A number vector that reports Ok before the motor stopped is a lie."""
        await connected_client.write(
            "Focuser Simulator", "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": 30000.0}
        )
        settled = await connected_client.write(
            "Focuser Simulator", "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": 35000.0}
        )
        assert settled.state is not PropertyState.BUSY
        assert float(settled["FOCUS_ABSOLUTE_POSITION"]) == pytest.approx(35000.0)


class TestDriverRestart:
    """SPEC section 14, M1 AUTO: survives driver restart, reconnects automatically."""

    @pytest.mark.parametrize("driver", sorted(SIMULATOR_DRIVERS.values()))
    async def test_a_killed_driver_is_noticed_and_reconnected(
        self,
        connected_client: IndiClient,
        indi_server: IndiServerProcess,
        driver: str,
    ) -> None:
        device = next(
            SIMULATOR_DEVICES[role]
            for role, executable in SIMULATOR_DRIVERS.items()
            if executable == driver
        )
        assert connected_client.is_device_connected(device)

        events: list[IndiEvent] = []
        unsubscribe = connected_client.subscribe(events.append)
        try:
            indi_server.kill_driver(driver)

            await wait_for(lambda: any(isinstance(e, DeviceVanished) for e in events))
            await wait_for(lambda: any(isinstance(e, DeviceAppeared) for e in events))
            await wait_for(lambda: connected_client.is_device_connected(device))
        finally:
            unsubscribe()

        # And it is usable again, not merely present.
        name, values, _element, _expected = WRITE_CASES[device]
        result = await connected_client.write(device, name, values)
        assert result.state is not PropertyState.ALERT

    async def test_the_server_connection_survives_a_driver_death(
        self, connected_client: IndiClient, indi_server: IndiServerProcess
    ) -> None:
        """A driver dying is not a server disconnection; the socket stays up."""
        indi_server.kill_driver("indi_simulator_wheel")
        await wait_for(lambda: connected_client.is_device_connected("Filter Simulator"))
        assert connected_client.is_connected
        assert indi_server.is_running

    async def test_the_other_devices_are_undisturbed(
        self, connected_client: IndiClient, indi_server: IndiServerProcess
    ) -> None:
        """One driver dying must cost one device, not the night."""
        vanished: list[str] = []
        unsubscribe = connected_client.subscribe(
            lambda event: (
                vanished.append(event.device) if isinstance(event, DeviceVanished) else None
            )
        )
        try:
            indi_server.kill_driver("indi_simulator_focus")
            await wait_for(lambda: "Focuser Simulator" in vanished)
            await wait_for(lambda: connected_client.is_device_connected("Focuser Simulator"))
        finally:
            unsubscribe()

        assert vanished == ["Focuser Simulator"]
        for device in ALL_DEVICES:
            assert connected_client.is_device_connected(device), device


class TestServerRestart:
    """The whole server going away, not just one driver."""

    async def test_client_reconnects_when_indiserver_comes_back(self) -> None:
        server = IndiServerProcess()
        server.start()
        client = IndiClient(SETTINGS.model_copy(update={"port": server.port}))
        try:
            await client.connect()
            await client.wait_for_device("Focuser Simulator")
            await client.connect_device("Focuser Simulator")

            server.stop()
            await wait_for(lambda: not client.is_connected, timeout=20.0)

            restarted = IndiServerProcess(port=server.port)
            restarted.start()
            try:
                await wait_for(lambda: client.is_connected, timeout=30.0)
                await wait_for(
                    lambda: client.is_device_connected("Focuser Simulator"), timeout=45.0
                )
            finally:
                restarted.stop()
        finally:
            await client.aclose()
            server.stop()


class TestThroughTheExecutor:
    """The same behaviours through the facade the rest of Nocturne uses."""

    @pytest.fixture
    async def executor(
        self, indi_server: IndiServerProcess, config_dir: Path
    ) -> AsyncIterator[Executor]:
        governor = SafetyGovernor(load_config_bundle(config_dir).safety)
        client = IndiClient(SETTINGS.model_copy(update={"port": indi_server.port}))
        running = Executor(client, governor)
        await running.start()
        try:
            yield running
        finally:
            await running.aclose()

    async def test_connects_every_device_through_the_governor(self, executor: Executor) -> None:
        for device in ALL_DEVICES:
            await executor.wait_for_device(device)
            await executor.connect_device(device)
            assert executor.is_device_connected(device)

    async def test_writes_a_property_through_the_governor(self, executor: Executor) -> None:
        await executor.wait_for_device("Filter Simulator")
        await executor.connect_device("Filter Simulator")
        result = await executor.set_property(
            "Filter Simulator", "FILTER_SLOT", {"FILTER_SLOT_VALUE": 4.0}
        )
        assert result is not None
        assert float(result["FILTER_SLOT_VALUE"]) == pytest.approx(4.0)

    async def test_unattended_autonomy_is_refused_on_this_rig(self, executor: Executor) -> None:
        """SPEC section 9.1.2 holds against the real stack, not just in unit tests."""
        with pytest.raises(SafetyViolation, match="meridian"):
            executor.governor.require_autonomy_level("autonomous")
