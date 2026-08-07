"""Every device is held at its configured values — docs/FIELD-NOTES-M1.md §11.

The mount starts at 800x sidereal on every connection. The cameras start at the
driver's gain and offset. The wheel starts holding ZWO factory filter names.
It is one behaviour with four faces, and the mechanism that corrects it is one
mechanism — so these tests exercise it per device *and* assert that the mount is
not special-cased, because a second copy is how the fifth device gets missed.

The fake server is built from ``devices-properties.txt``, real ``indi_getprop``
output, so the property names, elements and defaults below are the drivers'.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from nocturne.executor.executor import Executor
from nocturne.executor.indi.client import IndiClient
from nocturne.executor.instruments import (
    CCD_CONTROLS,
    FILTER_NAME,
    GAIN_ELEMENT,
    OFFSET_ELEMENT,
    filter_wheel_link,
    focuser_link,
    guide_camera_link,
    imaging_camera_link,
)
from nocturne.executor.link import DeviceBringUpError, DeviceLink
from nocturne.executor.settings import IndiSettings
from nocturne.safety import SafetyGovernor
from nocturne.schemas import load_config_bundle
from tests.fixtures.devices import (
    EFW,
    FOCUSER,
    GUIDE_CAMERA,
    IMAGING_CAMERA,
    fresh_devices,
    recorded_devices,
)
from tests.fixtures.fake_indi import FakeIndiServer

FAST = IndiSettings(
    connect_timeout_s=2.0,
    property_timeout_s=2.0,
    write_timeout_s=2.0,
    device_connect_timeout_s=2.0,
    reconnect_initial_delay_s=0.01,
    reconnect_max_delay_s=0.05,
)

#: What the drivers came up holding. Read out of the dump, not retyped.
DRIVER_DEFAULT_GAIN = {IMAGING_CAMERA: 200.0, GUIDE_CAMERA: 50.0}

#: The ZWO factory names the EFW arrived with. None of them is in the wheel.
ZWO_FACTORY_NAMES = (
    "Red",
    "Green",
    "Blue",
    "H_Alpha",
    "SII",
    "OIII",
    "LPR",
    "Luminance",
)


@pytest.fixture
async def server() -> AsyncIterator[FakeIndiServer]:
    fake = FakeIndiServer(fresh_devices())
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def executor(server: FakeIndiServer, config_dir: Path) -> AsyncIterator[Executor]:
    bundle = load_config_bundle(config_dir)
    client = IndiClient(FAST.model_copy(update={"port": server.port}))
    facade = Executor(client, SafetyGovernor(bundle.safety))
    await facade.start()
    for device in (IMAGING_CAMERA, GUIDE_CAMERA, EFW, FOCUSER):
        await facade.wait_for_device(device)
    try:
        yield facade
    finally:
        await facade.aclose()


@pytest.fixture
def equipment(config_dir: Path) -> Any:
    return load_config_bundle(config_dir).equipment


def values(server: FakeIndiServer, device: str, name: str) -> dict[str, Any]:
    return dict(server.devices[device][name].values)


class TestTheRecordedDefaultsAreWhatWeThinkTheyAre:
    """If the fixture drifts, every test below is measuring nothing."""

    def test_the_cameras_come_up_on_the_drivers_gain(self) -> None:
        recorded = recorded_devices()
        for device, gain in DRIVER_DEFAULT_GAIN.items():
            assert recorded[device][CCD_CONTROLS].values[GAIN_ELEMENT] == gain

    def test_the_wheel_comes_up_holding_zwo_factory_names(self) -> None:
        recorded = recorded_devices()[EFW][FILTER_NAME]
        assert tuple(recorded.values.values()) == ZWO_FACTORY_NAMES


class TestTheCamerasAreHeldAtTheConfiguredValues:
    async def test_the_imaging_camera_gain_comes_from_the_gain_profile(
        self, executor: Executor, server: FakeIndiServer, equipment: Any
    ) -> None:
        """SPEC §15: the exposure solver divides by this number."""
        assert values(server, IMAGING_CAMERA, CCD_CONTROLS)[GAIN_ELEMENT] == 200.0

        profile = equipment.imaging_camera.gain_profiles[0]
        link = imaging_camera_link(
            executor, equipment.imaging_camera, gain_profile=profile.name
        )
        async with link:
            await link.bring_up()

        applied = values(server, IMAGING_CAMERA, CCD_CONTROLS)
        assert applied[GAIN_ELEMENT] == float(profile.gain)
        assert applied[OFFSET_ELEMENT] == float(equipment.imaging_camera.offset)

    async def test_the_configured_gain_really_differs_from_the_driver_default(
        self, equipment: Any
    ) -> None:
        """Positive control: equal values would make the test above vacuous."""
        profile = equipment.imaging_camera.gain_profiles[0]
        assert float(profile.gain) != DRIVER_DEFAULT_GAIN[IMAGING_CAMERA]

    async def test_the_shipped_guide_gain_coincides_with_the_driver_default(
        self, equipment: Any
    ) -> None:
        """Worth stating rather than discovering.

        The 120MM Mini ships at Gain 50 and equipment.yaml also says 50. That is
        a coincidence, not agreement, and it means any test of the guide
        camera's re-application through the shipped value would pass whether or
        not the write happened. Tests that need to see the difference use a
        distinct value on purpose.
        """
        assert float(equipment.guiding.camera.gain) == DRIVER_DEFAULT_GAIN[GUIDE_CAMERA]

    async def test_the_guide_camera_is_applied_too(
        self, executor: Executor, server: FakeIndiServer, equipment: Any
    ) -> None:
        link = guide_camera_link(executor, equipment.guiding.camera)
        async with link:
            await link.bring_up()

        applied = values(server, GUIDE_CAMERA, CCD_CONTROLS)
        assert applied[GAIN_ELEMENT] == float(equipment.guiding.camera.gain)
        assert applied[OFFSET_ELEMENT] == float(equipment.guiding.camera.offset)


class TestTheFilterNamesAreWrittenNotRead:
    """docs/FIELD-NOTES-M1.md §10.2 — the wheel's own names are wrong."""

    async def test_the_configured_names_overwrite_the_factory_ones(
        self, executor: Executor, server: FakeIndiServer, equipment: Any
    ) -> None:
        before = values(server, EFW, FILTER_NAME)
        assert tuple(before.values()) == ZWO_FACTORY_NAMES

        link = filter_wheel_link(executor, equipment.filter_wheel)
        async with link:
            await link.bring_up()

        after = values(server, EFW, FILTER_NAME)
        assert after["FILTER_SLOT_NAME_1"] == "L"
        assert after["FILTER_SLOT_NAME_4"] == "B"
        assert after["FILTER_SLOT_NAME_5"] == "Dark"

    async def test_the_dangerous_one_is_gone(
        self, executor: Executor, server: FakeIndiServer, equipment: Any
    ) -> None:
        """Slot 4 read H_Alpha and holds B. That is the frame filed wrongly."""
        link = filter_wheel_link(executor, equipment.filter_wheel)
        async with link:
            await link.bring_up()

        after = values(server, EFW, FILTER_NAME)
        assert "H_Alpha" not in after.values()
        assert after["FILTER_SLOT_NAME_4"] != "H_Alpha"

    async def test_the_overwrite_is_logged_so_a_rearranged_wheel_leaves_a_trace(
        self, executor: Executor, equipment: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="nocturne.executor.instruments"):
            link = filter_wheel_link(executor, equipment.filter_wheel)
            async with link:
                await link.bring_up()

        assert "disagree with equipment.yaml" in caplog.text

    async def test_a_wheel_with_too_few_positions_is_refused(
        self, executor: Executor, server: FakeIndiServer, equipment: Any
    ) -> None:
        """Rather than naming the slots it can and leaving the rest wrong."""
        prop = server.devices[EFW][FILTER_NAME]
        prop.values = {"FILTER_SLOT_NAME_1": "Red", "FILTER_SLOT_NAME_2": "Green"}
        await server.broadcast(prop.define(EFW))

        def only_two_are_cached() -> bool:
            cached = executor.get_property(EFW, FILTER_NAME)
            return cached is not None and len(cached.elements) == 2

        await _until(only_two_are_cached)

        link = filter_wheel_link(executor, equipment.filter_wheel)
        async with link:
            with pytest.raises(DeviceBringUpError, match="FILTER_SLOT_NAME_3"):
                await link.bring_up()


