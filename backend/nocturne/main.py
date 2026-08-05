"""Nocturne entry point.

Milestone M1 provides one subcommand, ``check-config``: it loads and validates
the three configuration files and prints a short report. ``scripts/install.sh``
runs it as its final step, and the operator runs it before every session.

The FastAPI application (SPEC section 11.2) arrives with M2/M4; this module is
where it will be mounted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nocturne.schemas import ConfigError, NocturneConfig, load_config_bundle

DEFAULT_CONFIG_DIR = Path("config")


def _format_report(config: NocturneConfig) -> str:
    equipment = config.equipment
    meridian = config.safety.limits.meridian
    scale = equipment.imaging_plate_scale_arcsec_per_px()
    fov_width, fov_height = equipment.imaging_fov_arcmin()
    guide_scale = equipment.guide_plate_scale_arcsec_per_px()
    train = equipment.active_optical_train

    if meridian.calibrated:
        east, west = meridian.effective_hour_angle_limits_deg()
        meridian_line = (
            f"  Meridian:      calibrated {meridian.calibration_date}, "
            f"enforced hour angle {east:+.1f} deg to {west:+.1f} deg"
        )
    else:
        meridian_line = (
            "  Meridian:      NOT CALIBRATED — supervised and autonomous modes are "
            "refused (SPEC section 9.1). See docs/meridian-calibration.md."
        )

    return "\n".join(
        [
            "Nocturne configuration OK.",
            "",
            f"  Site:          {equipment.site.name} "
            f"({equipment.site.latitude:+.4f}, {equipment.site.longitude:+.4f}, "
            f"{equipment.site.elevation_m:.0f} m, {equipment.site.timezone})",
            f"  Optical train: {train.id} — {train.focal_length_mm:.0f} mm f/"
            f"{train.focal_ratio:.1f}" + (f", {train.corrector}" if train.corrector else ""),
            f"  Imaging:       {equipment.imaging_camera.name} — {scale:.3f} arcsec/px, "
            f"FOV {fov_width:.1f}' x {fov_height:.1f}'",
            f"  Guiding:       {equipment.guiding.mode} "
            f"({equipment.guiding.camera.name}) — {guide_scale:.2f} arcsec/px",
            f"  Mount:         {equipment.mount.device_label} via "
            f"{equipment.mount.indi_driver} over {equipment.mount.connection} "
            f"({equipment.mount.port or 'n/a'})",
            "  Filters:       "
            + ", ".join(
                f"{position}:{slot.name}"
                for position, slot in equipment.filter_wheel.populated_slots.items()
            ),
            f"  Autonomy:      {config.agent.autonomy_level} "
            f"(model {config.agent.model}, poll every "
            f"{config.agent.poll_interval_minutes} min)",
            meridian_line,
        ]
    )


def _check_config(config_dir: Path) -> int:
    try:
        config = load_config_bundle(config_dir)
    except ConfigError as exc:
        print(f"Nocturne configuration is INVALID.\n{exc}", file=sys.stderr)
        return 1
    print(_format_report(config))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(prog="nocturne", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check-config",
        help="validate config/*.yaml and print a summary of the configured rig",
    )
    check.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help="directory holding equipment.yaml, safety.yaml and agent.yaml",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line interface. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "check-config":
        config_dir: Path = args.config_dir
        return _check_config(config_dir)
    # argparse rejects anything else before we get here.
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
