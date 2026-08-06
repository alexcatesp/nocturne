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

from nocturne.devices import (
    MissingSerialDeviceError,
    describe_configured_port,
    require_configured_port,
)
from nocturne.schemas import ConfigError, NocturneConfig, load_config_bundle
from nocturne.schemas.equipment import Site
from nocturne.storage import describe_storage, inspect_storage

DEFAULT_CONFIG_DIR = Path("config")

#: The filesystem the startup report describes. FIELD-NOTES-M1 section 6 asks
#: for the root device specifically: it is what the operator can check by eye,
#: and on the reference rig it is the microSD everything else sits on.
REPORTED_FILESYSTEM = Path("/")


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
            *_site_lines(equipment.site),
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
            f"{equipment.mount.indi_driver} over {equipment.mount.connection}, "
            f"announced as {equipment.mount.indi_device_name!r}",
            f"  Mount port:    {describe_configured_port(equipment.mount.port)}",
            "  Filters:       "
            + ", ".join(
                f"{position}:{slot.name}"
                for position, slot in equipment.filter_wheel.populated_slots.items()
            ),
            f"  Autonomy:      {config.agent.autonomy_level} "
            f"(model {config.agent.model}, poll every "
            f"{config.agent.poll_interval_minutes} min)",
            *_storage_lines(),
            meridian_line,
        ]
    )


def _site_lines(site: Site) -> list[str]:
    """A banner above the report while the site is still the shipped one.

    Above, not below: it is the first thing that has to be fixed and the last
    thing anyone scrolls to. Nothing about wrong coordinates fails on its own —
    the run simply produces the wrong sky at the wrong times — so this is the
    only place it gets said.
    """
    if not site.is_placeholder:
        return []
    return [
        "  !! THE SITE COORDINATES ARE STILL THE SHIPPED PLACEHOLDER.",
        "     Replace site.latitude and site.longitude in config/equipment.yaml",
        "     with your own, to four decimal places, before observing.",
        "     Until you do, sunrise and sunset, target altitudes and the",
        "     meridian crossing times are all computed for somewhere else, and",
        "     nothing will report an error — the frames will simply be wrong.",
        "",
    ]


def _storage_lines() -> list[str]:
    """The storage report, and a warning if a stacking job would be refused.

    Never raises. This command exists to validate the configuration; storage is
    reported alongside it, and a filesystem that cannot be inspected must not
    turn a valid configuration into a failed check.
    """
    try:
        report = inspect_storage(REPORTED_FILESYSTEM)
    except OSError as exc:
        return [f"  Storage:       could not be inspected ({exc})"]

    lines = [f"  Storage:       {describe_storage(report)}"]
    if report.is_removable_flash:
        lines.append(
            "                 REMOVABLE FLASH — stacking jobs are refused on this "
            "device (SPEC section 2.2). Fit the NVMe SSD before M3."
        )
    return lines


def _check_config(config_dir: Path) -> int:
    try:
        config = load_config_bundle(config_dir)
    except ConfigError as exc:
        print(f"Nocturne configuration is INVALID.\n{exc}", file=sys.stderr)
        return 1
    print(_format_report(config))

    # After the report, not before: the operator sees what is configured even
    # when the device is missing, which is most of what they need to diagnose it.
    try:
        require_configured_port(
            config.equipment.mount.port, label=config.equipment.mount.device_label
        )
    except MissingSerialDeviceError as exc:
        print(f"\nNocturne cannot start.\n{exc}", file=sys.stderr)
        return 1
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