class TestTheFocuserLimitComesFromTheDriver:
    """docs/FIELD-NOTES-M1.md §12."""

    async def test_an_unset_limit_defers_to_the_driver(
        self, executor: Executor, equipment: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        assert equipment.focuser.max_position is None
        with caplog.at_level("INFO", logger="nocturne.executor.instruments"):
            link = focuser_link(executor, equipment.focuser)
            async with link:
                await link.bring_up()
        assert "travel limit the driver reports" in caplog.text

    async def test_a_tighter_limit_is_accepted(
        self, executor: Executor, equipment: Any
    ) -> None:
        focuser = equipment.focuser.model_copy(update={"max_position": 60000})
        link = focuser_link(executor, focuser)
        async with link:
            await link.bring_up()

    async def test_a_looser_limit_is_refused_rather_than_clamped(
        self, executor: Executor, equipment: Any
    ) -> None:
        """Clamping would grant travel the operator thought they had forbidden."""
        focuser = equipment.focuser.model_copy(update={"max_position": 200000})
        link = focuser_link(executor, focuser)
        async with link:
            with pytest.raises(DeviceBringUpError, match="may only tighten"):
                await link.bring_up()

    async def test_backlash_is_not_written_while_it_is_unmeasured(
        self, executor: Executor, server: FakeIndiServer, equipment: Any
    ) -> None:
        """180 is a ZWO default. Replacing it with a guess is not an improvement."""
        assert equipment.focuser.backlash_steps is None
        link = focuser_link(executor, equipment.focuser)
        async with link:
            await link.bring_up()

        assert (FOCUSER, "FOCUS_BACKLASH_STEPS") not in server.writes

    async def test_a_measured_backlash_is_written(
        self, executor: Executor, server: FakeIndiServer, equipment: Any
    ) -> None:
        focuser = equipment.focuser.model_copy(update={"backlash_steps": 42})
        link = focuser_link(executor, focuser)
        async with link:
            await link.bring_up()

        applied = values(server, FOCUSER, "FOCUS_BACKLASH_STEPS")
        assert applied["FOCUS_BACKLASH_VALUE"] == 42.0


class TestEveryDeviceSurvivesAReconnection:
    """The generalisation of the slew-ceiling regression, device by device."""

    #: The guide camera is configured at exactly its driver default — 50 — so a
    #: re-application test through the shipped value could not tell "re-applied"
    #: from "never touched". It is given a distinct gain here so the case means
    #: something; that the shipped values coincide is asserted separately below.
    GUIDE_GAIN_FOR_THIS_TEST = 77

    @pytest.mark.parametrize(
        ("device", "build", "property_name", "element", "expected"),
        [
            (IMAGING_CAMERA, "imaging", CCD_CONTROLS, GAIN_ELEMENT, 100.0),
            (
                GUIDE_CAMERA,
                "guide",
                CCD_CONTROLS,
                GAIN_ELEMENT,
                float(GUIDE_GAIN_FOR_THIS_TEST),
            ),
            (EFW, "wheel", FILTER_NAME, "FILTER_SLOT_NAME_1", "L"),
        ],
    )
    async def test_a_driver_restart_does_not_silently_restore_the_default(
        self,
        executor: Executor,
        server: FakeIndiServer,
        equipment: Any,
        device: str,
        build: str,
        property_name: str,
        element: str,
        expected: float | str,
    ) -> None:
        link = {
            "imaging": lambda: imaging_camera_link(
                executor,
                equipment.imaging_camera,
                gain_profile=equipment.imaging_camera.gain_profiles[0].name,
            ),
            "guide": lambda: guide_camera_link(
                executor,
                equipment.guiding.camera.model_copy(
                    update={"gain": self.GUIDE_GAIN_FOR_THIS_TEST}
                ),
            ),
            "wheel": lambda: filter_wheel_link(executor, equipment.filter_wheel),
        }[build]()

        async with link:
            await link.bring_up()
            assert values(server, device, property_name)[element] == expected

            # indiserver announces the device gone, then respawns the driver at
            # its own defaults.
            await server.kill_driver(device)
            await server.restart_driver(device, fresh_devices()[device])
            assert values(server, device, property_name)[element] != expected

            await _until(lambda: values(server, device, property_name)[element] == expected)


class TestTheMountIsNotSpecialCased:
    """A second copy of this mechanism is how the fifth device gets missed."""

    def test_the_mount_uses_the_same_link(self) -> None:
        import inspect

        from nocturne.executor import mount as mount_module

        source = inspect.getsource(mount_module)
        assert "DeviceLink" in source, "MountLink no longer shares the mechanism"

    def test_the_mount_module_defines_no_watcher_of_its_own(self) -> None:
        """The watching, re-applying and task handling live in one place."""
        import inspect

        from nocturne.executor import mount as mount_module

        source = inspect.getsource(mount_module)
        for duplicated in ("_on_event", "_reapply", "add_done_callback", "subscribe("):
            assert duplicated not in source, (
                f"mount.py has its own {duplicated}; the mechanism has been copied "
                "rather than shared, and a new device will not get it"
            )

    def test_the_shared_link_really_does_the_watching(self) -> None:
        """Positive control for the two above — CLAUDE.md §2."""
        import inspect

        source = inspect.getsource(DeviceLink)
        for required in ("_on_event", "_reapply", "subscribe("):
            assert required in source, required


async def _until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    """Wait until ``condition()`` is true, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition was still false after {timeout} s")
