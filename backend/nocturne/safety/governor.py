"""The safety governor — SPEC section 9.

Layer 2. Not bypassable. Every command passes through
``validate(command) -> Ok | Rejected(reason)``.

**M1 scope.** This module ships the entry point and the enforcement invariant.
The numeric limits of SPEC sections 9.1 and 9.2 — altitude floor and ceiling,
sun avoidance, meridian hour angle, TEC ramp, disk floor — land in M2 and are
registered as rules against the command types they constrain. What is already
enforced here:

* An :class:`Approval` is the only thing the executor accepts, and only this
  module can mint one. There is no second door (CLAUDE.md invariant 1).
* A command type that is not in the registry is **rejected**, not passed
  through. Adding a command in M2 without registering it fails closed.
* ``supervised`` and ``autonomous`` are refused while the meridian limits are
  uncalibrated — a hard failure with an explicit message, never a warning and
  never a default-to-permissive (SPEC section 9.1.2, CLAUDE.md invariant 3).

The governor never mutates its configuration and exposes no way to do so.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass
from typing import Final, final

from nocturne.safety.commands import Command, ConnectDevice, DisconnectDevice, SetProperty
from nocturne.schemas import AUTONOMY_LEVELS, AutonomyLevel, SafetyConfig

logger = logging.getLogger("nocturne.safety")

#: Autonomy levels that run without a human watching, and are therefore gated on
#: the meridian calibration (SPEC section 8.4 and section 9.1.2).
UNATTENDED_LEVELS: Final[frozenset[str]] = frozenset({"supervised", "autonomous"})

#: The highest level permitted while the meridian limits are uncalibrated.
MAX_UNCALIBRATED_LEVEL: Final[AutonomyLevel] = "advisory"


class SafetyViolation(Exception):
    """A safety rule was broken, or an unsafe operation was attempted.

    Raised rather than returned when the caller has no meaningful way to
    continue: forging an approval, or requesting an autonomy level the rig is
    not calibrated for. Never caught to proceed anyway.
    """


class Decision:
    """Outcome of :meth:`SafetyGovernor.validate`. Either :class:`Ok` or :class:`Rejected`."""

    __slots__ = ()

    def __bool__(self) -> bool:
        raise NotImplementedError


@final
@dataclass(frozen=True)
class Ok(Decision):
    """The command is permitted."""

    command: Command

    def __bool__(self) -> bool:
        return True


@final
@dataclass(frozen=True)
class Rejected(Decision):
    """The command is refused, with the reason and the rule that refused it."""

    reason: str
    rule: str

    def __bool__(self) -> bool:
        return False


#: Module-private sentinel. Possession of this object is what distinguishes an
#: approval minted by the governor from one a caller tried to build itself.
_APPROVAL_TOKEN: Final = object()


@final
@dataclass(frozen=True)
class Approval:
    """Proof that a command passed :meth:`SafetyGovernor.validate`.

    The executor's mutating API accepts nothing else. Only
    :meth:`SafetyGovernor.approve` can construct one; attempting to build it
    directly raises :class:`SafetyViolation`.

    ``token`` is an :class:`~dataclasses.InitVar`: it is checked at construction
    and never stored, so holding an approval does not hand the caller the means
    to mint another one.
    """

    command: Command
    token: InitVar[object]

    def __post_init__(self, token: object) -> None:
        if token is not _APPROVAL_TOKEN:
            raise SafetyViolation(
                "An Approval can only be issued by the safety governor. Call "
                "SafetyGovernor.approve(command); do not construct Approval directly "
                "(SPEC section 9, CLAUDE.md invariant 1)."
            )


#: A rule takes the command and the safety configuration and returns a rejection,
#: or None if the command is permitted. M2 populates these lists.
Rule = Callable[[Command, SafetyConfig], Rejected | None]

#: Every command type the governor knows about, with the rules that constrain it.
#: A command type absent from this mapping is rejected — adding a command in a
#: later milestone without deciding which limits apply to it fails closed.
COMMAND_RULES: Final[Mapping[type[Command], tuple[Rule, ...]]] = {
    ConnectDevice: (),
    DisconnectDevice: (),
    SetProperty: (),
}


@final
class SafetyGovernor:
    """Validates every command before it reaches the executor."""

    def __init__(self, config: SafetyConfig) -> None:
        self._config = config

    @property
    def config(self) -> SafetyConfig:
        """The safety configuration. Frozen; there is no setter, by design."""
        return self._config

    @property
    def max_autonomy_level(self) -> AutonomyLevel:
        """Highest autonomy level this configuration permits (SPEC section 9.1.2)."""
        if self._config.limits.meridian.calibrated:
            return AUTONOMY_LEVELS[-1]
        return MAX_UNCALIBRATED_LEVEL

    def validate(self, command: Command) -> Decision:
        """Validate ``command`` — SPEC section 9.

        Returns :class:`Ok` or :class:`Rejected`. Never raises for an unsafe
        command; rejection is a value so that callers must handle it.
        """
        rules = COMMAND_RULES.get(type(command))
        if rules is None:
            decision: Decision = Rejected(
                reason=(
                    f"{type(command).__name__} is not registered with the safety "
                    "governor, so no limits are known to apply to it. Register it in "
                    "COMMAND_RULES with the rules that constrain it."
                ),
                rule="command_registry",
            )
        else:
            decision = Ok(command=command)
            for rule in rules:
                rejection = rule(command, self._config)
                if rejection is not None:
                    decision = rejection
                    break

        self._log(command, decision)
        return decision

    def approve(self, command: Command) -> Approval:
        """Validate ``command`` and mint an :class:`Approval` for the executor.

        Raises:
            SafetyViolation: if the command is rejected.
        """
        decision = self.validate(command)
        if isinstance(decision, Rejected):
            raise SafetyViolation(
                f"Refused by the safety governor [{decision.rule}]: {decision.reason} "
                f"(command: {command.describe()})"
            )
        return Approval(command=command, token=_APPROVAL_TOKEN)

    def require_autonomy_level(self, level: str) -> None:
        """Assert that ``level`` is permitted, or raise.

        SPEC section 9.1.2: the system must refuse ``supervised`` and
        ``autonomous`` while ``meridian.calibrated`` is false. Hard failure with
        an explicit message — not a warning, and not a silent demotion.

        Raises:
            SafetyViolation: if the level is unknown or not permitted.
        """
        if level not in AUTONOMY_LEVELS:
            raise SafetyViolation(
                f"Unknown autonomy level {level!r}. Valid levels: "
                f"{', '.join(AUTONOMY_LEVELS)} (SPEC section 8.4)."
            )
        if level in UNATTENDED_LEVELS and not self._config.limits.meridian.calibrated:
            raise SafetyViolation(
                f"Autonomy level {level!r} is refused: the meridian limits for this "
                "mount and tripod have not been calibrated, so the governor cannot "
                "know at which hour angle the OTA strikes the tripod legs. Complete "
                "the procedure in docs/meridian-calibration.md and record the measured "
                "limits in config/safety.yaml. The highest level available until then "
                f"is {MAX_UNCALIBRATED_LEVEL!r} (SPEC section 9.1)."
            )
        logger.info(
            "autonomy level %s permitted",
            level,
            extra={
                "autonomy_level": level,
                "meridian_calibrated": self._config.limits.meridian.calibrated,
            },
        )

    def _log(self, command: Command, decision: Decision) -> None:
        if isinstance(decision, Rejected):
            logger.warning(
                "safety governor REJECTED %s: %s",
                command.describe(),
                decision.reason,
                extra={
                    "command_type": type(command).__name__,
                    "command": command.describe(),
                    "decision": "rejected",
                    "rule": decision.rule,
                    "reason": decision.reason,
                },
            )
        else:
            logger.info(
                "safety governor approved %s",
                command.describe(),
                extra={
                    "command_type": type(command).__name__,
                    "command": command.describe(),
                    "decision": "ok",
                },
            )
