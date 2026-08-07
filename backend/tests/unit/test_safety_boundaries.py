"""Structural enforcement of the safety invariants — CLAUDE.md section 2.

The other safety tests exercise behaviour: give the governor a command, see
what it decides. These tests are about *shape*. They read the source tree and
assert that the paths CLAUDE.md forbids do not exist, so that a future
milestone cannot open one without a test going red.

What Python can and cannot do is worth stating plainly. No construction in this
language stops a determined caller with ``object.__setattr__`` and an import.
What these tests guarantee is that there is no *legitimate* path: no public
handle, no import, no writable configuration.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path

import pytest

from nocturne.executor import Executor
from nocturne.safety import (
    Command,
    ConnectDevice,
    Ok,
    Rejected,
    Rule,
    SafetyGovernor,
)
from nocturne.schemas import load_config_bundle, load_safety_config

PACKAGE_ROOT = Path(inspect.getfile(Executor)).resolve().parents[2]
EXECUTOR_PACKAGE = "nocturne.executor"

#: Methods that move the instrument. Anything exposing one of these to a caller
#: who has not been through the governor is a bypass.
MUTATING_TRANSPORT_METHODS = frozenset(
    {"write", "connect_device", "disconnect_device", "connect_devices", "start_ekos"}
)


#: Modules permitted to write to disk, and only ever for their own output.
#: M1 grants nothing; the entries are the ones SPEC assigns output to, added as
#: those milestones land. nocturne.agent, nocturne.api, nocturne.safety and
#: nocturne.schemas may never appear here — see the test below.
MODULES_THAT_MAY_WRITE: frozenset[str] = frozenset()

#: The configuration files. No module may write one, allowlisted or not.
CONFIGURATION_FILENAMES = ("equipment.yaml", "safety.yaml", "agent.yaml")

#: Calls that put bytes on disk.
WRITING_METHODS = frozenset(
    {"write_text", "write_bytes", "writelines", "unlink", "rename", "replace", "mkdir"}
)


def write_calls(path: Path) -> list[tuple[int, str]]:
    """Every call in ``path`` that writes to disk, as (line number, description)."""
    return write_calls_in_source(path.read_text(encoding="utf-8"))


def write_calls_in_source(source: str) -> list[tuple[int, str]]:
    """The detector itself, over source text, so it can be tested on a fixture."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        # Both forms matter. Path("x").write_text(...) is an Attribute call;
        # the builtin open("x", "w") is a Name call, and looking only at
        # Attribute calls missed it entirely — a module could have written
        # anywhere it liked through the builtin and this check would have said
        # nothing. Found by pointing the detector at a module that writes on
        # purpose; see TestTheDetectorsActuallyDetect.
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in WRITING_METHODS:
                found.append((node.lineno, f"{node.func.attr}()"))
            elif node.func.attr == "open" and _opens_for_writing(node, mode_index=0):
                # Path("x").open("w") — the mode is the first argument.
                found.append((node.lineno, "open(...)"))
        # open("x", "w") — the first argument is the path, the mode is second.
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and _opens_for_writing(node, mode_index=1)
        ):
            found.append((node.lineno, "open(...)"))
    return found


def _opens_for_writing(node: ast.Call, *, mode_index: int) -> bool:
    """Whether this open() call asks for a writable handle.

    ``mode_index`` is where the mode sits positionally, which differs between
    the two spellings: ``Path(p).open("w")`` puts it first, ``open(p, "w")``
    second. Reading the wrong slot means reading the *path* as if it were the
    mode — and any path containing w, a, x or + then looks like a write, which
    is how ``open("x")`` was briefly reported as one.
    """
    modes = [
        keyword.value.value
        for keyword in node.keywords
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant)
    ]
    if len(node.args) > mode_index:
        argument = node.args[mode_index]
        if isinstance(argument, ast.Constant):
            modes.append(argument.value)
    return any(character in str(mode) for mode in modes for character in "wax+")


#: Modules that certainly exist. If the scan cannot see these, it is not
#: looking at the package, and every verdict built on it is worthless.
KNOWN_MODULES = ("nocturne.safety.governor", "nocturne.executor.executor", "nocturne.main")


