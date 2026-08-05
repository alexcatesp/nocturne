"""Safety governor — SPEC section 9, CLAUDE.md section 2.

M1 ships the governor's entry point and its enforcement invariant. The numeric
limits of SPEC sections 9.1 and 9.2 land in M2; what must already be true is
that there is exactly one door into the executor and it is this one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nocturne.safety import (
    Approval,
    ConnectDevice,
    Decision,
    DisconnectDevice,
    Ok,
    Rejected,
    SafetyGovernor,
    SafetyViolation,
    SetProperty,
)
from nocturne.schemas import AUTONOMY_LEVELS, SafetyConfig, load_config_bundle

UNATTENDED_LEVELS = ("supervised", "autonomous")
ATTENDED_LEVELS = ("observer", "advisory")


@pytest.fixture
def governor(config_dir: Path) -> SafetyGovernor:
    """A governor over the shipped, uncalibrated configuration."""
    return SafetyGovernor(load_config_bundle(config_dir).safety)


@pytest.fixture
def calibrated_governor(config_dir: Path) -> SafetyGovernor:
    """A governor over a configuration whose meridian limits have been measured."""
    import copy

    import yaml

    with (config_dir / "safety.yaml").open(encoding="utf-8") as handle:
        raw: dict[str, Any] = copy.deepcopy(yaml.safe_load(handle))
    raw["limits"]["meridian"].update(
        {
            "calibrated": True,
            "calibration_date": "2026-08-01",
            "hour_angle_east_limit_deg": -25.0,
            "hour_angle_west_limit_deg": 20.0,
        }
    )
    return SafetyGovernor(SafetyConfig.model_validate(raw))


@pytest.fixture
def command() -> SetProperty:
    return SetProperty(
        device="Telescope Simulator",
        property="TELESCOPE_SLEW_RATE",
        values={"SLEW_GUIDE": True},
    )


class TestValidateEntryPoint:
    """SPEC section 9: validate(command) -> Ok | Rejected(reason)."""

    def test_well_formed_command_is_accepted(
        self, governor: SafetyGovernor, command: SetProperty
    ) -> None:
        decision = governor.validate(command)
        assert isinstance(decision, Ok)
        assert decision.command is command

    def test_decision_is_ok_or_rejected_and_nothing_else(
        self, governor: SafetyGovernor, command: SetProperty
    ) -> None:
        assert Decision.__subclasses__() == [Ok, Rejected]
        assert isinstance(governor.validate(command), Decision)

    def test_rejection_carries_a_reason_and_the_rule_that_fired(self) -> None:
        rejected = Rejected(reason="below the altitude floor", rule="altitude_min_deg")
        assert rejected.reason
        assert rejected.rule
        assert not rejected

    def test_ok_is_truthy_and_rejected_is_falsy(
        self, governor: SafetyGovernor, command: SetProperty
    ) -> None:
        """So that ``if not governor.validate(cmd): ...`` cannot be got wrong."""
        assert governor.validate(command)
        assert not Rejected(reason="no", rule="r")

    def test_every_command_type_is_registered(self, governor: SafetyGovernor) -> None:
        """Fail-safe: a command type the governor does not know is rejected."""

        class UnregisteredCommand(ConnectDevice):
            pass

        decision = governor.validate(UnregisteredCommand(device="Telescope Simulator"))
        assert isinstance(decision, Rejected)
        assert "UnregisteredCommand" in decision.reason

    def test_connect_and_disconnect_are_validated(self, governor: SafetyGovernor) -> None:
        assert governor.validate(ConnectDevice(device="CCD Simulator"))
        assert governor.validate(DisconnectDevice(device="CCD Simulator"))

    def test_decisions_are_logged_with_structured_context(
        self,
        governor: SafetyGovernor,
        command: SetProperty,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SPEC section 1.3.5 — decisions are auditable after the fact."""
        with caplog.at_level(logging.INFO, logger="nocturne.safety"):
            governor.validate(command)
        record = next(r for r in caplog.records if r.name == "nocturne.safety")
        assert record.command_type == "SetProperty"  # type: ignore[attr-defined]
        assert record.decision == "ok"  # type: ignore[attr-defined]


