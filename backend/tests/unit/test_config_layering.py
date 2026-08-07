"""Shipped config plus an untracked local override — SPEC section 5, ADR 0013.

The defect this fixes was reported by the operator and could not have been found
here: `config/equipment.yaml` is version-controlled *and* is the file that must
carry the site's real coordinates, so every `git pull` collided with the edit
that made the rig work.

What the tests below have to establish is not "the merge works". It is that the
three ways a layered configuration lies quietly are all impossible:

* a value that was overridden but reads as shipped,
* an override that silently did nothing,
* an error in the operator's own file reported against the shipped one, sending
  them to edit the file that is not at fault.

Every absence-shaped assertion carries a positive control (CLAUDE.md section 2).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nocturne.schemas import ConfigError, load_config_bundle
from nocturne.schemas.equipment import EquipmentConfig
from nocturne.schemas.layering import (
    LOCAL_SUFFIX,
    is_local_path,
    local_path_for,
    merge_documents,
    source_for_error,
)
from nocturne.schemas.loader import load_layered

SHIPPED = Path("config/equipment.yaml")
LOCAL = Path("config/equipment.local.yaml")

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_CONFIG_DIR = REPO_ROOT / "config"


# --------------------------------------------------------------------------
# The merge itself: no I/O, no schema
# --------------------------------------------------------------------------


class TestMappingsMergeAndEverythingElseReplaces:
    def test_a_nested_scalar_replaces_only_itself(self) -> None:
        merged = merge_documents(
            {"site": {"latitude": 45.0, "longitude": 0.0, "name": "PLACEHOLDER"}},
            SHIPPED,
            {"site": {"latitude": 41.3874}},
            LOCAL,
        )
        assert merged.document == {
            "site": {"latitude": 41.3874, "longitude": 0.0, "name": "PLACEHOLDER"}
        }

    def test_a_list_replaces_wholesale(self) -> None:
        """A merged list would have to guess whether the operator meant to add a
        filter or renumber the wheel, and guessing about which filter is in slot
        4 is how B frames get filed as H-Alpha (ADR 0012)."""
        merged = merge_documents(
            {"wheel": {"slots": [{"position": 1}, {"position": 2}, {"position": 3}]}},
            SHIPPED,
            {"wheel": {"slots": [{"position": 1}]}},
            LOCAL,
        )
        assert merged.document == {"wheel": {"slots": [{"position": 1}]}}

    def test_a_mapping_replaced_by_a_scalar_replaces_wholesale(self) -> None:
        merged = merge_documents(
            {"mount": {"port": "/dev/ttyACM0", "baud": 115200}}, SHIPPED, {"mount": None}, LOCAL
        )
        assert merged.document == {"mount": None}

    def test_a_key_absent_from_the_shipped_file_is_added(self) -> None:
        merged = merge_documents({"site": {"name": "x"}}, SHIPPED, {"extra": 1}, LOCAL)
        assert merged.document["extra"] == 1

    def test_the_shipped_document_is_not_mutated(self) -> None:
        """The merge builds a new document. A loader that edited the shipped
        mapping in place would corrupt anything else reading it."""
        base: dict[str, object] = {"site": {"latitude": 45.0}}
        merge_documents(base, SHIPPED, {"site": {"latitude": 1.0}}, LOCAL)
        assert base == {"site": {"latitude": 45.0}}

    def test_no_override_file_leaves_the_document_alone(self) -> None:
        merged = merge_documents({"site": {"latitude": 45.0}}, SHIPPED, None, None)
        assert merged.document == {"site": {"latitude": 45.0}}
        assert merged.overrides == ()


class TestEveryValueKnowsWhereItCameFrom:
    def merged(self) -> object:
        return merge_documents(
            {"site": {"latitude": 45.0, "longitude": 0.0}, "mount": {"baud": 9600}},
            SHIPPED,
            {"site": {"latitude": 41.3874}},
            LOCAL,
        )

    def test_an_overridden_value_is_attributed_to_the_local_file(self) -> None:
        assert self.merged().source_of("site.latitude") == LOCAL  # type: ignore[attr-defined]

    def test_an_untouched_value_is_attributed_to_the_shipped_file(self) -> None:
        merged = self.merged()
        assert merged.source_of("site.longitude") == SHIPPED  # type: ignore[attr-defined]
        assert merged.source_of("mount.baud") == SHIPPED  # type: ignore[attr-defined]

    def test_every_leaf_is_attributed_to_something(self) -> None:
        """The positive control for the two above: a provenance map that came
        back empty would make both of them fail, but one that came back
        *partial* would not — so the whole set is checked."""
        sources = self.merged().sources  # type: ignore[attr-defined]
        assert set(sources) == {"site.latitude", "site.longitude", "mount.baud"}

    def test_a_replaced_mapping_leaves_no_stale_provenance(self) -> None:
        """Its old leaves are gone, so their entries must go with them —
        otherwise the report claims the shipped file still supplies a value that
        no longer exists."""
        merged = merge_documents(
            {"guiding": {"mode": "guidescope", "camera": {"gain": 50}}},
            SHIPPED,
            {"guiding": "off"},
            LOCAL,
        )
        assert set(merged.sources) == {"guiding"}
        assert merged.sources["guiding"] == LOCAL


class TestAnOverrideIsAlwaysVisible:
    def test_it_is_listed_with_what_it_displaced(self) -> None:
        merged = merge_documents(
            {"site": {"latitude": 45.0}}, SHIPPED, {"site": {"latitude": 41.3874}}, LOCAL
        )
        (override,) = merged.overrides
        assert override.key == "site.latitude"
        assert override.value == 41.3874
        assert override.previous == 45.0
        assert override.was_present

    def test_a_value_equal_to_the_shipped_one_is_still_listed(self) -> None:
        """It was written there deliberately. A report that hid it would make
        the file look inert, which is exactly the question the operator is
        asking when they read the report."""
        merged = merge_documents(
            {"site": {"latitude": 45.0}}, SHIPPED, {"site": {"latitude": 45.0}}, LOCAL
        )
        assert len(merged.overrides) == 1

    def test_a_key_the_shipped_file_lacks_is_marked_as_such(self) -> None:
        """Different from present-and-null, and the report says which."""
        merged = merge_documents({}, SHIPPED, {"invented": 1}, LOCAL)
        (override,) = merged.overrides
        assert not override.was_present
        assert override.previous is None

    def test_a_shipped_null_is_not_mistaken_for_an_absent_key(self) -> None:
        """The positive control for the distinction above."""
        merged = merge_documents({"port": None}, SHIPPED, {"port": "/dev/x"}, LOCAL)
        (override,) = merged.overrides
        assert override.was_present
        assert override.previous is None

    def test_untouched_values_are_not_listed_as_overrides(self) -> None:
        merged = merge_documents({"a": 1, "b": 2}, SHIPPED, {"a": 1}, LOCAL)
        assert [override.key for override in merged.overrides] == ["a"]


class TestTheOverridePathIsDerived:
    def test_it_sits_beside_the_shipped_file(self) -> None:
        assert local_path_for(Path("config/safety.yaml")) == Path("config/safety.local.yaml")

    def test_it_is_derived_rather_than_listed(self) -> None:
        """A fourth configuration file gets an override for free, and cannot be
        forgotten — which listing three names by hand would allow."""
        assert local_path_for(Path("config/anything.yaml")).name == f"anything{LOCAL_SUFFIX}"

    def test_a_local_file_has_no_local_file(self) -> None:
        with pytest.raises(ValueError, match="already a local override"):
            local_path_for(Path("config/safety.local.yaml"))

    def test_a_non_yaml_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"not a \.yaml file"):
            local_path_for(Path("config/safety.json"))

    def test_local_paths_are_recognised(self) -> None:
        assert is_local_path(Path("config/equipment.local.yaml"))
        assert not is_local_path(Path("config/equipment.yaml"))


class TestBlameForAValidationError:
    def test_it_finds_the_exact_key(self) -> None:
        sources = {"site.latitude": LOCAL, "site.longitude": SHIPPED}
        assert source_for_error(sources, ("site", "latitude")) == LOCAL

    def test_it_walks_up_to_a_parent(self) -> None:
        """Pydantic reports some errors against a mapping rather than a leaf."""
        sources = {"site.latitude": LOCAL}
        assert source_for_error(sources, ("site", "latitude", "deeper")) == LOCAL

    def test_it_gives_up_rather_than_guessing(self) -> None:
        """The positive control: an unknown location must return nothing, not
        the first file that happens to be in the map."""
        assert source_for_error({"site.latitude": LOCAL}, ("elsewhere",)) is None


# --------------------------------------------------------------------------
# Through the real loader, against the real shipped configuration
# --------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A copy of the shipped configuration, with no overrides."""
    directory = tmp_path / "config"
    directory.mkdir()
    for name in ("equipment.yaml", "safety.yaml", "agent.yaml"):
        (directory / name).write_text(
            (REAL_CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    return directory


def write_local(directory: Path, name: str, document: dict[str, object]) -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


class TestTheShippedConfigurationLoadsWithoutAnyOverride:
    """The state of a fresh clone, and of CI."""

    def test_it_is_valid(self, config_dir: Path) -> None:
        config = load_config_bundle(config_dir)
        assert config.equipment.site.is_placeholder

    def test_nothing_reports_a_local_file(self, config_dir: Path) -> None:
        config = load_config_bundle(config_dir)
        for sources in config.sources.all_sources():
            assert sources.local is None
            assert sources.overrides == ()

    def test_the_scan_looked_at_three_files(self, config_dir: Path) -> None:
        """Assert it saw something: zero files also have no local overrides."""
        config = load_config_bundle(config_dir)
        assert len(config.sources.all_sources()) == 3


class TestTheOverrideReachesTheModel:
    def test_the_site_comes_from_the_local_file(self, config_dir: Path) -> None:
        write_local(
            config_dir,
            "equipment.local.yaml",
            {"site": {"name": "Terrace", "latitude": 41.3874, "longitude": 2.1686}},
        )
        config = load_config_bundle(config_dir)

        assert config.equipment.site.name == "Terrace"
        assert config.equipment.site.latitude == pytest.approx(41.3874)
        assert not config.equipment.site.is_placeholder

    def test_values_it_does_not_mention_are_untouched(self, config_dir: Path) -> None:
        shipped = load_config_bundle(config_dir).equipment
        write_local(config_dir, "equipment.local.yaml", {"site": {"latitude": 41.3874}})
        layered = load_config_bundle(config_dir).equipment

        assert layered.imaging_camera.name == shipped.imaging_camera.name
        assert layered.site.timezone == shipped.site.timezone

    def test_the_placeholder_warning_stops_once_the_site_is_real(
        self, config_dir: Path
    ) -> None:
        """The positive control for the override taking effect at all: the
        shipped configuration triggers this and the layered one must not."""
        assert load_config_bundle(config_dir).equipment.site.is_placeholder
        write_local(
            config_dir,
            "equipment.local.yaml",
            {"site": {"latitude": 41.3874, "longitude": 2.1686}},
        )
        assert not load_config_bundle(config_dir).equipment.site.is_placeholder

    def test_every_file_can_be_layered(self, config_dir: Path) -> None:
        """Not equipment alone: anything an operator edits per site."""
        write_local(config_dir, "equipment.local.yaml", {"site": {"latitude": 41.0}})
        write_local(config_dir, "agent.local.yaml", {"poll_interval_minutes": 9})
        config = load_config_bundle(config_dir)

        assert config.agent.poll_interval_minutes == 9
        assert config.sources.equipment.is_layered
        assert config.sources.agent.is_layered
        assert not config.sources.safety.is_layered


class TestSafetyIsLayeredToo:
    """The meridian limits are a property of one tripod, tube and camera.

    They are exactly the "must edit per site" case, so they get the same
    treatment — and the same scrutiny, because a value here decides whether the
    tube swings into a leg. What makes that acceptable is that the local file is
    written by a human by hand, and that nothing in Nocturne can write it: see
    ``test_safety_boundaries.py``, which forbids it by the same rule as the
    shipped file.
    """

    def test_the_shipped_configuration_is_uncalibrated(self, config_dir: Path) -> None:
        assert not load_config_bundle(config_dir).safety.limits.meridian.calibrated

    def test_calibration_can_only_be_declared_by_editing_a_file(self, config_dir: Path) -> None:
        write_local(
            config_dir,
            "safety.local.yaml",
            {
                "limits": {
                    "meridian": {
                        "calibrated": True,
                        "calibration_date": "2026-08-08",
                        "hour_angle_west_limit_deg": 8.0,
                        "hour_angle_east_limit_deg": -8.0,
                    }
                }
            },
        )
        config = load_config_bundle(config_dir)

        assert config.safety.limits.meridian.calibrated
        assert config.sources.safety.is_layered

    def test_the_override_is_reported_value_by_value(self, config_dir: Path) -> None:
        """A safety limit that changed silently is the failure this prevents."""
        write_local(
            config_dir,
            "safety.local.yaml",
            {"limits": {"meridian": {"safety_margin_deg": 8.0}}},
        )
        (override,) = load_config_bundle(config_dir).sources.safety.overrides

        assert override.key == "limits.meridian.safety_margin_deg"
        assert override.value == 8.0
        assert override.previous != 8.0


class TestAnOverrideThatIsWrongFailsLoudlyAndNamesItself:
    def test_an_unknown_key_is_blamed_on_the_local_file(self, config_dir: Path) -> None:
        """`lattitude` merges cleanly, changes nothing, and would look exactly
        like a file that worked. `extra="forbid"` catches it; the blame is what
        stops the operator editing the wrong file to fix it."""
        write_local(config_dir, "equipment.local.yaml", {"site": {"lattitude": 41.3874}})

        with pytest.raises(ConfigError) as excinfo:
            load_config_bundle(config_dir)
        message = str(excinfo.value)

        assert "equipment.local.yaml" in message
        assert "site.lattitude" in message

    def test_an_error_in_the_shipped_file_is_not_blamed_on_the_local_one(
        self, config_dir: Path
    ) -> None:
        """The positive control: the blame has to be capable of pointing both
        ways, or it is not evidence of anything."""
        document = yaml.safe_load((config_dir / "equipment.yaml").read_text(encoding="utf-8"))
        document["site"]["nonsense_key"] = 1
        (config_dir / "equipment.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
        write_local(config_dir, "equipment.local.yaml", {"site": {"latitude": 41.0}})

        with pytest.raises(ConfigError) as excinfo:
            load_config_bundle(config_dir)
        message = str(excinfo.value)

        assert "site.nonsense_key" in message
        assert "equipment.local.yaml" not in message

    def test_a_malformed_override_is_not_silently_skipped(self, config_dir: Path) -> None:
        """Falling back to the shipped values is how an operator ends up
        observing from the placeholder site while believing they fixed it."""
        (config_dir / "equipment.local.yaml").write_text("site: [not, a, mapping\n", "utf-8")

        with pytest.raises(ConfigError, match="invalid YAML"):
            load_config_bundle(config_dir)

    def test_an_empty_override_is_an_error_rather_than_a_no_op(self, config_dir: Path) -> None:
        (config_dir / "equipment.local.yaml").write_text("", encoding="utf-8")
        with pytest.raises(ConfigError, match="empty"):
            load_config_bundle(config_dir)

    def test_an_out_of_range_value_is_blamed_on_the_local_file(self, config_dir: Path) -> None:
        write_local(config_dir, "equipment.local.yaml", {"site": {"latitude": 200.0}})
        with pytest.raises(ConfigError) as excinfo:
            load_config_bundle(config_dir)
        assert "equipment.local.yaml" in str(excinfo.value)


class TestLoadLayeredReportsItsSources:
    def test_it_names_both_files(self, config_dir: Path) -> None:
        local = write_local(config_dir, "equipment.local.yaml", {"site": {"latitude": 41.0}})
        _, sources = load_layered(EquipmentConfig, config_dir / "equipment.yaml")

        assert sources.shipped == config_dir / "equipment.yaml"
        assert sources.local == local
        assert sources.is_layered

    def test_it_names_one_when_there_is_no_override(self, config_dir: Path) -> None:
        _, sources = load_layered(EquipmentConfig, config_dir / "equipment.yaml")
        assert sources.local is None
        assert not sources.is_layered