def python_sources() -> list[Path]:
    """Every module in the nocturne package.

    Raises rather than returning an empty or partial list. Every structural
    test below asks "did the scan find an offender?", and a scan that found
    nothing because it is broken is indistinguishable from a clean tree — the
    whole suite goes green while enforcing nothing. Making the helper raise
    turns that into five red tests instead of five silent passes.
    """
    found = sorted(PACKAGE_ROOT.glob("nocturne/**/*.py"))
    if not found:
        raise AssertionError(
            f"the source scan found no modules under {PACKAGE_ROOT}. Every "
            "structural test in this file depends on it; none of them mean "
            "anything until this is fixed."
        )
    seen = {module_name(path) for path in found}
    missing = [name for name in KNOWN_MODULES if name not in seen]
    if missing:
        raise AssertionError(
            f"the source scan did not see {missing}, which certainly exist. It "
            "is looking in the wrong place, and the structural tests below are "
            "not enforcing what they claim."
        )
    return found


def transport_imports(source: str, *, module: str) -> list[str]:
    """Imports of the INDI transport, which only the executor package may make."""
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "nocturne.executor.indi"
        ):
            offenders.append(f"{module} imports {node.module}")
        elif isinstance(node, ast.Import):
            offenders.extend(
                f"{module} imports {alias.name}"
                for alias in node.names
                if alias.name.startswith("nocturne.executor.indi")
            )
    return offenders


def mutating_transport_calls(source: str, *, module: str) -> list[str]:
    """Calls that reach the instrument around the Executor rather than through it."""
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MUTATING_TRANSPORT_METHODS
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr in {"client", "_client", "ekos", "_ekos"}
        ):
            offenders.append(f"{module}:{node.lineno} calls .{node.func.attr}()")
    return offenders


def code_string_literals(source: str) -> list[str]:
    """String constants a module *uses*, excluding docstrings.

    Prose has to be able to name a thing in order to explain why it is
    forbidden — the module docstring of nocturne.executor.link tabulates
    FILTER_NAME precisely to say the driver is wrong about it. Comments never
    reach the AST at all; docstrings do, and are excluded here.
    """
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def install_rules(monkeypatch: pytest.MonkeyPatch, rules: tuple[Rule, ...]) -> None:
    """Register ``rules`` against ConnectDevice for the duration of one test."""
    from nocturne.safety import governor as governor_module

    registry: dict[type[Command], tuple[Rule, ...]] = dict(governor_module.COMMAND_RULES)
    registry[ConnectDevice] = rules
    monkeypatch.setattr(governor_module, "COMMAND_RULES", registry)


#: A module that breaks every rule in this file at once. Each detector below is
#: pointed at it and must find its own offence. Kept as source text rather than
#: a file so that nothing can accidentally import it.
OFFENDING_SOURCE = """
from pathlib import Path

from nocturne.executor.indi.client import IndiClient


class Rogue:
    def go(self) -> None:
        self._client.write("EQMod Mount", "EQUATORIAL_EOD_COORD", {"RA": 5.0})
        self.ekos.connect_device("EQMod Mount")
        Path("config/safety.yaml").write_text("altitude_min_deg: 0")
        Path("/tmp/x").mkdir()
        with open("out.fits", "w") as handle:
            handle.writelines(["data"])
        if self.status["RARunning"] == "Busy":
            self.slew()
"""


