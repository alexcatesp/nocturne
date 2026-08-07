"""Every DBus name in the bridge must appear in the interface recorded from KStars.

This is what closes https://github.com/alexcatesp/nocturne/issues/2. Until the
reference rig had a KStars to ask, ``nocturne/executor/ekos.py`` named paths,
interfaces and methods from expectation, and ``fake_kstars.py`` implemented the
same expectations — so the bridge and its stub agreed with each other and
neither agreed with anything real.

One of the guesses was wrong. ``org.kde.kstars.INDI.connect`` is
``connect(host: s, port: i) -> b`` — it attaches KStars' INDI client to an
indiserver — and the bridge was calling it with a device name, from a method
called ``connect_device``. Every test passed, because the stub declared
``connect(device: s)``.

So the assertions here run against
``backend/tests/fixtures/hardware/kstars-dbus-interfaces.xml`` and
``kstars-live-at-rest.xml``, captured 2026-08-07 from KStars 3.8.3
(``docs/ekos-dbus-capture.md``), and never against the stub. The stub is checked
*by* them instead: its signatures have to be the recorded ones.

Several of these assert an absence — no name is unrecorded, no signature
disagrees — so each carries a positive control that points the same detector at
something known to be wrong (CLAUDE.md section 2).
"""

from __future__ import annotations

import inspect
from typing import Final

import pytest
from dbus_next.service import ServiceInterface

from nocturne.executor import ekos
from tests.fixtures import fake_kstars
from tests.fixtures.ekos_interfaces import (
    EKOS_OPTICAL_TRAIN,
    STANDARD_INTERFACES,
    Interface,
    declared_interfaces,
    exported_child_nodes,
    exported_objects,
)

#: dbus-next turns a Python annotation into a DBus type by taking it literally,
#: so a stub method's signature is readable straight off its annotations.
_RETURN = "return"


def offending_stub() -> type[ServiceInterface]:
    """``FakeIndiInterface.connect`` as it was before the interface was recorded.

    The annotations are assigned rather than written as syntax. This test module
    uses ``from __future__ import annotations``, which would store ``"s"`` as the
    string ``"'s'"`` — quotes and all — whereas ``fake_kstars.py`` deliberately
    does not, so dbus-next reads its annotations as the signatures they are.
    Writing the offender the natural way would therefore have produced a
    mismatch for the wrong reason, and a positive control that fires for the
    wrong reason is not a control.
    """

    class WrongIndiInterface(ServiceInterface):
        def connect(self, device):  # type: ignore[no-untyped-def]  # signature set below
            raise NotImplementedError

    WrongIndiInterface.connect.__annotations__ = {"device": "s", "return": "b"}
    return WrongIndiInterface


def indi() -> Interface:
    return declared_interfaces()[ekos.INDI_INTERFACE]


def ekos_interface() -> Interface:
    return declared_interfaces()[ekos.EKOS_INTERFACE]


class TestTheRecordingIsUsable:
    """Without these, every assertion below could pass over an empty parse."""

    def test_the_declared_dump_has_the_interfaces_it_should(self) -> None:
        names = set(declared_interfaces())
        assert ekos.EKOS_INTERFACE in names
        assert ekos.INDI_INTERFACE in names
        assert len(names) == 19, sorted(names)

    def test_the_exported_dump_has_the_paths_it_should(self) -> None:
        paths = set(exported_objects())
        assert {ekos.EKOS_PATH, ekos.INDI_PATH} <= paths

    def test_the_interfaces_are_not_empty(self) -> None:
        assert len(ekos_interface().methods) >= 10
        assert len(indi().methods) >= 10

    def test_a_name_that_is_not_in_the_recording_is_not_found(self) -> None:
        """The positive control for the lookups: the detector must miss."""
        assert "org.kde.kstars.Ekos.Imaginary" not in declared_interfaces()
        assert "connectEverything" not in ekos_interface().methods


