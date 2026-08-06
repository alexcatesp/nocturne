"""Is the device the operator named actually there?

``mount.port`` is optional. Left out, ``indi_eqmod`` finds the mount by itself
and fills ``DEVICE_PORT.PORT`` with the right ``/dev/serial/by-id/`` path
(docs/FIELD-NOTES-M1.md section 2.3), which is the sensible default and the
only one that can be shipped in a repository — a by-id path carries a serial
number, and one operator's serial number is a dead path for everybody else.

Setting it is an override. An override exists because the operator wants *that*
device rather than whatever the driver picked, so a configured port that is not
present is a hard failure naming the path. Falling back to auto-detection would
mean silently using a different mount from the one Nocturne was told to use,
and that is precisely how a cable moved to the wrong socket goes unnoticed
until something is pointing the wrong way in the dark.
"""

from __future__ import annotations

import os
import stat
from typing import Final

#: Where udev keeps the paths that survive a reboot. Named in the messages
#: below because it is the first thing to look at when a port has gone missing.
BY_ID_DIRECTORY: Final = "/dev/serial/by-id/"

#: The group that owns /dev/ttyACM* (FIELD-NOTES-M1 section 2.4). Not being in
#: it produces an error that reads like a cable fault, so it is named up front.
SERIAL_GROUP: Final = "dialout"


class MissingSerialDeviceError(Exception):
    """The configured serial port is not present on this machine."""


def serial_device_exists(port: str) -> bool:
    """Whether ``port`` is a character device that can be opened.

    Follows symlinks, because a ``/dev/serial/by-id/`` path is one: when the
    mount is unplugged the link stays and its target vanishes, and a check that
    only asked whether the link existed would answer yes to an absent mount.

    A regular file at the path is *not* a device. That happens when something
    has written where a device node belongs, and treating it as present would
    hand the driver a file to talk to.
    """
    try:
        mode = os.stat(port).st_mode
    except OSError:
        # Absent, dangling, or unreadable. All three mean "do not proceed",
        # and the caller's message covers all three.
        return False
    return stat.S_ISCHR(mode)


def require_configured_port(port: str | None, *, label: str) -> None:
    """Refuse to continue if a configured port is not there.

    Takes the port rather than the whole :class:`~nocturne.schemas.equipment.Mount`
    so that this module knows nothing about the configuration schema: it answers
    one question about one path, and the caller decides which path to ask about.

    Raises:
        MissingSerialDeviceError: if ``port`` is set and absent. ``None`` is not
            an error: it means "use the port the driver reports".
    """
    if port is None or serial_device_exists(port):
        return
    raise MissingSerialDeviceError(
        f"{label} is configured on {port}, and that device is not present.\n"
        "  Nocturne will not guess a different one: it was told to use this "
        "port, and quietly using another would hide a cable in the wrong "
        "socket.\n"
        f"  Check the mount is powered and plugged in, then list what is "
        f"there:  ls -l {BY_ID_DIRECTORY}\n"
        f"  If nothing is listed, it is the cable, the socket or the power. If "
        f"a different path is listed, put that one in config/equipment.yaml — "
        f"or remove the port: line entirely and let the driver find it.\n"
        f"  If the path is listed but this still fails, check the account is "
        f"in the {SERIAL_GROUP} group and that you have logged out and back in "
        f"since it was added."
    )


def describe_configured_port(port: str | None) -> str:
    """One line for the startup report."""
    if port is None:
        return "not configured — the driver reports its own (recommended)"
    state = "present" if serial_device_exists(port) else "NOT PRESENT"
    return f"{port} ({state})"


__all__ = [
    "BY_ID_DIRECTORY",
    "SERIAL_GROUP",
    "MissingSerialDeviceError",
    "describe_configured_port",
    "require_configured_port",
    "serial_device_exists",
]