class TestTheDetectorsActuallyDetect:
    """Before any verdict in this file is worth reading.

    Every test below asks "did the scan find an offender?", and answers "no" by
    passing. That makes a broken scan and a clean tree indistinguishable — the
    suite goes green while enforcing nothing at all. It is the same failure that
    made the bench-script motion test pass vacuously once the script stopped
    calling write().

    So each detector is pointed at a module that breaks the rule on purpose, and
    must catch it. If one of these fails, no other result in this file means
    anything.
    """

    def test_the_source_scan_sees_the_package(self) -> None:
        """The single point of failure: five tests below share this helper."""
        found = python_sources()
        assert len(found) > 10, found
        assert {module_name(path) for path in found} >= set(KNOWN_MODULES)

    def test_the_source_scan_refuses_to_look_in_the_wrong_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty scan must raise, not return nothing and let tests pass."""
        monkeypatch.setattr("tests.unit.test_safety_boundaries.PACKAGE_ROOT", tmp_path)
        with pytest.raises(AssertionError, match="found no modules"):
            python_sources()

    def test_the_write_detector_finds_every_way_of_writing(self) -> None:
        found = {description for _, description in write_calls_in_source(OFFENDING_SOURCE)}
        assert found >= {"write_text()", "mkdir()", "writelines()", "open(...)"}, found

    def test_the_write_detector_ignores_reads(self) -> None:
        """A detector that fires on everything is as useless as one that never does.

        The paths here are chosen to be nasty: "wax.txt" contains three
        characters that appear in write modes. Reading the wrong argument slot
        would report every one of these as a write.
        """
        innocent = (
            'Path("wax.txt").read_text()\n'
            'open("wax.txt")\n'
            'open("wax.txt", "r")\n'
            'open("wax.txt", mode="rb")\n'
            'Path("wax.txt").open("r")\n'
        )
        assert write_calls_in_source(innocent) == []

    def test_the_write_detector_finds_both_spellings_of_open(self) -> None:
        assert write_calls_in_source('open("f", "w")\n')
        assert write_calls_in_source('Path("f").open("w")\n')
        assert write_calls_in_source('open("f", mode="a")\n')

    def test_the_transport_import_detector_fires(self) -> None:
        assert transport_imports(OFFENDING_SOURCE, module="rogue")

    def test_the_transport_import_detector_ignores_the_facade(self) -> None:
        allowed = "from nocturne.executor import Executor\n"
        assert transport_imports(allowed, module="rogue") == []

    def test_the_mutating_call_detector_fires(self) -> None:
        found = mutating_transport_calls(OFFENDING_SOURCE, module="rogue")
        assert len(found) >= 2, found

    def test_the_mutating_call_detector_ignores_the_gated_methods(self) -> None:
        """Executor.set_property is the sanctioned path and must not be flagged."""
        allowed = 'self._executor.set_property("d", "P", {})\n'
        assert mutating_transport_calls(allowed, module="rogue") == []

    def test_the_configuration_filename_detector_fires(self) -> None:
        named = [name for name in CONFIGURATION_FILENAMES if name in OFFENDING_SOURCE]
        assert named
        assert write_calls_in_source(OFFENDING_SOURCE)

    def test_the_unresolved_property_detector_fires(self) -> None:
        names = TestRARunningIsNotTreatedAsMotion.UNRESOLVED_MOTION_PROPERTIES
        assert names, "the name list is empty; the test that uses it enforces nothing"
        assert any(name in OFFENDING_SOURCE for name in names)

    def test_the_motion_property_detector_fires(self) -> None:
        assert MOVING_PROPERTIES, "the list is empty; the bench test enforces nothing"
        written = properties_written_by_source(OFFENDING_SOURCE)
        assert written & set(MOVING_PROPERTIES)

    def test_every_name_list_this_file_relies_on_is_populated(self) -> None:
        """Emptying one of these silently disarms its test."""
        for label, names in (
            ("MUTATING_TRANSPORT_METHODS", MUTATING_TRANSPORT_METHODS),
            ("CONFIGURATION_FILENAMES", CONFIGURATION_FILENAMES),
            ("WRITING_METHODS", WRITING_METHODS),
            ("MOVING_PROPERTIES", MOVING_PROPERTIES),
            ("MUTATING_CALLS", MUTATING_CALLS),
            ("KNOWN_MODULES", KNOWN_MODULES),
        ):
            assert names, f"{label} is empty"


class TestTheExecutorExposesNoTransportHandle:
    """CLAUDE.md invariant 1: there is no other path to the instrument."""

    def test_no_public_attribute_exposes_a_mutating_transport(self) -> None:
        exposed: list[str] = []
        for name, member in inspect.getmembers(Executor):
            if name.startswith("_"):
                continue
            annotation = getattr(member, "fget", None)
            if annotation is None:
                continue
            returns = inspect.signature(annotation).return_annotation
            if any(method in str(returns) for method in ("IndiClient", "EkosBridge")):
                exposed.append(name)
        assert not exposed, (
            f"Executor.{', Executor.'.join(exposed)} hands a caller the raw transport, "
            "whose methods are not validated by the governor"
        )

    def test_the_transport_is_held_privately(self) -> None:
        source = inspect.getsource(Executor.__init__)
        assert "self._client" in source
        assert "self.client" not in source

    def test_the_governor_is_reachable_because_it_is_the_gate(self) -> None:
        """The governor itself is public on purpose: callers must be able to ask."""
        assert isinstance(inspect.getattr_static(Executor, "governor"), property)


class TestTheTransportIsConfinedToTheExecutorPackage:
    def test_no_module_outside_the_executor_imports_the_indi_client(self) -> None:
        offenders: list[str] = []
        for path in python_sources():
            name = module_name(path)
            if name.startswith(EXECUTOR_PACKAGE):
                continue
            offenders.extend(transport_imports(path.read_text(encoding="utf-8"), module=name))
        assert not offenders, (
            "the INDI transport must be reachable only from within "
            f"{EXECUTOR_PACKAGE}: {'; '.join(offenders)}"
        )

    def test_nothing_outside_the_executor_calls_a_mutating_transport_method(
        self,
    ) -> None:
        """A future module must go through Executor, not around it."""
        offenders: list[str] = []
        for path in python_sources():
            name = module_name(path)
            if name.startswith(EXECUTOR_PACKAGE) or name.startswith("nocturne.safety"):
                continue
            offenders.extend(
                mutating_transport_calls(path.read_text(encoding="utf-8"), module=name)
            )
        assert not offenders, "; ".join(offenders)


class TestConfigurationIsNotWritable:
    """CLAUDE.md invariant 2: the agent cannot write config/safety.yaml."""

    def test_only_allowlisted_modules_write_to_disk(self) -> None:
        """Writing is a capability granted per module, not an ambient one.

        M1 writes nothing. M3 onward will: the store writes SQLite, stacking
        writes FITS, telemetry writes session output. Rather than assert "no
        writes anywhere" — which would have to be weakened the moment M3
        landed, and a structural test that gets weakened under deadline
        pressure is one that gets deleted — the allowlist names who may write.
        Adding a module to it is a deliberate act with a reviewer attached.
        """
        offenders: list[str] = []
        for path in python_sources():
            name = module_name(path)
            if any(name.startswith(allowed) for allowed in MODULES_THAT_MAY_WRITE):
                continue
            for line, call in write_calls(path):
                offenders.append(f"{name}:{line} {call}")
        assert not offenders, (
            f"{'; '.join(offenders)}\n"
            "These modules write to disk but are not in MODULES_THAT_MAY_WRITE. "
            "If the write is legitimate, add the module to that list in this test "
            "on purpose. Never add nocturne.agent, nocturne.api or nocturne.safety."
        )

    def test_the_allowlist_never_covers_the_agent_the_api_or_the_safety_layer(
        self,
    ) -> None:
        """The three that must never hold a pen, whatever else the list grows."""
        never = ("nocturne.agent", "nocturne.api", "nocturne.safety", "nocturne.schemas")
        for forbidden in never:
            assert not any(
                allowed.startswith(forbidden) or forbidden.startswith(allowed)
                for allowed in MODULES_THAT_MAY_WRITE
            ), f"{forbidden} must never be granted write access"

    def test_no_module_may_write_a_configuration_file(self) -> None:
        """Unconditional, allowlist or not: nothing writes config/*.yaml.

        A module that both writes to disk and names a configuration file is
        refused even if it is allowed to write other things.
        """
        offenders: list[str] = []
        for path in python_sources():
            source = path.read_text(encoding="utf-8")
            named = [name for name in CONFIGURATION_FILENAMES if name in source]
            if named and write_calls(path):
                offenders.append(f"{module_name(path)} writes and names {named}")
        assert not offenders, "; ".join(offenders)

    def test_loading_the_configuration_does_not_change_it_on_disk(
        self, config_dir: Path
    ) -> None:
        """The behavioural counterpart to the source scan."""
        before = {
            name: hashlib.sha256((config_dir / name).read_bytes()).hexdigest()
            for name in CONFIGURATION_FILENAMES
        }
        governor = SafetyGovernor(load_config_bundle(config_dir).safety)
        governor.validate(ConnectDevice(device="CCD Simulator"))
        governor.require_autonomy_level("advisory")
        after = {
            name: hashlib.sha256((config_dir / name).read_bytes()).hexdigest()
            for name in CONFIGURATION_FILENAMES
        }
        assert before == after

    def test_the_loader_opens_configuration_read_only(self) -> None:
        from nocturne.schemas import loader

        source = inspect.getsource(loader)
        assert "read_text" in source
        assert "write_text" not in source

    def test_a_loaded_safety_config_cannot_be_mutated(self, config_dir: Path) -> None:
        from pydantic import ValidationError

        config = load_safety_config(config_dir / "safety.yaml")
        for target, field, value in (
            (config.limits, "altitude_min_deg", 0.0),
            (config.limits.meridian, "calibrated", True),
            (config.limits.meridian, "hour_angle_east_limit_deg", -90.0),
            (config.abort_conditions, "disk_free_min_gb", 0.0),
            (config.on_abort, "action", "park"),
        ):
            with pytest.raises(ValidationError, match="frozen"):
                setattr(target, field, value)

    def test_the_governor_holds_the_same_frozen_object(self, config_dir: Path) -> None:
        from pydantic import ValidationError

        governor = SafetyGovernor(load_config_bundle(config_dir).safety)
        with pytest.raises(ValidationError, match="frozen"):
            governor.config.limits.meridian.calibrated = True  # type: ignore[misc]

    def test_the_governor_offers_no_mutating_method(self, config_dir: Path) -> None:
        governor = SafetyGovernor(load_config_bundle(config_dir).safety)
        forbidden = [
            name
            for name in dir(governor)
            if not name.startswith("_")
            and any(
                name.startswith(prefix) for prefix in ("set_", "update_", "reload", "write")
            )
        ]
        assert not forbidden


class TestRARunningIsNotTreatedAsMotion:
    """github.com/alexcatesp/nocturne/issues/3.

    The reference rig reported ``RASTATUS.RARunning=Busy`` and
    ``DESTATUS.DERunning=Busy`` while parked with ``TRACK_OFF=On``
    (docs/FIELD-NOTES-M1.md section 4). The obvious reading — "this axis is
    moving" — is therefore wrong, or at best incomplete.

    Until the semantics are established, nothing may use these properties as an
    indicator of physical motion. A watchdog built on a property that is always
    Busy is a watchdog that never fires, and it would look implemented while
    doing nothing. This test is the enforcement: the property names may not
    appear in the package at all, so M2 cannot reach for them absent-mindedly.
    """

    #: Elements whose meaning is unknown. Not "use with care" — do not use.
    UNRESOLVED_MOTION_PROPERTIES = ("RARunning", "DERunning", "RAGoto", "DEGoto")

    def test_no_module_names_a_property_whose_meaning_is_unresolved(self) -> None:
        offenders: list[str] = []
        for path in python_sources():
            source = path.read_text(encoding="utf-8")
            for line_number, line in enumerate(source.splitlines(), start=1):
                named = [name for name in self.UNRESOLVED_MOTION_PROPERTIES if name in line]
                if named:
                    offenders.append(f"{module_name(path)}:{line_number} names {named}")
        assert not offenders, (
            f"{'; '.join(offenders)}\n"
            "These properties read Busy on a parked, non-tracking mount, so their "
            "meaning is not known. Resolve issue #3 before using one, and delete "
            "this test on purpose when you do — do not weaken it to make a build "
            "pass."
        )

    def test_the_fixture_still_shows_the_behaviour_that_prompted_this(self) -> None:
        """If the recorded evidence changes, the constraint should be revisited."""
        from tests.fixtures.eqmod import load_recorded_properties

        recorded = load_recorded_properties()
        assert recorded["RASTATUS"].values["RARunning"] == "Busy"
        assert recorded["DESTATUS"].values["DERunning"] == "Busy"
        assert recorded["TELESCOPE_TRACK_STATE"].values["TRACK_OFF"] is True


class TestFilterNamesAreNeverReadFromTheDriver:
    """docs/FIELD-NOTES-M1.md section 10.2.

    The EFW arrived holding ZWO factory names — Red, Green, Blue, H_Alpha, SII,
    OIII, LPR, Luminance — none of which is in the wheel. Slot 4 reads H_Alpha
    and holds B. Anything that believed the driver would file B frames as
    H-Alpha, and nothing in the image would look wrong: no artefact, no error,
    just a calibration library quietly indexed against the wrong channel.

    So the direction is one-way. ``equipment.yaml`` is the source of truth,
    Nocturne writes FILTER_NAME on connect, and no code path reads it back into
    a decision. Reading it to *log* what was overwritten is allowed, and is what
    the one permitted reader does.
    """

    #: The one place allowed to read FILTER_NAME, and only to report the
    #: overwrite. Anything else added here needs a reason in the diff.
    MODULES_THAT_MAY_READ_FILTER_NAME = frozenset({"nocturne.executor.instruments"})

    #: Naming either of these in code means addressing the driver's own names.
    FILTER_NAME_PROPERTIES = ("FILTER_NAME", "FILTER_SLOT_NAME")

    def names_the_drivers_filters(self, source: str) -> list[str]:
        return [
            literal
            for literal in code_string_literals(source)
            if any(name in literal for name in self.FILTER_NAME_PROPERTIES)
        ]

    def test_no_module_reads_the_drivers_filter_names(self) -> None:
        offenders: list[str] = []
        for path in python_sources():
            name = module_name(path)
            if name in self.MODULES_THAT_MAY_READ_FILTER_NAME:
                continue
            found = self.names_the_drivers_filters(path.read_text(encoding="utf-8"))
            offenders.extend(f"{name}: {literal!r}" for literal in found)
        assert not offenders, (
            f"{'; '.join(offenders)}\n"
            "FILTER_NAME belongs to the driver and the driver is wrong about it. "
            "Read filter_wheel.slots from configuration instead."
        )

    def test_the_permitted_reader_only_writes_and_logs(self) -> None:
        """The exemption is narrow: it may name the property, not act on it."""
        from nocturne.executor import instruments

        source = inspect.getsource(instruments)
        # The one read is the comparison that produces the warning.
        assert "disagree with equipment.yaml" in source
        assert "set_property(device, FILTER_NAME" in source

    def test_the_detector_catches_a_module_that_does_read_them(self) -> None:
        """Positive control — CLAUDE.md section 2.

        This test passes by finding nothing, and finding nothing is what a
        broken scan does too. So the same predicate is pointed at a module that
        reads the driver's names on purpose, and must catch it.
        """
        offender = (
            "async def choose(executor, device):\n"
            "    names = executor.get_property(device, 'FILTER_NAME')\n"
            "    return names['FILTER_SLOT_NAME_4']\n"
        )
        assert sorted(self.names_the_drivers_filters(offender)) == [
            "FILTER_NAME",
            "FILTER_SLOT_NAME_4",
        ]

    def test_the_detector_ignores_prose_that_explains_the_rule(self) -> None:
        """Otherwise the rule could not be documented where it applies."""
        innocent = '''"""FILTER_NAME is the driver's and the driver is wrong."""\n'''
        assert self.names_the_drivers_filters(innocent) == []

    def test_configuration_is_where_the_filter_names_live(self, config_dir: Path) -> None:
        """The thing the rule points at instead."""
        from nocturne.schemas import load_equipment_config

        wheel = load_equipment_config(config_dir / "equipment.yaml").filter_wheel
        assert [slot.name for slot in wheel.populated_slots.values()] == [
            "L",
            "R",
            "G",
            "B",
            "Dark",
        ]


class TestRuleEvaluation:
    """The loop M2's limits will run through, exercised now rather than then."""

    def test_a_rule_that_rejects_stops_the_command(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse_everything(command: Command, _config: object) -> Rejected:
            return Rejected(reason=f"no {command.describe()}", rule="test_rule")

        governor = SafetyGovernor(load_config_bundle(config_dir).safety)
        install_rules(monkeypatch, (refuse_everything,))
        decision = governor.validate(ConnectDevice(device="CCD Simulator"))

        assert isinstance(decision, Rejected)
        assert decision.rule == "test_rule"
        assert "connect CCD Simulator" in decision.reason

    def test_a_rule_that_permits_lets_the_command_through(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def permit(_command: Command, _config: object) -> None:
            return None

        governor = SafetyGovernor(load_config_bundle(config_dir).safety)
        install_rules(monkeypatch, (permit, permit))
        assert isinstance(governor.validate(ConnectDevice(device="CCD Simulator")), Ok)

    def test_the_first_rejecting_rule_wins(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def first(_command: Command, _config: object) -> Rejected:
            return Rejected(reason="first", rule="first")

        def second(_command: Command, _config: object) -> Rejected:
            return Rejected(reason="second", rule="second")

        governor = SafetyGovernor(load_config_bundle(config_dir).safety)
        install_rules(monkeypatch, (first, second))
        decision = governor.validate(ConnectDevice(device="X"))
        assert isinstance(decision, Rejected)
        assert decision.rule == "first"

    def test_m1_ships_no_rules_and_says_so(self) -> None:
        """The numeric limits of SPEC 9.1 and 9.2 land in M2. This is the record."""
        from nocturne.safety import COMMAND_RULES

        assert all(rules == () for rules in COMMAND_RULES.values()), (
            "a rule appeared in COMMAND_RULES; update this test and the M1/M2 note "
            "in nocturne/safety/governor.py"
        )


class TestDecisionContract:
    def test_the_base_decision_cannot_be_used_as_a_verdict(self) -> None:
        """Ok and Rejected must each answer for themselves."""
        from nocturne.safety.governor import Decision

        with pytest.raises(NotImplementedError):
            bool(Decision())

    def test_the_base_command_describes_itself_by_type(self) -> None:
        class Anonymous(Command):
            pass

        assert Anonymous().describe() == "Anonymous"


#: INDI vectors that command motion. None may be written by anything the bench
#: procedure runs.
MOVING_PROPERTIES = (
    "EQUATORIAL_EOD_COORD",
    "EQUATORIAL_COORD",
    "TELESCOPE_PARK",
    "TELESCOPE_MOTION_NS",
    "TELESCOPE_MOTION_WE",
    "TELESCOPE_TIMED_GUIDE",
    "ON_COORD_SET",
    "TELESCOPE_ABORT_MOTION",
)

#: Every way a caller can reach the instrument. The bench script drives the
#: Executor rather than the raw client, so scanning only ``write()`` would now
#: find nothing at all and pass while the script slewed the mount.
MUTATING_CALLS = frozenset(
    {"write", "set_property", "connect_device", "disconnect_device", "_perform"}
)


def properties_written_by(path: Path) -> set[str]:
    """String constants handed to any call that reaches the instrument."""
    return properties_written_by_source(path.read_text(encoding="utf-8"))


def properties_written_by_source(source: str) -> set[str]:
    """The detector itself, over source text, so it can be tested on a fixture."""
    written: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MUTATING_CALLS
        ):
            written |= {
                argument.value
                for argument in ast.walk(node)
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            }
    return written


class TestTheBenchTestMovesNothing:
    """docs/hardware-setup.md promises the operator that no motor turns.

    The script now brings the mount up through Executor and MountLink — the
    same path a session will use — rather than through a parallel one that
    could drift from it. That means the promise covers two files, so both are
    checked: what the script writes directly, and what the bring-up sequence
    it calls writes on its behalf.
    """

    BENCH_SCRIPT = PACKAGE_ROOT.parent / "scripts" / "bench-test-mount.py"
    MOUNT_MODULE = PACKAGE_ROOT / "nocturne" / "executor" / "mount.py"

    def script(self) -> str:
        return self.BENCH_SCRIPT.read_text(encoding="utf-8")

    def test_the_script_exists_where_the_procedure_says_it_does(self) -> None:
        assert self.BENCH_SCRIPT.is_file()

    def test_it_writes_no_property_that_commands_motion(self) -> None:
        offenders = sorted(properties_written_by(self.BENCH_SCRIPT) & set(MOVING_PROPERTIES))
        assert not offenders, (
            f"the bench script writes {offenders}, which moves the mount. "
            "docs/hardware-setup.md tells the operator nothing moves."
        )

    def test_the_bring_up_sequence_it_calls_moves_nothing_either(self) -> None:
        """MountLink writes on the script's behalf; the promise covers it too."""
        offenders = sorted(properties_written_by(self.MOUNT_MODULE) & set(MOVING_PROPERTIES))
        assert not offenders, (
            f"MountLink writes {offenders}, which moves the mount. The bench "
            "procedure calls bring_up(), so this breaks its promise as well."
        )

    def test_the_scan_actually_sees_the_calls_the_script_makes(self) -> None:
        """Guard: an empty scan would make the two tests above vacuous.

        If the script is rewritten onto some third API, this fails rather than
        quietly passing.
        """
        found = properties_written_by(self.BENCH_SCRIPT)
        assert found, "no instrument call was found in the bench script at all"
        assert "GEOGRAPHIC_COORD" in found, sorted(found)

    def test_it_never_turns_tracking_on(self) -> None:
        assert "TRACK_ON" not in self.script()
        assert "TRACK_ON" not in self.MOUNT_MODULE.read_text(encoding="utf-8")

    def test_the_procedure_promises_no_motion(self) -> None:
        procedure = (PACKAGE_ROOT.parent / "docs" / "hardware-setup.md").read_text(
            encoding="utf-8"
        )
        assert "Nothing in this procedure moves the mount" in procedure
        assert "No telescope" in procedure
