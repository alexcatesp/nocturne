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
    found: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in WRITING_METHODS:
            found.append((node.lineno, f"{node.func.attr}()"))
        elif node.func.attr == "open" and _opens_for_writing(node):
            found.append((node.lineno, "open(...)"))
    return found


def _opens_for_writing(node: ast.Call) -> bool:
    modes = [
        keyword.value.value
        for keyword in node.keywords
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant)
    ]
    modes += [
        argument.value for argument in node.args[:1] if isinstance(argument, ast.Constant)
    ]
    return any(character in str(mode) for mode in modes for character in "wax+")


def python_sources() -> list[Path]:
    """Every module in the nocturne package."""
    return sorted(PACKAGE_ROOT.glob("nocturne/**/*.py"))


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
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "nocturne.executor.indi"
                ):
                    offenders.append(f"{name} imports {node.module}")
                elif isinstance(node, ast.Import):
                    offenders.extend(
                        f"{name} imports {alias.name}"
                        for alias in node.names
                        if alias.name.startswith("nocturne.executor.indi")
                    )
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
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in MUTATING_TRANSPORT_METHODS
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr in {"client", "_client", "ekos", "_ekos"}
                ):
                    offenders.append(f"{name}:{node.lineno} calls .{node.func.attr}()")
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
                named = [
                    name for name in self.UNRESOLVED_MOTION_PROPERTIES if name in line
                ]
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


class TestTheBenchTestMovesNothing:
    """docs/hardware-setup.md promises the operator that no motor turns."""

    #: INDI vectors that command motion. None may appear in the bench script.
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

    def script(self) -> str:
        return (PACKAGE_ROOT.parent / "scripts" / "bench-test-mount.py").read_text(
            encoding="utf-8"
        )

    def test_the_script_exists_where_the_procedure_says_it_does(self) -> None:
        assert (PACKAGE_ROOT.parent / "scripts" / "bench-test-mount.py").is_file()

    def test_it_writes_no_property_that_commands_motion(self) -> None:
        """Every string handed to a write() call, checked against the motion list."""
        tree = ast.parse(self.script())
        written: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write"
            ):
                written |= {
                    argument.value
                    for argument in ast.walk(node)
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                }
        offenders = sorted(written & set(self.MOVING_PROPERTIES))
        assert not offenders, (
            f"the bench script writes {offenders}, which moves the mount. "
            "docs/hardware-setup.md tells the operator nothing moves."
        )

    def test_it_never_turns_tracking_on(self) -> None:
        assert "TRACK_ON" not in self.script()

    def test_the_procedure_promises_no_motion(self) -> None:
        procedure = (PACKAGE_ROOT.parent / "docs" / "hardware-setup.md").read_text(
            encoding="utf-8"
        )
        assert "Nothing in this procedure moves the mount" in procedure
        assert "No telescope" in procedure
