"""Mount bring-up against the recorded Wave 150i — docs/FIELD-NOTES-M1.md.

Three findings from the real rig are enforced here:

* section 2.2 — the driver starts at 9600 baud, so the configured rate has to
  be set on every connection, *before* CONNECT.
* section 2.3 — the driver populates DEVICE_PORT itself; a configured port is
  an override, not the primary source.
* section 3.2 — the driver starts at 800x sidereal on every connection. The
  configured ceiling has to be applied at connect and re-applied after a driver
  restart. "A reconnection that silently restores 800x is a safety regression."

The server these tests talk to is built from ``wave150i-properties.txt``, real
``indi_getprop`` output, so the property names, elements and default values are
the hardware's rather than the author's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from nocturne.devices import MissingSerialDeviceError
from nocturne.executor.executor import Executor
from nocturne.executor.indi.client import IndiClient
from nocturne.executor.indi.protocol import PropertyKind
from nocturne.executor.mount import (
    BAUD_PROPERTY,
    DE_SLEW_ELEMENT,
    PORT_ELEMENT,
    PORT_PROPERTY,
    RA_SLEW_ELEMENT,
    SIDEREAL_ARCSEC_PER_SECOND,
    SLEW_SPEEDS_PROPERTY,
    MountBringUpError,
    MountLink,
    sidereal_multiple,
)
from nocturne.executor.settings import IndiSettings
from nocturne.safety import SafetyGovernor
from nocturne.schemas import load_config_bundle
from nocturne.schemas.equipment import Mount
from tests.fixtures.eqmod import EQMOD_DEVICE, fresh_driver, load_recorded_properties
from tests.fixtures.fake_indi import FakeIndiServer, FakeProperty

FAST = IndiSettings(
    connect_timeout_s=2.0,
    property_timeout_s=2.0,
    write_timeout_s=2.0,
    device_connect_timeout_s=2.0,
    reconnect_initial_delay_s=0.01,
    reconnect_max_delay_s=0.05,
)

#: The port the real driver reported, read out of the recorded dump rather than
#: retyped: it is one rig's serial number and belongs in exactly one file.
REFERENCE_PORT = str(load_recorded_properties()[PORT_PROPERTY].values[PORT_ELEMENT])


def mount_config(**overrides: object) -> Mount:
    """The reference mount configuration, with fields replaced for one test."""
    fields: dict[str, object] = {
        "indi_driver": "indi_eqmod_telescope",
        "indi_device_name": EQMOD_DEVICE,
        "device_label": "Wave 150i",
        "connection": "serial",
        "port": REFERENCE_PORT,
        "baud": 115200,
        "slew_rate_max_deg_s": 3.0,
        "counterweight_fitted": False,
    }
    fields.update(overrides)
    return Mount.model_validate(fields)


@pytest.fixture(autouse=True)
def the_configured_port_is_plugged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Say the device is present, for every test that does not say otherwise.

    These tests talk to a fake INDI server, so the configured port names a
    device that is not on the machine running the suite. The real check is
    exercised in test_devices.py; that MountLink calls it, and refuses when it
    fails, is exercised in TestAConfiguredPortThatIsNotThere below.
    """
    monkeypatch.setattr("nocturne.devices.serial_device_exists", lambda _port: True)


@pytest.fixture
async def server() -> AsyncIterator[FakeIndiServer]:
    fake = FakeIndiServer(fresh_driver())
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
    await facade.wait_for_device(EQMOD_DEVICE)
    try:
        yield facade
    finally:
        await facade.aclose()


def slew_speeds(server: FakeIndiServer) -> dict[str, float | str | bool]:
    return server.devices[EQMOD_DEVICE][SLEW_SPEEDS_PROPERTY].values


