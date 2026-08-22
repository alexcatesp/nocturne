"""Shared observing fixtures: a real site, frozen instants, calibrated limits.

Three things every pointing test needs and none of them should invent twice.

**A site with real coordinates.** The shipped ``equipment.yaml`` carries the
placeholder, and the pointing rules refuse to work from it (SPEC section 9.2),
so a test that wants to exercise altitude or hour angle needs somewhere to
stand. These are not the operator's coordinates — the repository ships nobody's
address — they are the worked example from ``docs/meridian-calibration.md``.

**Frozen instants.** Sun and hour angle are functions of time. A test that
reads the system clock passes tonight and fails in February.

**A calibrated safety configuration**, because the shipped one is uncalibrated
by design and uncalibrated refuses everything.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from nocturne.safety.sky import DEGREES_PER_HOUR, SkyPosition, local_sidereal_time_deg
from nocturne.schemas import SafetyConfig, Site

#: The worked example from docs/meridian-calibration.md: a terrace at 41.6 N.
TEST_SITE = Site(
    name="Test site — not anybody's address",
    latitude=41.6,
    longitude=-3.7,
    elevation_m=900.0,
    timezone="Europe/Madrid",
)

#: Deep night at the test site: the Sun is far below the horizon.
NIGHT = datetime(2026, 8, 22, 1, 0, tzinfo=UTC)

#: Around local solar noon at the test site: the Sun is high.
DAY = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

#: Measured meridian limits for a calibrated configuration. With the shipped
#: safety_margin_deg of 5 these enforce -20 east and +15 west.
EAST_LIMIT_DEG = -25.0
WEST_LIMIT_DEG = 20.0


def calibrated_safety_config(config_dir: Path) -> SafetyConfig:
    """The shipped configuration with the meridian calibration filled in."""
    with (config_dir / "safety.yaml").open(encoding="utf-8") as handle:
        raw: dict[str, Any] = copy.deepcopy(yaml.safe_load(handle))
    raw["limits"]["meridian"].update(
        {
            "calibrated": True,
            "calibration_date": "2026-08-01",
            "hour_angle_east_limit_deg": EAST_LIMIT_DEG,
            "hour_angle_west_limit_deg": WEST_LIMIT_DEG,
        }
    )
    return SafetyConfig.model_validate(raw)


def target_at(hour_angle_deg: float, dec_deg: float, when: datetime) -> SkyPosition:
    """The position that has this hour angle at this instant, from TEST_SITE.

    Tests are written in the quantity the limits are stated in — "thirty degrees
    east of the meridian" — rather than in a right ascension that means nothing
    on its own and stops meaning it the following month.
    """
    local = local_sidereal_time_deg(when, TEST_SITE.longitude)
    return SkyPosition(
        ra_hours=((local - hour_angle_deg) / DEGREES_PER_HOUR) % 24.0, dec_deg=dec_deg
    )