class TestTheBridgesConstantsWereReadNotGuessed:
    def test_the_bus_name_is_the_one_kstars_registers(self) -> None:
        """Verified on the rig: ``ListNames`` returns this and nothing else
        under ``org.kde.*`` — no per-process ``org.kde.kstars-<pid>``."""
        assert ekos.KSTARS_BUS_NAME == "org.kde.kstars"

    @pytest.mark.parametrize(
        ("path", "interface"),
        [
            (ekos.EKOS_PATH, ekos.EKOS_INTERFACE),
            (ekos.INDI_PATH, ekos.INDI_INTERFACE),
        ],
    )
    def test_each_path_exports_the_interface_the_bridge_asks_for(
        self, path: str, interface: str
    ) -> None:
        exported = exported_objects()
        assert path in exported, f"{path} was not exported by the running KStars"
        assert interface in exported[path], (
            f"{path} exports {sorted(set(exported[path]) - STANDARD_INTERFACES)}, "
            f"not {interface}"
        )

    def test_every_required_ekos_method_exists(self) -> None:
        missing = sorted(ekos.REQUIRED_EKOS_METHODS - ekos_interface().method_names())
        assert not missing, missing

    def test_every_required_indi_method_exists(self) -> None:
        missing = sorted(ekos.REQUIRED_INDI_METHODS - indi().method_names())
        assert not missing, missing

    def test_the_required_sets_are_not_empty(self) -> None:
        """Two empty sets have no missing members either."""
        assert len(ekos.REQUIRED_EKOS_METHODS) >= 4
        assert len(ekos.REQUIRED_INDI_METHODS) >= 3

    def test_a_method_kstars_does_not_have_is_reported_missing(self) -> None:
        """The positive control for the two checks above."""
        invented = frozenset({"start", "warpDrive"})
        assert sorted(invented - ekos_interface().method_names()) == ["warpDrive"]

    def test_the_declared_and_exported_interfaces_agree(self) -> None:
        """The source XML says what KStars declares; introspection says what it
        exported. For these two objects they must be the same interface."""
        exported = exported_objects()
        for path, name in (
            (ekos.EKOS_PATH, ekos.EKOS_INTERFACE),
            (ekos.INDI_PATH, ekos.INDI_INTERFACE),
        ):
            live = exported[path][name].method_names()
            assert declared_interfaces()[name].method_names() == live, path


class TestINDIConnectIsNotAPerDeviceConnect:
    """The defect issue #2 existed to find, kept where it cannot be undone."""

    def test_connect_takes_a_host_and_a_port(self) -> None:
        overloads = indi().methods["connect"]
        assert len(overloads) == 1
        assert overloads[0].in_signature == "si"
        assert overloads[0].out_signature == "b"
        assert "host" in overloads[0].argument_names
        assert "port" in overloads[0].argument_names

    def test_disconnect_takes_a_host_and_a_port(self) -> None:
        overloads = indi().methods["disconnect"]
        assert len(overloads) == 1
        assert overloads[0].in_signature == "si"

    def test_a_single_string_would_not_satisfy_it(self) -> None:
        """The positive control: the call the bridge used to make.

        ``connect_device("Focuser Simulator")`` sent one string to a method
        wanting a string and an int32.
        """
        assert indi().methods["connect"][0].in_signature != "s"

    def test_nothing_on_the_indi_interface_connects_one_device(self) -> None:
        """There is no per-device connect to have meant. Devices are connected
        for the whole profile by ``Ekos.connectDevices``, or one at a time by
        writing ``CONNECTION`` through IndiClient — which the safety governor
        gates and this interface does not."""
        per_device = [
            name
            for name, overloads in indi().methods.items()
            if name in {"connect", "disconnect"}
            and any(overload.in_signature == "s" for overload in overloads)
        ]
        assert per_device == []

    def test_the_scan_saw_the_methods_it_was_looking_at(self) -> None:
        """The check above passes by finding nothing; this proves it looked."""
        assert {"connect", "disconnect"} <= set(indi().methods)

    def test_the_bridge_exposes_the_indiserver_call_by_its_real_name(self) -> None:
        signature = inspect.signature(ekos.EkosBridge.connect_indiserver, eval_str=True)
        assert list(signature.parameters) == ["self", "host", "port"]
        assert signature.parameters["host"].annotation is str
        assert signature.parameters["port"].annotation is int


