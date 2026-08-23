"""Which measured calibration applies to the rig, right now — ADR 0021.

The store keeps every calibration ever recorded; the governor takes at most
one. This module is the join, and it is deliberately the only place that knows
both — ``nocturne.safety`` never imports the store, so the safety layer stays a
pure function of what it was handed (CLAUDE.md section 2).

The matching rules are geometry, not preference. A calibration applies only if
the rig is at the latitude it was measured at, with the same counterweight
state and the same optical train. Anything else is a different geometry wearing
the same numbers.
"""

from __future__ import annotations

from nocturne.safety.rules import MeasuredBand
from nocturne.schemas import EquipmentConfig, SafetyConfig
from nocturne.store import CalibrationStore


def resolve_measured_band(
    store: CalibrationStore, equipment: EquipmentConfig, safety: SafetyConfig
) -> MeasuredBand | None:
    """The calibration in force for this rig at this site, or ``None``.

    ``None`` means uncalibrated by this route. It is never a fallback to
    something permissive: with no band from here and none in ``safety.yaml``,
    every pointing command is refused (ADR 0019).

    Called **once, at startup**. The governor holds what it returns for the life
    of the process, because a limit that can change under a running session is
    not a limit (ADR 0010).
    """
    profile = store.latest_for(
        latitude_deg=equipment.site.latitude,
        latitude_tolerance_deg=safety.limits.meridian.latitude_tolerance_deg,
        counterweight_fitted=equipment.mount.counterweight_fitted,
        optical_train_id=equipment.active_optical_train.id,
    )
    if profile is None:
        return None
    return MeasuredBand(
        east_deg=profile.hour_angle_east_deg,
        west_deg=profile.hour_angle_west_deg,
        source=(
            f"measured {profile.recorded_at.date()} at latitude "
            f"{profile.latitude_deg:+.4f}, train {profile.optical_train_id}"
        ),
    )


__all__ = ["resolve_measured_band"]