class TestTheSiderealConversion:
    """SPEC section 5.1 gives a limit in degrees per second; eqmod wants a multiple."""

    def test_the_reference_limit_converts_to_718(self) -> None:
        """3.0 deg/s at 15.041... arcsec/s is 718.03 sidereal; 718 is the whole part."""
        assert sidereal_multiple(3.0) == 718

    def test_it_never_rounds_up_past_the_limit(self) -> None:
        for limit in (0.5, 1.0, 3.0, 3.5, 7.5):
            multiple = sidereal_multiple(limit)
            achieved_deg_s = multiple * SIDEREAL_ARCSEC_PER_SECOND / 3600.0
            assert achieved_deg_s <= limit
            # And it is the largest such multiple: one more would exceed it.
            assert (multiple + 1) * SIDEREAL_ARCSEC_PER_SECOND / 3600.0 > limit

    def test_a_limit_the_driver_cannot_express_is_refused_not_clamped(self) -> None:
        """Below one sidereal multiple there is no safe value to write."""
        with pytest.raises(MountBringUpError, match="below one multiple of the sidereal"):
            sidereal_multiple(0.001)

    def test_a_non_positive_limit_is_refused(self) -> None:
        with pytest.raises(MountBringUpError):
            sidereal_multiple(0.0)


