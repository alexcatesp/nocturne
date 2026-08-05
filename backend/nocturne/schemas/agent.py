"""Schema for ``config/agent.yaml`` — SPEC sections 5.3, 8.2, 8.3 and 8.4.

SPEC section 5.3 defines the file's contents as: model selection, system prompt
path, autonomy level, event subscription list, poll interval, and the tool
allow-list per autonomy level. Nothing else belongs here.

The model is frozen. CLAUDE.md invariant 2: the agent cannot raise its own
autonomy level, and there is therefore no setter for it.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self, get_args

from pydantic import Field, field_validator, model_validator

from .common import StrictModel

#: SPEC section 8.4.
AutonomyLevel = Literal["observer", "advisory", "supervised", "autonomous"]

#: Declaration order matters: it is the order of increasing privilege.
AUTONOMY_LEVELS: tuple[AutonomyLevel, ...] = get_args(AutonomyLevel)

#: SPEC section 8.2 — read-only tools, always available at every level.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "get_session_state",
        "get_block_summary",
        "get_frame_detail",
        "get_target_visibility",
        "get_integration_history",
        "get_weather",
        "get_catalog_search",
        "get_sky_conditions",
    }
)

#: SPEC section 8.2 — action tools, gated by autonomy level and always
#: validated by the safety governor (SPEC section 9).
ACTION_TOOLS: frozenset[str] = frozenset(
    {
        "propose_plan",
        "set_target",
        "request_refocus",
        "request_meridian_decision",
        "skip_target",
        "end_session",
        "notify_operator",
    }
)

#: SPEC section 8.3 — the event codes that wake the agent.
EVENT_CODES: frozenset[str] = frozenset(
    {
        "BLOCK_COMPLETE",
        "TARGET_LIMIT_APPROACHING",
        "FOCUS_DRIFT",
        "TRANSPARENCY_DROP",
        "GUIDING_DEGRADED",
        "REJECT_RATE_HIGH",
        "SOLVE_FAILED",
        "TARGET_COMPLETE",
        "SESSION_START",
        "SESSION_END",
        "SAFETY_ABORT",
    }
)

#: SPEC section 8.3: SAFETY_ABORT is a post-hoc notification. The safety layer
#: never waits for the agent, but the agent is never left unaware either.
MANDATORY_EVENT_CODES: frozenset[str] = frozenset({"SAFETY_ABORT"})


class AgentConfig(StrictModel):
    """Root model for ``config/agent.yaml``."""

    model: str = Field(min_length=1)
    system_prompt_path: str = Field(min_length=1)
    autonomy_level: AutonomyLevel
    poll_interval_minutes: Annotated[int, Field(gt=0)]
    event_subscriptions: tuple[str, ...]
    tool_allow_list: dict[str, tuple[str, ...]]

    @field_validator("system_prompt_path")
    @classmethod
    def _prompt_path_stays_inside_the_repository(cls, value: str) -> str:
        from pathlib import PurePosixPath

        path = PurePosixPath(value)
        if path.is_absolute():
            raise ValueError("must be relative to the repository root, not an absolute path")
        if ".." in path.parts:
            raise ValueError("must not traverse outside the repository root with '..'")
        return value

    @model_validator(mode="after")
    def _event_subscriptions_are_known_and_unique(self) -> Self:
        codes = list(self.event_subscriptions)
        duplicates = sorted({code for code in codes if codes.count(code) > 1})
        if duplicates:
            raise ValueError(
                f"duplicate event code(s) in event_subscriptions: {', '.join(duplicates)}"
            )
        unknown = sorted(set(codes) - EVENT_CODES)
        if unknown:
            raise ValueError(
                f"unknown event code(s) in event_subscriptions: {', '.join(unknown)}. "
                "The codes are defined in SPEC section 8.3."
            )
        missing = sorted(MANDATORY_EVENT_CODES - set(codes))
        if missing:
            raise ValueError(
                f"event_subscriptions must include {', '.join(missing)}: the agent is "
                "always told about a safety abort (SPEC section 8.3)."
            )
        return self

    @model_validator(mode="after")
    def _allow_list_covers_every_level(self) -> Self:
        levels = set(self.tool_allow_list)
        missing = [level for level in AUTONOMY_LEVELS if level not in levels]
        if missing:
            raise ValueError(
                f"tool_allow_list is missing autonomy level(s): {', '.join(missing)}"
            )
        unknown = sorted(levels - set(AUTONOMY_LEVELS))
        if unknown:
            raise ValueError(
                f"tool_allow_list has unknown autonomy level(s): {', '.join(unknown)}"
            )
        return self

    @model_validator(mode="after")
    def _allow_list_names_action_tools_only(self) -> Self:
        for level, tools in self.tool_allow_list.items():
            listed = list(tools)
            duplicates = sorted({tool for tool in listed if listed.count(tool) > 1})
            if duplicates:
                raise ValueError(
                    f"tool_allow_list.{level} has duplicate tool(s): {', '.join(duplicates)}"
                )
            read_only = sorted(set(listed) & READ_ONLY_TOOLS)
            if read_only:
                raise ValueError(
                    f"tool_allow_list.{level} lists read-only tool(s) "
                    f"{', '.join(read_only)}; read-only tools are available at every "
                    "level and must not be listed (SPEC section 8.2)."
                )
            unknown = sorted(set(listed) - ACTION_TOOLS)
            if unknown:
                raise ValueError(
                    f"tool_allow_list.{level} names unknown tool(s): "
                    f"{', '.join(unknown)}. The tools are defined in SPEC section 8.2."
                )
        return self

    @model_validator(mode="after")
    def _observer_cannot_act(self) -> Self:
        """SPEC section 8.4 — observer comments, it does not act."""
        granted = self.tool_allow_list.get("observer", ())
        if granted:
            raise ValueError(
                "tool_allow_list.observer must be empty: the observer level is "
                f"read-only, but it grants {', '.join(sorted(granted))}"
            )
        return self

    def tools_for(self, level: str) -> frozenset[str]:
        """Every tool available at ``level``: read-only tools plus its action tools."""
        if level not in AUTONOMY_LEVELS:
            raise KeyError(f"unknown autonomy level {level!r}")
        return READ_ONLY_TOOLS | frozenset(self.tool_allow_list[level])
