"""The enforcement invariant — CLAUDE.md invariant 1, SPEC section 9.

"Every command reaching the executor has passed safety.validate(). There is no
other path." This suite attempts to find another path.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nocturne.executor import Executor, IndiClient, IndiSettings
from nocturne.safety import (
    COMMAND_RULES,
    Approval,
    Command,
    ConnectDevice,
    Decision,
    Rejected,
    SafetyGovernor,
    SafetyViolation,
    SetProperty,
)
from nocturne.schemas import load_config_bundle
from tests.fixtures.fake_indi import FakeIndiServer

DEVICE = "Focuser Simulator"
POSITION = "ABS_FOCUS_POSITION"
ELEMENT = "FOCUS_ABSOLUTE_POSITION"

FAST = IndiSettings(
    connect_timeout_s=2.0,
    property_timeout_s=2.0,
    write_timeout_s=2.0,
    device_connect_timeout_s=2.0,
    reconnect_initial_delay_s=0.01,
    reconnect_max_delay_s=0.05,
)

#: Public methods of Executor that change instrument state. Every one of them
#: must be shown to pass through the governor.
MUTATING_METHODS = ("connect_device", "disconnect_device", "set_property")


class GovernorSpy:
    """Records what a real governor was asked, and can make it refuse.

    SafetyGovernor is ``@final`` on purpose — nothing subclasses the safety
    layer to soften it, not even a test — so this wraps an instance instead of
    deriving from one.
    """

    def __init__(self, governor: SafetyGovernor) -> None:
        self.governor = governor
        self.seen: list[Command] = []
        self.refuse = False
        self._original = governor.validate
        governor.validate = self._validate  # type: ignore[method-assign]

    def _validate(self, command: Command) -> Decision:
        self.seen.append(command)
        if self.refuse:
            return Rejected(reason="refused by the test", rule="test")
        return self._original(command)


@pytest.fixture
async def server() -> AsyncIterator[FakeIndiServer]:
    fake = FakeIndiServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
def governor(config_dir: Path) -> GovernorSpy:
    return GovernorSpy(SafetyGovernor(load_config_bundle(config_dir).safety))


@pytest.fixture
async def executor(server: FakeIndiServer, governor: GovernorSpy) -> AsyncIterator[Executor]:
    client = IndiClient(FAST.model_copy(update={"port": server.port}))
    running = Executor(client, governor.governor)
    await running.start()
    await running.wait_for_property(DEVICE, POSITION)
    try:
        yield running
    finally:
        await running.aclose()


class TestEveryCommandIsValidated:
    async def test_connect_device_is_validated(
        self, executor: Executor, governor: GovernorSpy
    ) -> None:
        await executor.connect_device(DEVICE)
        assert [type(c).__name__ for c in governor.seen] == ["ConnectDevice"]

    async def test_disconnect_device_is_validated(
        self, executor: Executor, governor: GovernorSpy
    ) -> None:
        await executor.connect_device(DEVICE)
        governor.seen.clear()
        await executor.disconnect_device(DEVICE)
        assert [type(c).__name__ for c in governor.seen] == ["DisconnectDevice"]

    async def test_set_property_is_validated(
        self, executor: Executor, governor: GovernorSpy
    ) -> None:
        await executor.set_property(DEVICE, POSITION, {ELEMENT: 1234.0})
        assert [type(c).__name__ for c in governor.seen] == ["SetProperty"]
        assert isinstance(governor.seen[0], SetProperty)
        assert governor.seen[0].values == {ELEMENT: 1234.0}

    @pytest.mark.parametrize("method_name", MUTATING_METHODS)
    async def test_no_mutating_method_bypasses_the_governor(
        self, executor: Executor, governor: GovernorSpy, method_name: str
    ) -> None:
        arguments: dict[str, tuple[object, ...]] = {
            "connect_device": (DEVICE,),
            "disconnect_device": (DEVICE,),
            "set_property": (DEVICE, POSITION, {ELEMENT: 1.0}),
        }
        governor.seen.clear()
        await getattr(executor, method_name)(*arguments[method_name])
        assert governor.seen, f"{method_name} did not consult the governor"

    def test_every_mutating_method_is_accounted_for(self) -> None:
        """A new mutating method added without a test here fails this test."""
        public = {
            name
            for name, member in inspect.getmembers(Executor, inspect.isfunction)
            if not name.startswith("_")
            and inspect.iscoroutinefunction(member)
            and name not in {"start", "aclose", "wait_for_property", "wait_for_device"}
        }
        assert public == set(MUTATING_METHODS)


class TestRejectionStopsTheCommand:
    async def test_a_refused_command_raises(
        self, executor: Executor, governor: GovernorSpy
    ) -> None:
        governor.refuse = True
        with pytest.raises(SafetyViolation, match="refused by the test"):
            await executor.set_property(DEVICE, POSITION, {ELEMENT: 99.0})

    async def test_a_refused_command_never_reaches_the_instrument(
        self, executor: Executor, governor: GovernorSpy, server: FakeIndiServer
    ) -> None:
        before = server.devices[DEVICE][POSITION].values[ELEMENT]
        governor.refuse = True
        with pytest.raises(SafetyViolation):
            await executor.set_property(DEVICE, POSITION, {ELEMENT: 99.0})
        assert server.devices[DEVICE][POSITION].values[ELEMENT] == before

    async def test_an_unregistered_command_is_refused(self, executor: Executor) -> None:
        class Sneaky(ConnectDevice):
            pass

        with pytest.raises(SafetyViolation, match="Sneaky"):
            await executor._execute(Sneaky(device=DEVICE), timeout=1.0)


class TestPerformRequiresAnApproval:
    async def test_perform_refuses_a_raw_command(self, executor: Executor) -> None:
        command = SetProperty(device=DEVICE, property=POSITION, values={ELEMENT: 1.0})
        with pytest.raises(SafetyViolation, match="Approval"):
            await executor._perform(command, timeout=1.0)  # type: ignore[arg-type]

    async def test_perform_refuses_a_look_alike(self, executor: Executor) -> None:
        class NotAnApproval:
            def __init__(self, command: Command) -> None:
                self.command = command

        fake = NotAnApproval(ConnectDevice(device=DEVICE))
        with pytest.raises(SafetyViolation, match="Approval"):
            await executor._perform(fake, timeout=1.0)  # type: ignore[arg-type]

    async def test_perform_accepts_a_governor_issued_approval(self, executor: Executor) -> None:
        approval = executor.governor.approve(
            SetProperty(device=DEVICE, property=POSITION, values={ELEMENT: 4321.0})
        )
        assert isinstance(approval, Approval)
        result = await executor._perform(approval, timeout=2.0)
        assert result is not None
        assert result[ELEMENT] == pytest.approx(4321.0)


class TestCommandRegistryMatchesTheExecutor:
    def test_every_registered_command_can_be_performed(self) -> None:
        """A command the governor allows but the executor cannot run is a gap."""
        assert COMMAND_RULES, (
            "the command registry is empty, so this loop checks nothing. "
            "Every command type the governor knows about belongs in it."
        )
        source = inspect.getsource(Executor._perform)
        for command_type in COMMAND_RULES:
            assert command_type.__name__ in source, (
                f"{command_type.__name__} is registered with the governor but is not "
                "handled by Executor._perform"
            )


class TestReadsAreNotGated:
    async def test_reading_does_not_consult_the_governor(
        self, executor: Executor, governor: GovernorSpy
    ) -> None:
        governor.seen.clear()
        executor.get_property(DEVICE, POSITION)
        executor.devices()
        executor.properties(DEVICE)
        executor.is_device_connected(DEVICE)
        await executor.wait_for_property(DEVICE, POSITION)
        assert governor.seen == []
