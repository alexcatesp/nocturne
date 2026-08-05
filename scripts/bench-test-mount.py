#!/usr/bin/env python3
"""Bench test for the Sky-Watcher Wave 150i — SPEC section 14, M1 HITL.

Proves the mount talks to the Pi over a direct USB serial cable, with no
SynScan app acting as a bridge.

**This script never moves the mount.** It connects, reads, writes one
non-pointing value, reads it back, and disconnects. There is no slew, no goto,
no park and no tracking anywhere in it: not one motor turns. Motion belongs to
stage two, with the OTA fitted, after the meridian calibration.

Run the procedure in docs/hardware-setup.md, not this file directly, unless you
know what you are doing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from nocturne.executor import IndiClient, IndiSettings  # noqa: E402
from nocturne.executor.indi.client import IndiError  # noqa: E402
from nocturne.schemas import ConfigError, load_config_bundle  # noqa: E402

#: Properties worth printing, in the order a person would want to read them.
REPORTED = (
    ("DRIVER_INFO", "driver"),
    ("EQUATORIAL_EOD_COORD", "position (RA/Dec)"),
    ("TELESCOPE_TRACK_STATE", "tracking"),
    ("TELESCOPE_PARK", "park state"),
    ("GEOGRAPHIC_COORD", "site"),
    ("MOUNT_TYPE", "mount type"),
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


async def run() -> int:
    step("Reading config/equipment.yaml")
    try:
        config = load_config_bundle(REPO_ROOT / "config")
    except ConfigError as exc:
        bad(str(exc))
        return verdict_fail("the configuration did not load")
    mount = config.equipment.mount
    good(f"{mount.device_label} on {mount.port} at {mount.baud} baud")
    if mount.connection != "serial":
        bad(f"mount.connection is '{mount.connection}', not 'serial'")
        return verdict_fail("this test is only meaningful for a serial connection")

    step("Connecting to indiserver")
    client = IndiClient(SETTINGS)
    try:
        await client.connect()
    except IndiError as exc:
        bad(str(exc))
        return verdict_fail("indiserver is not running — see step 3 of the procedure")
    good("connected")

    try:
        return await _exercise(client, config)
    finally:
        await client.aclose()


async def _exercise(client: IndiClient, config: object) -> int:
    equipment = config.equipment  # type: ignore[attr-defined]  # NocturneConfig
    mount = equipment.mount

    step("Waiting for the mount driver to announce itself")
    for _ in range(60):
        if client.devices():
            break
        await asyncio.sleep(0.5)
    devices = client.devices()
    if not devices:
        bad("no device appeared")
        return verdict_fail("indi_eqmod_telescope started but no mount was found")
    device = devices[0]
    good(f"device '{device}'")

    step("Pointing the driver at the serial port from equipment.yaml")
    try:
        await client.wait_for_property(device, "DEVICE_PORT", timeout=15.0)
        await client.write(device, "DEVICE_PORT", {"PORT": mount.port})
        good(f"port set to {mount.port}")
    except IndiError as exc:
        bad(str(exc))
        return verdict_fail("the driver would not accept the serial port")

    step("Connecting the mount  <-- this is the test")
    try:
        await client.connect_device(device)
    except IndiError as exc:
        bad(str(exc))
        say()
        say("    The cable is present but the mount did not answer on it.")
        say("    This is the outcome the project needs to know about.")
        return verdict_fail("the mount did not connect over direct USB serial without SynScan")
    if not client.is_device_connected(device):
        return verdict_fail("the mount reported itself not connected")
    good("THE MOUNT IS CONNECTED")

    step("Reading what it says about itself")
    for name, label in REPORTED:
        prop = client.get(device, name)
        if prop is None:
            say(f"    --    {label}: not offered by this driver")
            continue
        values = ", ".join(f"{k}={v}" for k, v in _readable(prop).items())
        good(f"{label}: {values}")

    step("Writing a value and reading it back (nothing moves)")
    written = await _write_and_read_back(client, device, equipment)
    if written is None:
        return verdict_fail("the mount accepted no write, so the link is read-only")
    good(written)

    step("Disconnecting")
    try:
        await client.disconnect_device(device)
        good("disconnected cleanly")
    except IndiError as exc:
        bad(f"did not disconnect cleanly: {exc}")

    return verdict_pass()


def _readable(prop: object) -> dict[str, str]:
    elements = prop.elements  # type: ignore[attr-defined]
    return {name: f"{element.value}" for name, element in elements.items()}


async def _write_and_read_back(
    client: IndiClient, device: str, equipment: object
) -> str | None:
    """Write one harmless value. Returns a description, or None if nothing took."""
    site = equipment.site  # type: ignore[attr-defined]

    # Preferred: the observing site. Meaningful, reversible, and moves nothing.
    if client.get(device, "GEOGRAPHIC_COORD") is not None:
        longitude = site.longitude % 360.0  # INDI uses 0..360 east of Greenwich
        try:
            result = await client.write(
                device,
                "GEOGRAPHIC_COORD",
                {"LAT": site.latitude, "LONG": longitude, "ELEV": site.elevation_m},
            )
        except IndiError:
            return None
        latitude = float(result["LAT"])
        if abs(latitude - site.latitude) < 0.01:
            return f"site latitude written and read back as {latitude:.4f}"
        return f"site written, mount reports latitude {latitude:.4f}"

    # Fallback: pick a slew *rate*. Selects a speed; commands no motion.
    rates = client.get(device, "TELESCOPE_SLEW_RATE")
    if rates is not None:
        names = list(rates.elements)
        try:
            await client.write(
                device, "TELESCOPE_SLEW_RATE", {n: (n == names[0]) for n in names}
            )
        except IndiError:
            return None
        return f"slew rate set to '{names[0]}' (a speed setting; nothing moved)"
    return None


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

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