class TestTheSlewRateLimitIsApplied:
    """FIELD-NOTES-M1 section 3.2."""

    async def test_the_recorded_hardware_really_does_default_to_800(self) -> None:
        """If this fails the fixture has drifted and the rest proves nothing."""
        recorded = load_recorded_properties()[SLEW_SPEEDS_PROPERTY]
        assert recorded.values == {RA_SLEW_ELEMENT: 800.0, DE_SLEW_ELEMENT: 800.0}

    async def test_bring_up_replaces_800_with_the_configured_ceiling(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        assert slew_speeds(server) == {RA_SLEW_ELEMENT: 800.0, DE_SLEW_ELEMENT: 800.0}

        link = MountLink(executor, mount_config())
        async with link:
            await link.bring_up()
            assert slew_speeds(server) == {RA_SLEW_ELEMENT: 718.0, DE_SLEW_ELEMENT: 718.0}

    async def test_a_different_configured_limit_produces_a_different_write(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        """The value comes from config, not from a constant in the code."""
        link = MountLink(executor, mount_config(slew_rate_max_deg_s=1.0))
        async with link:
            await link.bring_up()
            assert slew_speeds(server) == {RA_SLEW_ELEMENT: 239.0, DE_SLEW_ELEMENT: 239.0}

    async def test_it_waits_for_the_vector_the_driver_defines_only_after_connect(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        """SLEWSPEEDS is not in the cache until the driver has read the mount.

        The withholding has to reach the *client's* cache, not just the
        server's dictionary — hence the delProperty. Popping it server-side
        alone leaves the client holding the definition it was sent at
        connection time, and the wait this test is about never happens.
        """
        withheld = server.devices[EQMOD_DEVICE].pop(SLEW_SPEEDS_PROPERTY)
        await server.broadcast(
            f'<delProperty device="{EQMOD_DEVICE}" name="{SLEW_SPEEDS_PROPERTY}"/>'.encode()
        )
        await _until(lambda: executor.get_property(EQMOD_DEVICE, SLEW_SPEEDS_PROPERTY) is None)

        link = MountLink(executor, mount_config())
        async with link:
            waiting = asyncio.create_task(link.bring_up())
            await asyncio.sleep(0.05)
            assert not waiting.done(), "it did not wait for the vector"

            server.devices[EQMOD_DEVICE][SLEW_SPEEDS_PROPERTY] = withheld
            await server.broadcast(withheld.define(EQMOD_DEVICE))
            await asyncio.wait_for(waiting, timeout=2.0)

        assert slew_speeds(server) == {RA_SLEW_ELEMENT: 718.0, DE_SLEW_ELEMENT: 718.0}


class TestTheSlewRateLimitSurvivesRecovery:
    """The safety regression FIELD-NOTES-M1 section 3.2 asks to be covered."""

    async def test_a_driver_restart_does_not_silently_restore_800(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        link = MountLink(executor, mount_config())
        async with link:
            await link.bring_up()
            assert slew_speeds(server) == {RA_SLEW_ELEMENT: 718.0, DE_SLEW_ELEMENT: 718.0}

            # indiserver announces the device gone, then respawns the driver.
            # A respawned driver comes back at its own defaults, 800x among them.
            await server.kill_driver(EQMOD_DEVICE)
            await server.restart_driver(EQMOD_DEVICE, fresh_driver()[EQMOD_DEVICE])
            assert slew_speeds(server) == {RA_SLEW_ELEMENT: 800.0, DE_SLEW_ELEMENT: 800.0}

            await _until(lambda: slew_speeds(server)[RA_SLEW_ELEMENT] == 718.0)
            assert slew_speeds(server) == {RA_SLEW_ELEMENT: 718.0, DE_SLEW_ELEMENT: 718.0}

    async def test_a_server_reconnection_does_not_silently_restore_800(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        link = MountLink(executor, mount_config())
        async with link:
            await link.bring_up()

            server.devices[EQMOD_DEVICE] = fresh_driver()[EQMOD_DEVICE]
            await server.drop_clients()

            await _until(lambda: slew_speeds(server)[RA_SLEW_ELEMENT] == 718.0)
            assert slew_speeds(server) == {RA_SLEW_ELEMENT: 718.0, DE_SLEW_ELEMENT: 718.0}

    async def test_the_watcher_stops_when_the_link_is_closed(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        link = MountLink(executor, mount_config())
        async with link:
            await link.bring_up()
        assert not link.is_watching

        server.devices[EQMOD_DEVICE] = fresh_driver()[EQMOD_DEVICE]
        await server.kill_driver(EQMOD_DEVICE)
        await asyncio.sleep(0.1)
        # Nothing re-applies, because nothing is watching. Stated so that the
        # test above is known to be measuring the watcher and not the client.
        assert EQMOD_DEVICE not in server.devices


class TestWhenTheCeilingCannotBeApplied:
    """The mount must not be driven at 800x because a write quietly failed."""

    async def test_a_slewspeeds_vector_missing_an_axis_refuses(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        one_axis = FakeProperty(
            name=SLEW_SPEEDS_PROPERTY,
            kind=PropertyKind.NUMBER,
            values={RA_SLEW_ELEMENT: 800.0},
        )
        server.devices[EQMOD_DEVICE][SLEW_SPEEDS_PROPERTY] = one_axis
        await server.broadcast(one_axis.define(EQMOD_DEVICE))

        def one_axis_is_cached() -> bool:
            cached = executor.get_property(EQMOD_DEVICE, SLEW_SPEEDS_PROPERTY)
            return cached is not None and DE_SLEW_ELEMENT not in cached.elements

        await _until(one_axis_is_cached)

        link = MountLink(executor, mount_config())
        async with link:
            with pytest.raises(MountBringUpError, match=DE_SLEW_ELEMENT):
                await link.bring_up()

    async def test_a_failure_during_re_application_is_logged_not_swallowed(
        self,
        executor: Executor,
        server: FakeIndiServer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The watcher runs in a bare task: an exception there has nowhere to go."""
        link = MountLink(executor, mount_config())
        async with link:
            await link.bring_up()

            # The driver comes back without an axis, so the re-application fails.
            broken = fresh_driver()[EQMOD_DEVICE]
            broken[SLEW_SPEEDS_PROPERTY] = FakeProperty(
                name=SLEW_SPEEDS_PROPERTY,
                kind=PropertyKind.NUMBER,
                values={RA_SLEW_ELEMENT: 800.0},
            )
            caplog.clear()
            with caplog.at_level("ERROR", logger="nocturne.executor.mount"):
                await server.kill_driver(EQMOD_DEVICE)
                await server.restart_driver(EQMOD_DEVICE, broken)
                await _until(lambda: "800x sidereal" in caplog.text)

        assert "may be at its default" in caplog.text

    async def test_the_conversion_failure_happens_before_anything_connects(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        """A limit the driver cannot express is caught at construction."""
        with pytest.raises(MountBringUpError):
            MountLink(executor, mount_config(slew_rate_max_deg_s=0.001))
        assert (EQMOD_DEVICE, "CONNECTION") not in server.writes


class TestTheWatcherLifecycle:
    async def test_watching_twice_subscribes_once(self, executor: Executor) -> None:
        link = MountLink(executor, mount_config())
        async with link:
            link.watch()
            link.watch()
            assert link.is_watching
        assert not link.is_watching

    async def test_closing_an_unwatched_link_is_harmless(self, executor: Executor) -> None:
        link = MountLink(executor, mount_config())
        link.stop_watching()
        await link.aclose()
        assert not link.is_watching

    async def test_the_configured_ceiling_is_readable_without_connecting(
        self, executor: Executor
    ) -> None:
        link = MountLink(executor, mount_config())
        assert link.device == EQMOD_DEVICE
        assert link.slew_speed_multiple == 718
        assert link.port_in_use is None


class TestTheBaudRateIsSetBeforeConnect:
    """FIELD-NOTES-M1 section 2.2."""

    async def test_the_configured_rate_is_selected(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        baud = server.devices[EQMOD_DEVICE][BAUD_PROPERTY]
        assert baud.values["9600"] is True

        link = MountLink(executor, mount_config())
        async with link:
            await link.bring_up()

        assert baud.values["115200"] is True
        assert baud.values["9600"] is False

    async def test_it_is_written_before_the_connect(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        """Order matters: still at 9600 when CONNECT lands means no link at all."""
        link = MountLink(executor, mount_config())
        async with link:
            await link.bring_up()

        order = [name for device, name in server.writes if device == EQMOD_DEVICE]
        assert BAUD_PROPERTY in order, order
        assert "CONNECTION" in order, order
        assert order.index(BAUD_PROPERTY) < order.index("CONNECTION"), order
        assert order.index("CONNECTION") < order.index(SLEW_SPEEDS_PROPERTY), order

    async def test_a_rate_the_driver_does_not_offer_is_refused_loudly(
        self, executor: Executor
    ) -> None:
        link = MountLink(executor, mount_config(baud=4800))
        async with link:
            with pytest.raises(MountBringUpError, match="4800"):
                await link.bring_up()


class TestThePortComesFromTheDriverFirst:
    """FIELD-NOTES-M1 section 2.3."""

    async def test_an_unset_port_uses_the_one_the_driver_reports(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        link = MountLink(executor, mount_config(port=None))
        async with link:
            await link.bring_up()

        port = server.devices[EQMOD_DEVICE][PORT_PROPERTY]
        assert port.values[PORT_ELEMENT] == REFERENCE_PORT
        assert link.port_in_use == REFERENCE_PORT

    async def test_a_configured_port_overrides_the_driver(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        link = MountLink(executor, mount_config(port="/dev/ttyACM1"))
        async with link:
            await link.bring_up()

        port = server.devices[EQMOD_DEVICE][PORT_PROPERTY]
        assert port.values[PORT_ELEMENT] == "/dev/ttyACM1"
        assert link.port_in_use == "/dev/ttyACM1"

    async def test_no_port_anywhere_refuses_rather_than_connecting_blind(
        self, executor: Executor, server: FakeIndiServer
    ) -> None:
        """A connect with an empty port produces an opaque driver error instead."""
        server.devices[EQMOD_DEVICE][PORT_PROPERTY] = FakeProperty(
            name=PORT_PROPERTY, kind=PropertyKind.TEXT, values={PORT_ELEMENT: ""}
        )
        await server.broadcast(server.devices[EQMOD_DEVICE][PORT_PROPERTY].define(EQMOD_DEVICE))

        link = MountLink(executor, mount_config(port=None))
        async with link:
            with pytest.raises(MountBringUpError, match="did not report a port"):
                await link.bring_up()


class TestAConfiguredPortThatIsNotThere:
    """The override must not degrade into auto-detection."""

    async def test_bring_up_refuses_before_it_touches_the_driver(
        self, executor: Executor, server: FakeIndiServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nocturne.devices.serial_device_exists", lambda _port: False)
        link = MountLink(executor, mount_config())

        async with link:
            with pytest.raises(MissingSerialDeviceError, match=REFERENCE_PORT):
                await link.bring_up()

        # Nothing was said to the mount, and in particular it was not connected
        # on whatever port the driver happened to have found.
        assert (EQMOD_DEVICE, "CONNECTION") not in server.writes
        assert slew_speeds(server) == {RA_SLEW_ELEMENT: 800.0, DE_SLEW_ELEMENT: 800.0}

    async def test_an_unset_port_is_unaffected_by_the_check(
        self, executor: Executor, server: FakeIndiServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is no configured device to be missing."""
        monkeypatch.setattr("nocturne.devices.serial_device_exists", lambda _port: False)
        link = MountLink(executor, mount_config(port=None))

        async with link:
            await link.bring_up()

        assert slew_speeds(server) == {RA_SLEW_ELEMENT: 718.0, DE_SLEW_ELEMENT: 718.0}


class TestTheDeviceNameComesFromConfiguration:
    """CLAUDE.md section 6: no hardcoded equipment."""

    async def test_the_link_addresses_the_device_the_config_names(
        self, executor: Executor
    ) -> None:
        link = MountLink(executor, mount_config())
        assert link.device == EQMOD_DEVICE

    async def test_a_different_configured_name_addresses_a_different_device(
        self, executor: Executor
    ) -> None:
        link = MountLink(executor, mount_config(indi_device_name="Some Other Mount"))
        assert link.device == "Some Other Mount"

    async def test_the_display_label_is_never_used_to_address_the_device(
        self, executor: Executor
    ) -> None:
        """device_label is translatable; addressing by it would break in French."""
        link = MountLink(executor, mount_config(device_label="Monture Wave 150i"))
        assert link.device == EQMOD_DEVICE

    async def test_the_module_hardcodes_no_device_name(self) -> None:
        """Prose may name the reference mount; code may not.

        Docstrings are excluded deliberately — explaining *why* the name is
        configuration requires saying what the name is.
        """
        import ast
        import inspect

        from nocturne.executor import mount as mount_module

        tree = ast.parse(inspect.getsource(mount_module))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        offenders = [text for text in literals if "EQMod" in text or "Wave 150i" in text]
        assert not offenders, f"the device name is hardcoded in mount.py: {offenders}"


class TestEverythingGoesThroughTheGovernor:
    """CLAUDE.md invariant 1, for this new path in particular."""

    async def test_the_link_holds_no_transport_handle(self) -> None:
        import inspect

        source = inspect.getsource(MountLink)
        assert "IndiClient" not in source
        assert "_client" not in source

    async def test_a_governor_that_refuses_stops_the_bring_up(
        self, executor: Executor, server: FakeIndiServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nocturne.safety import Command, Rejected, SafetyViolation
        from nocturne.safety import governor as governor_module
        from nocturne.safety.commands import SetProperty

        def refuse(command: Command, _config: object) -> Rejected:
            return Rejected(reason=f"no {command.describe()}", rule="test_rule")

        registry = dict(governor_module.COMMAND_RULES)
        registry[SetProperty] = (refuse,)
        monkeypatch.setattr(governor_module, "COMMAND_RULES", registry)

        link = MountLink(executor, mount_config())
        async with link:
            with pytest.raises(SafetyViolation):
                await link.bring_up()

        # Nothing reached the mount.
        assert slew_speeds(server) == {RA_SLEW_ELEMENT: 800.0, DE_SLEW_ELEMENT: 800.0}


async def _until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    """Wait until ``condition()`` is true, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition was still false after {timeout} s")