class TestTheStubMatchesTheRecordedSignatures:
    """The stub cannot be allowed to drift back into agreeing with the bridge."""

    def stub_signature(self, interface: type[ServiceInterface], member: str) -> tuple[str, str]:
        """``(in signature, out signature)`` as dbus-next reads the stub."""
        annotations = dict(inspect.get_annotations(getattr(interface, member)))
        out = annotations.pop(_RETURN, "")
        return "".join(annotations.values()), out

    @pytest.mark.parametrize("member", ["getDevices", "connect", "disconnect"])
    def test_the_indi_stub_declares_what_kstars_declares(self, member: str) -> None:
        recorded = indi().methods[member][0]
        assert self.stub_signature(fake_kstars.FakeIndiInterface, member) == (
            recorded.in_signature,
            recorded.out_signature,
        )

    @pytest.mark.parametrize("member", ["start", "stop", "connectDevices", "disconnectDevices"])
    def test_the_ekos_stub_declares_what_kstars_declares(self, member: str) -> None:
        """All four are void and take nothing; KStars marks them NoReply."""
        recorded = ekos_interface().methods[member][0]
        assert recorded.in_signature == ""
        assert self.stub_signature(fake_kstars.FakeEkosInterface, member) == ("", "")

    def test_a_stub_that_disagrees_is_detected(self) -> None:
        """The positive control, and it is the real historical defect.

        This is what ``FakeIndiInterface.connect`` looked like before the
        interface was recorded. The comparison above has to reject it.
        """
        offender = offending_stub()
        recorded = indi().methods["connect"][0]

        assert self.stub_signature(offender, "connect") == ("s", "b")
        assert self.stub_signature(offender, "connect") != (
            recorded.in_signature,
            recorded.out_signature,
        )

    def test_the_stub_module_does_not_use_postponed_annotations(self) -> None:
        """A trap worth a test of its own.

        dbus-next reads ``__annotations__`` as DBus signature strings. Adding
        ``from __future__ import annotations`` to ``fake_kstars.py`` would turn
        every ``"s"`` into the string ``"'s'"`` — quotes included — and the stub
        would export nonsense signatures. The module says so in a comment; this
        makes it fail rather than be read.
        """
        statements = [line.strip() for line in inspect.getsource(fake_kstars).splitlines()]
        assert statements, "the stub module has no source"
        assert "from __future__ import annotations" not in statements
        # The consequence, asserted directly: an "s" that is still an "s".
        assert inspect.get_annotations(fake_kstars.FakeIndiInterface.connect)["host"] == "s"


class TestWhatIsNotYetRecorded:
    """Honest about the edges of the capture, so nobody reads more into it."""

    #: Created when Ekos is *started*, so absent from a capture taken at rest.
    MODULE_NODES: Final = ("Capture", "Focus", "Guide", "Align", "Mount")

    def test_the_ekos_module_objects_were_not_exported_at_rest(self) -> None:
        children = exported_child_nodes()[ekos.EKOS_PATH]
        assert children == {"Scheduler"}, children
        for node in self.MODULE_NODES:
            assert node not in children

    def test_but_their_interfaces_are_declared_by_the_build(self) -> None:
        """So they are not unknowns — they are unstarted."""
        declared = set(declared_interfaces())
        for node in self.MODULE_NODES:
            assert f"org.kde.kstars.Ekos.{node}" in declared

    def test_the_optical_train_interface_is_present_in_the_built_tree(self) -> None:
        """The interface ADR 0006 and ADR 0008 cite as the reason for requiring
        KStars >= 3.8.2. Confirmed in the tree that was built, rather than taken
        from release notes."""
        assert EKOS_OPTICAL_TRAIN in declared_interfaces()

    def test_the_bridge_does_not_touch_the_optical_train_yet(self) -> None:
        """Using it is M2. Recording that it exists is M1."""
        source = inspect.getsource(ekos)
        assert "OpticalTrain" not in source.replace(EKOS_OPTICAL_TRAIN, "")
