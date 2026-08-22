"""Safety governor — SPEC section 9.

The most important package in the repository (CLAUDE.md section 2). Nothing
reaches the executor without an :class:`Approval` minted here.
"""

from __future__ import annotations

from .commands import Command, ConnectDevice, DisconnectDevice, PropertyValue, SetProperty
from .decisions import Decision, Ok, Rejected
from .governor import (
    COMMAND_RULES,
    MAX_UNCALIBRATED_LEVEL,
    UNATTENDED_LEVELS,
    Approval,
    Clock,
    SafetyGovernor,
    SafetyViolation,
)
from .properties import (
    COORDINATE_PROPERTIES,
    DRIVER_LIMIT_PROPERTIES,
    UNVALIDATABLE_MOTION_PROPERTIES,
    PointingIntent,
    pointing_intent,
)
from .rules import POINTING_RULES, Rule, RuleContext

__all__ = [
    "COMMAND_RULES",
    "COORDINATE_PROPERTIES",
    "DRIVER_LIMIT_PROPERTIES",
    "MAX_UNCALIBRATED_LEVEL",
    "POINTING_RULES",
    "UNATTENDED_LEVELS",
    "UNVALIDATABLE_MOTION_PROPERTIES",
    "Approval",
    "Clock",
    "Command",
    "ConnectDevice",
    "Decision",
    "DisconnectDevice",
    "Ok",
    "PointingIntent",
    "PropertyValue",
    "Rejected",
    "Rule",
    "RuleContext",
    "SafetyGovernor",
    "SafetyViolation",
    "SetProperty",
    "pointing_intent",
]
