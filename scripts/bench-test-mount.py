#!/usr/bin/env python3
"""Bench test for the Sky-Watcher Wave 150i — SPEC section 14, M1 HITL.

Proves the mount talks to the Pi over a direct USB serial cable, with no
SynScan app acting as a bridge.

**This script never moves the mount.** It brings the mount up, reads, writes one
non-pointing value, reads it back, and disconnects. There is no slew, no goto,
no park and no tracking anywhere in it: not one motor turns. Motion belongs to
stage two, with the OTA fitted, after the meridian calibration.

It drives the **real** bring-up path — ``Executor`` and ``MountLink``, with the
safety governor in front of both — rather than a parallel one written just for
the bench. Two reasons. A parallel path drifts: it gets fixed while the real one
does not, and then the thing the operator tested is not the thing that runs at
night. And the bring-up sequence carries a safety action of its own — it clamps
the mount's slew rate down from the driver's 800x sidereal default — so running
it here exercises that clamp against real hardware rather than only against a
fake server.

Run the procedure in docs/hardware-setup.md, not this file directly, unless you
know what you are doing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from nocturne.devices import (  # noqa: E402
    SERIAL_GROUP,
    MissingSerialDeviceError,
    describe_configured_port,
)
from nocturne.executor import IndiClient, IndiSettings  # noqa: E402
from nocturne.executor.executor import Executor  # noqa: E402
from nocturne.executor.indi.client import IndiError  # noqa: E402
from nocturne.executor.mount import MountBringUpError, MountLink  # noqa: E402
from nocturne.safety import SafetyGovernor, SafetyViolation  # noqa: E402
from nocturne.schemas import ConfigError, NocturneConfig, load_config_bundle  # noqa: E402

#: Properties worth printing, in the order a person would want to read them.
REPORTED = (
    ("DRIVER_INFO", "driver"),
    ("MOUNTINFORMATION", "controller"),
    ("STEPPERS", "encoder counts"),
    ("EQUATORIAL_EOD_COORD", "position (RA/Dec)"),
    ("TELESCOPE_TRACK_STATE", "tracking"),
    ("TELESCOPE_PARK", "park state"),
    ("GEOGRAPHIC_COORD", "site"),
    ("MOUNT_TYPE", "mount type"),
    ("SLEWSPEEDS", "slew speeds"),
)

SETTINGS = IndiSettings(
    connect_timeout_s=15.0,
    property_timeout_s=30.0,
    write_timeout_s=30.0,
    device_connect_timeout_s=45.0,
    reconnect_max_attempts=1,
)


def say(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    print(f"\n  {message}", flush=True)


def good(message: str) -> None:
    print(f"    ok    {message}", flush=True)


def bad(message: str) -> None:
    print(f"    FAIL  {message}", flush=True)


def verdict_pass() -> int:
    say()
    say("RESULT: PASS — the Wave 150i is talking to the Pi over direct USB serial.")
    say()
    say("  Nothing was moved. Stop indiserver with Ctrl+C in the other window.")
    say("  Next: the meridian calibration in docs/meridian-calibration.md.")
    return 0


def verdict_fail(reason: str) -> int:
    say()
    say(f"RESULT: FAIL — {reason}")
    say()
    say("  Do not work around this. Capture the logs as described in")
    say("  docs/hardware-setup.md under 'Fail', and send the file.")
    return 1


def _in_serial_group() -> bool:
    """Whether this process can open a device owned by root:dialout."""
    import grp
    import os

    try:
        group = grp.getgrnam(SERIAL_GROUP)
    except KeyError:
        # No such group on this system. Say nothing rather than block: the
        # connect below will fail loudly enough if permissions are the problem.
        return True
    return group.gr_gid in os.getgroups()


async def run() -> int:
    step("Reading config/equipment.yaml")
    try:
        config = load_config_bundle(REPO_ROOT / "config")
    except ConfigError as exc:
        bad(str(exc))
        return verdict_fail("the configuration did not load")

    mount = config.equipment.mount
    good(f"{mount.device_label}, announced by the driver as '{mount.indi_device_name}'")
    good(f"port: {describe_configured_port(mount.port)}")
    good(f"baud: {mount.baud}")
    good(f"slew ceiling: {mount.slew_rate_max_deg_s} deg/s")
    if mount.connection != "serial":
        bad(f"mount.connection is '{mount.connection}', not 'serial'")
        return verdict_fail("this test is only meaningful for a serial connection")
    if mount.port is not None and not mount.uses_stable_port_path:
        say(f"    note  {mount.port} moves if another USB serial device is plugged in.")
        say("          Prefer the /dev/serial/by-id/ path: ls -l /dev/serial/by-id/")

    step(f"Checking {SERIAL_GROUP} group membership")
    if not _in_serial_group():
        bad(f"this account is not in the {SERIAL_GROUP} group")
        say(f"          Fix it with:  sudo usermod -aG {SERIAL_GROUP} $USER")
        say("          Then log out and log back in — it does not take effect until you do.")
        return verdict_fail(f"no permission to open the serial port without {SERIAL_GROUP}")
    good(f"in the {SERIAL_GROUP} group")

    step("Connecting to indiserver")
    executor = Executor(IndiClient(SETTINGS), SafetyGovernor(config.safety))
    try:
        await executor.start()
    except IndiError as exc:
        bad(str(exc))
        return verdict_fail("indiserver is not running — see step 3 of the procedure")
    good("connected")

    try:
        return await _exercise(executor, config)
    finally:
        await executor.aclose()


async def _exercise(executor: Executor, config: NocturneConfig) -> int:
    equipment = config.equipment
    mount = equipment.mount
    device = mount.indi_device_name

    step("Waiting for the mount driver to announce itself")
    try:
        await executor.wait_for_device(device, timeout=30.0)
    except IndiError:
        bad(f"no device called '{device}' appeared")
        offered = executor.devices()
        if offered:
            say(f"          indiserver is offering: {', '.join(offered)}")
            say("          If one of those is the mount, put its name in")
            say("          config/equipment.yaml under mount.indi_device_name.")
        else:
            say("          indiserver is offering no devices at all.")
        return verdict_fail("the mount driver did not announce the configured device")
    good(f"device '{device}'")

    step("Bringing the mount up  <-- this is the test")
    say("          baud before CONNECT, then the port, then CONNECT, then the")
    say("          slew rate ceiling. Nothing in that sequence turns a motor.")
    link = MountLink(executor, mount)
    async with link:
        try:
            await link.bring_up(timeout=45.0)
        except MissingSerialDeviceError as exc:
            bad(str(exc))
            return verdict_fail("the configured serial port is not present")
        except MountBringUpError as exc:
            bad(str(exc))
            return verdict_fail("the driver did not offer what equipment.yaml configures")
        except SafetyViolation as exc:
            bad(str(exc))
            return verdict_fail("the safety governor refused part of the bring-up")
        except IndiError as exc:
            bad(str(exc))
            # Which failure this is matters more than any other distinction in
            # this script. "The mount never answered" is the result that would
            # send the project to the WiFi fallback (ADR 0011). A driver that
            # connected and then did not offer some vector is a different thing
            # entirely, and reporting it as a link failure would be the exact
            # mistake FIELD-NOTES-M1 section 5.1 warns about.
            if executor.is_device_connected(device):
                say()
                say("    The mount CONNECTED. The link over USB serial works.")
                say("    What failed came after that, so this is not a cable problem.")
                say("    Check that mount.indi_driver in equipment.yaml is right for")
                say("    this mount: a driver that is not indi_eqmod does not offer")
                say("    the properties Nocturne needs.")
                return verdict_fail(
                    "the mount connected, but the driver did not behave as expected "
                    "after connecting — NOT a link failure"
                )
            say()
            say("    The cable is present but the mount did not answer on it.")
            say("    This is the outcome the project needs to know about.")
            return verdict_fail(
                "the mount did not connect over direct USB serial without SynScan"
            )

        if not executor.is_device_connected(device):
            return verdict_fail("the mount reported itself not connected")
        good("THE MOUNT IS CONNECTED")
        good(f"port in use: {link.port_in_use}")
        good(
            f"slew ceiling applied: {link.slew_speed_multiple}x sidereal "
            "(the driver's own default is 800x)"
        )

        step("Reading what it says about itself")
        for name, label in REPORTED:
            prop = executor.get_property(device, name)
            if prop is None:
                say(f"    --    {label}: not offered by this driver")
                continue
            values = ", ".join(f"{k}={v}" for k, v in _readable(prop).items())
            good(f"{label}: {values}")

        step("Writing a value and reading it back (nothing moves)")
        written = await _write_and_read_back(executor, device, equipment)
        if written is None:
            return verdict_fail("the mount accepted no write, so the link is read-only")
        good(written)

        step("Disconnecting")
        try:
            await executor.disconnect_device(device)
            good("disconnected cleanly")
        except (IndiError, SafetyViolation) as exc:
            bad(f"did not disconnect cleanly: {exc}")

    return verdict_pass()


def _readable(prop: object) -> dict[str, str]:
    elements = prop.elements  # type: ignore[attr-defined]
    return {name: f"{element.value}" for name, element in elements.items()}


async def _write_and_read_back(
    executor: Executor, device: str, equipment: object
) -> str | None:
    """Write one harmless value. Returns a description, or None if nothing took.

    The observing site: meaningful, reversible, and it moves nothing. The
    reference rig reported GEOGRAPHIC_COORD as all zeros, so this is also the
    value Nocturne will have to set on every connect (FIELD-NOTES-M1 section 4).
    """
    site = equipment.site  # type: ignore[attr-defined]

    if executor.get_property(device, "GEOGRAPHIC_COORD") is None:
        return None
    longitude = site.longitude % 360.0  # INDI uses 0..360 east of Greenwich
    try:
        result = await executor.set_property(
            device,
            "GEOGRAPHIC_COORD",
            {"LAT": site.latitude, "LONG": longitude, "ELEV": site.elevation_m},
        )
    except (IndiError, SafetyViolation):
        return None
    if result is None:
        return None
    latitude = float(result["LAT"])
    if abs(latitude - site.latitude) < 0.01:
        return f"site latitude written and read back as {latitude:.4f}"
    return f"site written, mount reports latitude {latitude:.4f}"


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    # Without a handler, a library warning arrives on stderr with no prefix and
    # reads like a line this script printed. Mark them.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("    log   %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])

    say("Wave 150i bench test — nothing here moves the mount.")
    say("Make sure the SynScan app is closed everywhere before you continue.")
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        say()
        return verdict_fail("interrupted — if it hung, say so and send the logs")
    except Exception as exc:
        # An operator at a keyboard in the dark gets a sentence, not a traceback.
        say()
        say(f"    unexpected error: {type(exc).__name__}: {exc}")
        return verdict_fail("the test hit an error it did not expect")


if __name__ == "__main__":
    raise SystemExit(main())
