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
from collections.abc import Callable
from typing import Final

import pytest
from dbus_next.service import ServiceInterface

from nocturne.executor import ekos
from tests.fixtures import fake_kstars
from tests.fixtures.ekos_interfaces import (
    EKOS_OPTICAL_TRAIN,
    EKOS_OPTICAL_TRAIN_PATH,
    STANDARD_INTERFACES,
    Interface,
    declared_interfaces,
    exported_child_nodes,
    exported_objects,
    mount_units_documentation,
    running_child_nodes,
    running_objects,
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


def mount() -> Interface:
    return declared_interfaces()[ekos.EKOS_MOUNT_INTERFACE]


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


class TestTheModulesAppearOnlyOnceEkosIsStarted:
    """Two captures of the same process, twenty minutes and one call apart.

    Before ``Ekos.start`` KStars exports five paths; after it, twenty-four. That
    is not a quirk to note in passing — it is why a single introspection can
    never establish that a module interface is absent, and why the bridge's
    containment check has to run after Ekos is up rather than at attach time.
    """

    #: Created when Ekos is started. Confirmed absent at rest, present running.
    MODULE_NODES: Final = ("Capture", "Focus", "Guide", "Align", "Mount")

    def test_they_were_absent_at_rest(self) -> None:
        children = exported_child_nodes()[ekos.EKOS_PATH]
        assert children == {"Scheduler"}, children
        for node in self.MODULE_NODES:
            assert node not in children

    def test_they_are_present_once_ekos_is_running(self) -> None:
        children = running_child_nodes()[ekos.EKOS_PATH]
        assert set(self.MODULE_NODES) <= children, sorted(children)
        assert "OpticalTrain" in children

    def test_the_running_capture_is_the_larger_of_the_two(self) -> None:
        """Assert the two captures are different documents, not the same file
        read twice — which is what a copy-paste slip in the fixture would be."""
        assert len(running_objects()) == 24
        assert len(exported_objects()) == 5

    @pytest.mark.parametrize("node", MODULE_NODES)
    def test_each_module_exports_the_interface_it_declares(self, node: str) -> None:
        name = f"org.kde.kstars.Ekos.{node}"
        exported = running_objects()[f"{ekos.EKOS_PATH}/{node}"]
        assert name in exported, sorted(set(exported) - STANDARD_INTERFACES)
        assert exported[name].method_names() == declared_interfaces()[name].method_names()


class TestTheDeclaredInterfacesAreTheExportedOnes:
    """The source tree can be trusted as the contract — measured, not assumed.

    Every KStars interface in the running capture matches its adaptor definition
    method-for-method. That is what makes the declared XML usable for planning
    M2 work without starting KStars each time.
    """

    def exported_pairs(self) -> list[tuple[str, str, Interface]]:
        """``(path, interface name, interface)`` for KStars' own interfaces."""
        return [
            (path, name, interface)
            for path, interfaces in running_objects().items()
            for name, interface in interfaces.items()
            if name.startswith("org.kde.kstars")
        ]

    def test_the_scan_found_the_interfaces_it_is_about_to_check(self) -> None:
        """Passing over an empty list is what a broken scan looks like."""
        names = {name for _, name, _ in self.exported_pairs()}
        assert len(names) >= 10, sorted(names)
        assert EKOS_OPTICAL_TRAIN in names

    def test_every_exported_interface_is_one_the_build_declares(self) -> None:
        declared = declared_interfaces()
        undeclared = sorted({name for _, name, _ in self.exported_pairs()} - set(declared))
        assert undeclared == []

    def test_every_exported_interface_matches_its_declaration(self) -> None:
        declared = declared_interfaces()
        differences = [
            (path, name)
            for path, name, interface in self.exported_pairs()
            if interface.method_names() != declared[name].method_names()
        ]
        assert differences == []

    def test_a_disagreement_would_be_detected(self) -> None:
        """The positive control for both checks above."""
        declared = declared_interfaces()
        interface = running_objects()[ekos.EKOS_PATH][ekos.EKOS_INTERFACE]
        assert interface.method_names() == declared[ekos.EKOS_INTERFACE].method_names()

        tampered = Interface(name=ekos.EKOS_INTERFACE, methods=dict(interface.methods))
        tampered.methods.pop("start")
        assert tampered.method_names() != declared[ekos.EKOS_INTERFACE].method_names()

    def test_some_declared_interfaces_are_never_exported(self) -> None:
        """Not a discrepancy — device-type interfaces for hardware the profile
        does not have. Recorded so their absence is not read as a problem."""
        exported = {name for _, name, _ in self.exported_pairs()}
        never = sorted(set(declared_interfaces()) - exported)
        assert never == [
            "org.kde.kstars.Ekos.Observatory",
            "org.kde.kstars.INDI.Dome",
            "org.kde.kstars.INDI.DustCap",
            "org.kde.kstars.INDI.LightBox",
            "org.kde.kstars.INDI.PAC",
            "org.kde.kstars.INDI.Weather",
        ]


class TestTheOpticalTrain:
    """The interface the Trixie decision was justified by, now exported."""

    def test_it_is_declared_by_the_built_tree(self) -> None:
        assert EKOS_OPTICAL_TRAIN in declared_interfaces()

    def test_it_is_exported_at_an_indexed_path(self) -> None:
        """ADR 0006 and ADR 0008 required KStars >= 3.8.2 for this. It is here,
        on the machine, rather than in release notes."""
        exported = running_objects()[EKOS_OPTICAL_TRAIN_PATH]
        assert EKOS_OPTICAL_TRAIN in exported
        assert "setCamera" in exported[EKOS_OPTICAL_TRAIN].methods

    def test_the_parent_path_is_a_bare_container(self) -> None:
        """``/KStars/Ekos/OpticalTrain`` carries no KStars interface at all.

        A train is addressed by id. Anything that ever calls this interface has
        to enumerate the children rather than assume the parent answers, and
        assuming otherwise is the kind of thing that reads as KStars being
        broken.
        """
        parent = running_objects()[f"{ekos.EKOS_PATH}/OpticalTrain"]
        assert {name for name in parent if name.startswith("org.kde")} == set()
        assert running_child_nodes()[f"{ekos.EKOS_PATH}/OpticalTrain"] == {"1"}

    def test_the_bridge_does_not_touch_it(self) -> None:
        """Using it is M2. Recording that it exists is M1."""
        source = inspect.getsource(ekos)
        assert "OpticalTrain" not in source.replace(EKOS_OPTICAL_TRAIN, "")

    def test_the_bridge_names_only_the_module_it_drives(self) -> None:
        """The M2 boundary, asserted rather than intended.

        ``Mount`` is named because Nocturne drives it. The other four are not,
        and each arrives with the commands that use it and the safety rules that
        constrain it — never as a proxy handle somebody might find and call.
        """
        source = inspect.getsource(ekos)
        assert ekos.EKOS_MOUNT_INTERFACE in source
        for node in ("Capture", "Focus", "Guide", "Align", "Scheduler"):
            assert f"org.kde.kstars.Ekos.{node}" not in source

    def test_that_boundary_check_can_fail(self) -> None:
        """The positive control: the same detector, over a source that does."""
        offender = 'INTERFACE = "org.kde.kstars.Ekos.Capture"'
        assert any(f"org.kde.kstars.Ekos.{node}" in offender for node in ("Capture", "Focus"))


class TestTheMountModuleTheBridgeDrives:
    """``/KStars/Ekos/Mount`` — the first Ekos module Nocturne actually uses.

    Everything here is checked against the recording, never against
    ``fake_kstars.py``. The stub is checked *by* it, exactly as the INDI stub is.
    """

    def test_the_path_is_a_child_of_the_ekos_object(self) -> None:
        assert f"{ekos.EKOS_PATH}/Mount" == ekos.EKOS_MOUNT_PATH
        assert "Mount" in running_child_nodes()[ekos.EKOS_PATH]

    def test_the_path_exports_the_interface_the_bridge_asks_for(self) -> None:
        exported = running_objects()[ekos.EKOS_MOUNT_PATH]
        assert ekos.EKOS_MOUNT_INTERFACE in exported, sorted(
            set(exported) - STANDARD_INTERFACES
        )

    def test_it_did_not_exist_before_ekos_was_started(self) -> None:
        """Why ``attach_modules`` is separate from ``connect``, as evidence."""
        assert ekos.EKOS_MOUNT_PATH not in exported_objects()
        assert "Mount" not in exported_child_nodes()[ekos.EKOS_PATH]

    def test_every_required_mount_method_exists(self) -> None:
        missing = sorted(ekos.REQUIRED_MOUNT_METHODS - mount().method_names())
        assert not missing, missing

    def test_the_required_set_is_not_empty(self) -> None:
        """An empty set has no missing members either."""
        assert len(ekos.REQUIRED_MOUNT_METHODS) == 9
        assert "slew" in ekos.REQUIRED_MOUNT_METHODS

    def test_a_mount_method_kstars_does_not_have_is_reported_missing(self) -> None:
        """The positive control for the check above."""
        invented = frozenset({"slew", "flipNow"})
        assert sorted(invented - mount().method_names()) == ["flipNow"]

    def test_slew_takes_two_doubles_and_returns_a_boolean(self) -> None:
        overloads = mount().methods["slew"]
        assert len(overloads) == 1
        assert overloads[0].in_signature == "dd"
        assert overloads[0].out_signature == "b"
        assert overloads[0].argument_names == ("", "RA", "DEC")

    @pytest.mark.parametrize("member", ["park", "unpark", "abort"])
    def test_the_others_take_nothing_and_return_a_boolean(self, member: str) -> None:
        overloads = mount().methods[member]
        assert len(overloads) == 1
        assert overloads[0].in_signature == ""
        assert overloads[0].out_signature == "b"


class TestTheBridgeDrivesTheWholeMountInterface:
    """ADR 0023 replaced a partition with a covering, and this is the covering.

    ADR 0022 called four of the nine methods and refused five by name. The
    operator's decision was that all nine be implemented, each with a rule
    rather than a refusal — so what has to hold now is that
    ``REQUIRED_MOUNT_METHODS`` *is* the recorded interface, exactly. Not a
    subset, which would leave a call nobody decided about, and not a superset,
    which would be a name KStars does not have.
    """

    def test_it_is_exactly_what_kstars_declares(self) -> None:
        assert mount().method_names() == ekos.REQUIRED_MOUNT_METHODS

    def test_that_is_nine_methods(self) -> None:
        """Two empty sets are equal too."""
        assert len(ekos.REQUIRED_MOUNT_METHODS) == 9

    def test_the_five_that_used_to_be_refused_are_among_them(self) -> None:
        """Named, so that a later edit dropping one is a failure and not a diff."""
        assert {
            "sync",
            "syncTarget",
            "gotoTarget",
            "resetModel",
            "setMeridianFlipValues",
        } <= ekos.REQUIRED_MOUNT_METHODS

    def test_a_method_kstars_gained_would_be_caught(self) -> None:
        """The positive control, and the reason equality is asserted rather
        than containment: a KStars release adding a method has to fail here."""
        gained = mount().method_names() | {"flipNow"}
        assert gained != ekos.REQUIRED_MOUNT_METHODS

    def test_a_method_the_bridge_dropped_would_be_caught_too(self) -> None:
        dropped = ekos.REQUIRED_MOUNT_METHODS - {"sync"}
        assert dropped != mount().method_names()

    def test_every_one_is_reachable_through_a_bridge_method(self) -> None:
        """A name in the required set that nothing calls is a name that was
        added to make this file pass rather than to be used."""
        source = inspect.getsource(ekos)
        for member in sorted(ekos.REQUIRED_MOUNT_METHODS):
            assert f'"{member}"' in source, member

    def test_that_reachability_check_can_fail(self) -> None:
        assert '"flipNow"' not in inspect.getsource(ekos)


class TestTheMountPropertiesThatMakeCommandsJudgeable:
    """Two reads, and three commands that cannot be decided without them."""

    def test_both_are_declared_by_the_module(self) -> None:
        missing = sorted(ekos.REQUIRED_MOUNT_PROPERTIES - set(mount().properties))
        assert not missing, missing

    def test_the_set_is_not_empty(self) -> None:
        assert {"equatorialCoords", "parkStatus"} == ekos.REQUIRED_MOUNT_PROPERTIES

    def test_a_property_kstars_does_not_have_is_reported_missing(self) -> None:
        """The positive control."""
        invented = frozenset({"parkStatus", "isFlipping"})
        assert sorted(invented - set(mount().properties)) == ["isFlipping"]

    def test_the_coordinates_come_back_as_an_array_of_doubles(self) -> None:
        assert mount().properties["equatorialCoords"] == "ad"

    def test_the_park_status_is_an_integer_with_no_names_attached(self) -> None:
        """Which is exactly why ``PARK_STATUS_PARKED`` is a recorded constant.

        The DBus type is a bare ``i``. Nothing in the interface says which
        integer means parked, and getting it wrong would let a model reset
        through on a tracking mount.
        """
        assert mount().properties["parkStatus"] == "i"
        assert ekos.PARK_STATUS_PARKED == 1


class TestTheCatalogueLookups:
    """Resolving a name is what turns ``gotoTarget`` into a pointing decision."""

    def test_the_path_is_kstars_own_object(self) -> None:
        assert ekos.KSTARS_PATH == "/KStars"
        assert ekos.KSTARS_INTERFACE == "org.kde.kstars"

    def test_it_exists_before_ekos_is_started(self) -> None:
        """Unlike the module objects. This is why it attaches at connect time."""
        assert ekos.KSTARS_PATH in exported_objects()
        assert ekos.KSTARS_INTERFACE in exported_objects()[ekos.KSTARS_PATH]

    def test_both_lookups_exist(self) -> None:
        declared = declared_interfaces()[ekos.KSTARS_INTERFACE]
        missing = sorted(ekos.REQUIRED_KSTARS_METHODS - declared.method_names())
        assert not missing, missing

    def test_the_set_is_not_empty(self) -> None:
        assert len(ekos.REQUIRED_KSTARS_METHODS) == 2

    def test_a_lookup_kstars_does_not_have_is_reported_missing(self) -> None:
        declared = declared_interfaces()[ekos.KSTARS_INTERFACE]
        assert "resolveTarget" not in declared.method_names()

    def test_each_takes_a_name_and_returns_a_document(self) -> None:
        declared = declared_interfaces()[ekos.KSTARS_INTERFACE]
        for member in sorted(ekos.REQUIRED_KSTARS_METHODS):
            single_argument = [
                overload
                for overload in declared.methods[member]
                if overload.in_signature == "s"
            ]
            assert single_argument, f"{member} has no one-name overload"
            assert single_argument[0].out_signature == "s"

    def test_getObjectDataXML_is_overloaded_and_the_bridge_uses_the_short_one(  # noqa: N802
        self,
    ) -> None:
        """It takes one, two or three arguments; the extra two ask KStars to go
        to the internet for a name it does not know. The bridge calls it with
        one, which keeps a catalogue lookup a local operation."""
        overloads = declared_interfaces()[ekos.KSTARS_INTERFACE].methods["getObjectDataXML"]
        assert {overload.in_signature for overload in overloads} == {"s", "sb", "sbb"}


class TestTheUnitsWereReadFromTheSourceNotAssumed:
    """The one thing introspection cannot record, recorded anyway.

    ``slew(RA: d, DEC: d)`` is all the XML says. Whether that first double is
    hours or degrees is a factor of fifteen between the target and somewhere
    else, and no signature check would ever catch it.
    """

    def test_the_recording_says_right_ascension_is_in_hours(self) -> None:
        text = mount_units_documentation()
        assert "@param RA Right ascention is hours." in text
        assert "@param DEC Declination in degrees." in text

    def test_the_recording_says_the_frame_is_jnow(self) -> None:
        assert "Slew the mount to the RA/DEC (JNow)." in mount_units_documentation()

    def test_the_recording_says_the_boolean_means_sent_not_done(self) -> None:
        assert "true if the command is sent successfully" in mount_units_documentation()

    def test_the_bridge_documents_the_same_units(self) -> None:
        """A future editor changing one without the other fails here."""
        docstring = inspect.getdoc(ekos.EkosBridge.mount_slew) or ""
        assert "hours" in docstring
        assert "degrees" in docstring
        assert "JNow" in docstring

    def test_it_covers_every_method_the_bridge_calls(self) -> None:
        text = mount_units_documentation()
        for member in sorted(ekos.REQUIRED_MOUNT_METHODS):
            assert f"{member}(" in text, member

    def test_the_detector_would_notice_a_missing_declaration(self) -> None:
        """The positive control for the check above."""
        assert "flipNow(" not in mount_units_documentation()


class TestTheFlipOffsetIsInDegreesAndTheXMLSaysHours:
    """The one place the recorded interface is not merely silent but wrong.

    ``kstars-dbus-interfaces.xml`` names the second argument of
    ``setMeridianFlipValues`` ``hours``. It is degrees. Everything in this
    repository that trusts the introspection XML — which is nearly everything,
    deliberately — would have taken that name at face value, and a flip
    configured in hours where degrees were wanted is out by a factor of fifteen
    in the direction that flips too late.
    """

    def test_the_xml_really_does_say_hours(self) -> None:
        """The defect, asserted against the recording rather than described."""
        recorded = mount().methods["setMeridianFlipValues"][0]
        assert recorded.argument_names == ("activate", "hours")

    def test_the_source_says_degrees(self) -> None:
        text = mount_units_documentation()
        assert "void Mount::setMeridianFlipValues(bool activate, double degrees)" in text
        assert "meridianFlipOffsetDegrees->setValue(degrees);" in text

    def test_the_arithmetic_that_settles_it_is_recorded_too(self) -> None:
        """A doxygen comment can be stale. The division cannot."""
        assert "double offset = rangeHA(m_offset / 15.0);" in mount_units_documentation()

    def test_the_bridge_names_its_argument_for_the_unit(self) -> None:
        signature = inspect.signature(ekos.EkosBridge.mount_set_meridian_flip)
        assert "offset_deg" in signature.parameters
        assert "hours" not in signature.parameters

    def test_the_command_does_too(self) -> None:
        from nocturne.safety import SetMeridianFlipOffset

        assert "offset_deg" in SetMeridianFlipOffset.model_fields
        assert "hours" not in SetMeridianFlipOffset.model_fields

    def test_the_park_status_values_are_recorded(self) -> None:
        text = mount_units_documentation()
        assert "PARK_UNKNOWN, PARK_PARKED, PARK_PARKING" in text
        assert "PARK_PARKED is 1." in text

    def test_the_catalogue_documents_are_recorded_in_degrees(self) -> None:
        text = mount_units_documentation()
        assert 'writeTextElement("RA_Degrees"' in text
        assert "86.4 seconds" in text


class TestTheMountStubMatchesTheRecordedSignatures:
    """The stub cannot be allowed to agree with the bridge instead of KStars."""

    def stub_signature(self, member: str) -> tuple[str, str]:
        annotations = dict(
            inspect.get_annotations(getattr(fake_kstars.FakeMountInterface, member))
        )
        out = annotations.pop(_RETURN, "")
        return "".join(annotations.values()), out

    @pytest.mark.parametrize(
        "member",
        ["slew", "sync", "gotoTarget", "syncTarget", "park", "unpark", "abort", "resetModel"],
    )
    def test_it_declares_what_kstars_declares(self, member: str) -> None:
        recorded = mount().methods[member][0]
        assert self.stub_signature(member) == (
            recorded.in_signature,
            recorded.out_signature,
        )

    def test_the_stub_offers_everything_the_bridge_requires(self) -> None:
        exported = {
            name
            for name, member in inspect.getmembers(
                fake_kstars.FakeMountInterface, inspect.isfunction
            )
            if not name.startswith("_")
        }
        assert exported >= ekos.REQUIRED_MOUNT_METHODS, sorted(exported)

    def test_the_flip_setter_declares_what_kstars_declares(self) -> None:
        """Separately, because it is the one that returns nothing.

        KStars marks it ``NoReply``, so the stub must too — and because it takes
        the argument KStars misnames, this is where the misnaming is pinned to
        the recording rather than to anyone's memory of it.
        """
        recorded = mount().methods["setMeridianFlipValues"][0]
        assert recorded.in_signature == "bd"
        assert recorded.out_signature == ""
        assert self.stub_signature("setMeridianFlipValues") == ("bd", "")

    def test_the_stub_reports_a_position_and_a_park_status(self) -> None:
        """Both are properties rather than methods, and dbus-next declares them
        differently — so a stub that got that wrong would export nothing and the
        bridge's containment check would fail for a confusing reason."""
        for member in sorted(ekos.REQUIRED_MOUNT_PROPERTIES):
            assert hasattr(fake_kstars.FakeMountInterface, member)

    def test_the_stub_park_status_starts_unknown(self) -> None:
        """PARK_UNKNOWN, which is what a driver that has not spoken reports —
        and it must not read as parked. A stub starting at PARK_PARKED would
        make every model-reset test pass without the rule doing anything."""
        assert fake_kstars.FakeMountInterface().park_status != ekos.PARK_STATUS_PARKED

    def test_the_minimal_stub_really_is_missing_something(self) -> None:
        """It exists to make the bridge's containment check fire; if it offered
        everything, the test built on it would prove the opposite of its name."""
        exported = {
            name
            for name, member in inspect.getmembers(
                fake_kstars.MinimalMountInterface, inspect.isfunction
            )
            if not name.startswith("_")
        }
        assert ekos.REQUIRED_MOUNT_METHODS - exported


class TestTheBridgeNamesAccessorsTheWayDbusNextDoes:
    """A defect this suite did not catch until a method with an acronym in it.

    ``_call`` looks up the accessor dbus-next generated, by converting the DBus
    member name the same way. It used to do that with a rule of its own — an
    underscore before every capital — which is right for ``connectDevices`` and
    wrong for ``getObjectDataXML``: dbus-next keeps a run of capitals together
    and produces ``get_object_data_xml``, the naive rule produces
    ``get_object_data_x_m_l``, and the bridge reports "not available on this
    KStars build" for a method KStars plainly has.

    Every member the bridge had called until then happened to have no capital
    run, so the rule was correct for everything it was ever asked. That is
    exactly the shape of bug this file exists for, so the check is now against
    dbus-next itself and over every name in the recording rather than over an
    example somebody thought of.
    """

    def member_names(self) -> list[str]:
        """Every method and property name in the recorded interfaces."""
        names = {
            name
            for interfaces in running_objects().values()
            for interface_name, interface in interfaces.items()
            if interface_name.startswith("org.kde.kstars")
            for name in (*interface.methods, *interface.properties)
        }
        return sorted(names)

    def test_the_recording_has_names_to_check(self) -> None:
        """An empty list agrees with anything."""
        names = self.member_names()
        assert len(names) >= 100, len(names)
        assert "getObjectDataXML" in names

    def test_it_agrees_with_dbus_next_on_every_recorded_name(self) -> None:
        from dbus_next.proxy_object import BaseProxyInterface

        # dbus-next is untyped, and this is the one place its private
        # conversion is called. Reaching for it is the point: the authority on
        # what accessor dbus-next generates is dbus-next.
        convert: Callable[[str], str] = BaseProxyInterface._to_snake_case
        disagreements = [
            (name, ekos._snake(name), convert(name))
            for name in self.member_names()
            if ekos._snake(name) != convert(name)
        ]
        assert disagreements == []

    def test_the_rule_that_used_to_be_here_does_not_agree(self) -> None:
        """The positive control, and it is the real historical defect.

        Without this, the comparison above would pass just as happily over a
        conversion nobody had ever got wrong.
        """

        def naive(member: str) -> str:
            out: list[str] = []
            for index, character in enumerate(member):
                if character.isupper() and index:
                    out.append("_")
                out.append(character.lower())
            return "".join(out)

        assert naive("getObjectDataXML") == "get_object_data_x_m_l"
        assert ekos._snake("getObjectDataXML") == "get_object_data_xml"
        assert [name for name in self.member_names() if naive(name) != ekos._snake(name)]

    @pytest.mark.parametrize(
        ("member", "accessor"),
        [
            ("start", "start"),
            ("stop", "stop"),
            ("connectDevices", "connect_devices"),
            ("getDevices", "get_devices"),
            ("slew", "slew"),
            ("setMeridianFlipValues", "set_meridian_flip_values"),
            ("equatorialCoords", "equatorial_coords"),
            ("parkStatus", "park_status"),
        ],
    )
    def test_the_names_without_a_capital_run_are_unchanged_by_the_fix(
        self, member: str, accessor: str
    ) -> None:
        """Which is why it went unnoticed: every earlier call was unaffected.

        These are spelled out rather than derived, so that a future rewrite of
        the conversion has to keep working for the calls that already do.
        """
        assert ekos._snake(member) == accessor
