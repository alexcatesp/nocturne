"""Agent configuration schema — SPEC sections 5.3, 8.2, 8.3 and 8.4."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from nocturne.schemas import ACTION_TOOLS, READ_ONLY_TOOLS, AgentConfig, load_agent_config


@pytest.fixture
def raw(config_dir: Path) -> dict[str, Any]:
    """The shipped agent.yaml parsed to a plain dict, safe to mutate."""
    import yaml

    with (config_dir / "agent.yaml").open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return copy.deepcopy(loaded)


class TestShippedFile:
    def test_shipped_agent_config_validates(self, config_dir: Path) -> None:
        config = load_agent_config(config_dir / "agent.yaml")
        assert isinstance(config, AgentConfig)

    def test_default_autonomy_level_is_advisory(self, config_dir: Path) -> None:
        """SPEC section 8.4 — advisory is the default, in bold."""
        assert load_agent_config(config_dir / "agent.yaml").autonomy_level == "advisory"

    def test_poll_interval_default(self, config_dir: Path) -> None:
        """SPEC section 8.3 — scheduled poll, default every 15 min."""
        assert load_agent_config(config_dir / "agent.yaml").poll_interval_minutes == 15

    def test_all_spec_event_codes_are_subscribed(self, config_dir: Path) -> None:
        config = load_agent_config(config_dir / "agent.yaml")
        assert set(config.event_subscriptions) == {
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

    def test_observer_level_has_no_action_tools(self, config_dir: Path) -> None:
        """SPEC section 8.4 — observer is read-only: comments, does not act."""
        config = load_agent_config(config_dir / "agent.yaml")
        assert config.tool_allow_list["observer"] == ()


class TestToolAllowList:
    def test_read_only_tools_are_always_available(self, config_dir: Path) -> None:
        """SPEC section 8.2 — read-only tools are available at every level."""
        config = load_agent_config(config_dir / "agent.yaml")
        for level in ("observer", "advisory", "supervised", "autonomous"):
            assert config.tools_for(level) >= READ_ONLY_TOOLS

    def test_tools_for_includes_the_level_action_tools(self, config_dir: Path) -> None:
        config = load_agent_config(config_dir / "agent.yaml")
        assert config.tools_for("autonomous") >= ACTION_TOOLS
        assert config.tools_for("observer") == READ_ONLY_TOOLS

    def test_every_autonomy_level_must_be_listed(self, raw: dict[str, Any]) -> None:
        del raw["tool_allow_list"]["supervised"]
        with pytest.raises(ValidationError, match="supervised"):
            AgentConfig.model_validate(raw)

    def test_unknown_tool_name_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["tool_allow_list"]["advisory"].append("slew_to")
        with pytest.raises(ValidationError, match="slew_to"):
            AgentConfig.model_validate(raw)

    def test_read_only_tool_may_not_be_listed_as_an_action_tool(
        self, raw: dict[str, Any]
    ) -> None:
        raw["tool_allow_list"]["advisory"].append("get_session_state")
        with pytest.raises(ValidationError, match="get_session_state"):
            AgentConfig.model_validate(raw)

    def test_duplicate_tool_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["tool_allow_list"]["advisory"].append("set_target")
        with pytest.raises(ValidationError, match="duplicate"):
            AgentConfig.model_validate(raw)

    def test_observer_may_not_be_granted_action_tools(self, raw: dict[str, Any]) -> None:
        """SPEC section 8.4 — observer does not act. Config cannot make it act."""
        raw["tool_allow_list"]["observer"].append("end_session")
        with pytest.raises(ValidationError, match="observer"):
            AgentConfig.model_validate(raw)


class TestStrictness:
    def test_unknown_key_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["temperature"] = 1.0
        with pytest.raises(ValidationError, match="temperature"):
            AgentConfig.model_validate(raw)

    def test_agent_config_is_immutable(self, config_dir: Path) -> None:
        """CLAUDE.md invariant 2: the agent cannot raise its own autonomy level."""
        config = load_agent_config(config_dir / "agent.yaml")
        with pytest.raises(ValidationError):
            config.autonomy_level = "autonomous"  # type: ignore[misc]

    def test_unknown_autonomy_level_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["autonomy_level"] = "god_mode"
        with pytest.raises(ValidationError, match="autonomy_level"):
            AgentConfig.model_validate(raw)

    def test_unknown_event_code_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["event_subscriptions"].append("WEATHER_NICE")
        with pytest.raises(ValidationError, match="WEATHER_NICE"):
            AgentConfig.model_validate(raw)

    def test_duplicate_event_code_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["event_subscriptions"].append("BLOCK_COMPLETE")
        with pytest.raises(ValidationError, match="duplicate"):
            AgentConfig.model_validate(raw)

    def test_safety_abort_subscription_is_mandatory(self, raw: dict[str, Any]) -> None:
        """SPEC section 8.3 — SAFETY_ABORT is informational but never silent."""
        raw["event_subscriptions"].remove("SAFETY_ABORT")
        with pytest.raises(ValidationError, match="SAFETY_ABORT"):
            AgentConfig.model_validate(raw)

    @pytest.mark.parametrize("value", [0, -5])
    def test_non_positive_poll_interval_is_rejected(
        self, raw: dict[str, Any], value: int
    ) -> None:
        raw["poll_interval_minutes"] = value
        with pytest.raises(ValidationError, match="poll_interval_minutes"):
            AgentConfig.model_validate(raw)

    def test_empty_model_name_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["model"] = ""
        with pytest.raises(ValidationError, match="model"):
            AgentConfig.model_validate(raw)

    def test_absolute_system_prompt_path_is_rejected(self, raw: dict[str, Any]) -> None:
        """Prompt paths are resolved against the repository root, never absolute."""
        raw["system_prompt_path"] = "/etc/passwd"
        with pytest.raises(ValidationError, match="system_prompt_path"):
            AgentConfig.model_validate(raw)

    def test_escaping_system_prompt_path_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["system_prompt_path"] = "../../etc/passwd"
        with pytest.raises(ValidationError, match="system_prompt_path"):
            AgentConfig.model_validate(raw)
