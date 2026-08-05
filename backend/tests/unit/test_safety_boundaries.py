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

    def test_no_module_opens_a_file_for_writing(self) -> None:
        offenders: list[str] = []
        writing_calls = {"write_text", "write_bytes", "unlink", "rename", "replace"}
        for path in python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if isinstance(function, ast.Attribute) and function.attr in writing_calls:
                    offenders.append(f"{module_name(path)}:{node.lineno} {function.attr}()")
                elif isinstance(function, ast.Attribute) and function.attr == "open":
                    mode = next(
                        (
                            keyword.value.value
                            for keyword in node.keywords
                            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant)
                        ),
                        None,
                    )
                    positional = next(
                        (
                            argument.value
                            for argument in node.args[:1]
                            if isinstance(argument, ast.Constant)
                        ),
                        None,
                    )
                    if any("w" in str(m) or "a" in str(m) for m in (mode, positional) if m):
                        offenders.append(f"{module_name(path)}:{node.lineno} open(...)")
        assert not offenders, (
            "nothing in the backend may write to disk in M1, and nothing ever "
            f"writes configuration: {'; '.join(offenders)}"
        )

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