class TestApprovalCannotBeForged:
    """CLAUDE.md invariant 1: there is no other path to the executor."""

    def test_governor_approve_returns_an_approval(
        self, governor: SafetyGovernor, command: SetProperty
    ) -> None:
        approval = governor.approve(command)
        assert isinstance(approval, Approval)
        assert approval.command is command

    def test_approval_cannot_be_constructed_directly(self, command: SetProperty) -> None:
        with pytest.raises(SafetyViolation, match="safety governor"):
            Approval(command=command, token=object())

    def test_approval_cannot_be_constructed_with_no_token(self, command: SetProperty) -> None:
        with pytest.raises(TypeError):
            Approval(command=command)  # type: ignore[call-arg]

    def test_approval_is_immutable(
        self, governor: SafetyGovernor, command: SetProperty
    ) -> None:
        approval = governor.approve(command)
        with pytest.raises((AttributeError, TypeError)):
            approval.command = command  # type: ignore[misc]

    def test_approve_raises_on_a_rejected_command(self, governor: SafetyGovernor) -> None:
        class UnregisteredCommand(ConnectDevice):
            pass

        with pytest.raises(SafetyViolation) as excinfo:
            governor.approve(UnregisteredCommand(device="CCD Simulator"))
        assert "UnregisteredCommand" in str(excinfo.value)

    def test_holding_an_approval_does_not_leak_the_token(
        self, governor: SafetyGovernor, command: SetProperty
    ) -> None:
        """An approval must not be a factory for further approvals."""
        approval = governor.approve(command)
        assert not hasattr(approval, "token")
        leaked = getattr(approval, "token", object())
        other = SetProperty(device="X", property="Y", values={"a": True})
        with pytest.raises(SafetyViolation, match="safety governor"):
            Approval(command=other, token=leaked)


class TestMeridianAutonomyGate:
    """SPEC section 9.1.2 and CLAUDE.md invariant 3. Hard failure, never a warning."""

    @pytest.mark.parametrize("level", UNATTENDED_LEVELS)
    def test_unattended_levels_are_refused_while_uncalibrated(
        self, governor: SafetyGovernor, level: str
    ) -> None:
        with pytest.raises(SafetyViolation) as excinfo:
            governor.require_autonomy_level(level)
        message = str(excinfo.value)
        assert "meridian" in message.lower()
        assert "docs/meridian-calibration.md" in message

    @pytest.mark.parametrize("level", ATTENDED_LEVELS)
    def test_attended_levels_are_permitted_while_uncalibrated(
        self, governor: SafetyGovernor, level: str
    ) -> None:
        governor.require_autonomy_level(level)

    @pytest.mark.parametrize("level", AUTONOMY_LEVELS)
    def test_every_level_is_permitted_once_calibrated(
        self, calibrated_governor: SafetyGovernor, level: str
    ) -> None:
        calibrated_governor.require_autonomy_level(level)

    def test_max_autonomy_level_is_advisory_while_uncalibrated(
        self, governor: SafetyGovernor
    ) -> None:
        assert governor.max_autonomy_level == "advisory"

    def test_max_autonomy_level_is_autonomous_once_calibrated(
        self, calibrated_governor: SafetyGovernor
    ) -> None:
        assert calibrated_governor.max_autonomy_level == "autonomous"

    def test_unknown_autonomy_level_is_refused(self, governor: SafetyGovernor) -> None:
        with pytest.raises(SafetyViolation, match="god_mode"):
            governor.require_autonomy_level("god_mode")

    @given(level=st.sampled_from(UNATTENDED_LEVELS))
    def test_no_uncalibrated_configuration_permits_unattended_operation(
        self, level: str
    ) -> None:
        """Property: uncalibrated implies refused, for every valid safety config."""
        import copy

        import yaml

        with (Path(__file__).resolve().parents[3] / "config" / "safety.yaml").open(
            encoding="utf-8"
        ) as handle:
            raw: dict[str, Any] = copy.deepcopy(yaml.safe_load(handle))
        governor = SafetyGovernor(SafetyConfig.model_validate(raw))
        with pytest.raises(SafetyViolation):
            governor.require_autonomy_level(level)


class TestGovernorDoesNotMutateConfiguration:
    """CLAUDE.md invariant 2: limits cannot be modified at runtime."""

    def test_governor_exposes_no_setter_for_limits(self, governor: SafetyGovernor) -> None:
        public = [name for name in dir(governor) if not name.startswith("_")]
        assert not [name for name in public if name.startswith("set_")]

    def test_governor_config_is_frozen(self, governor: SafetyGovernor) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            governor.config.limits.altitude_min_deg = 0.0  # type: ignore[misc]
