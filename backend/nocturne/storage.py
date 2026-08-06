"""What Nocturne is running on, and whether it may be stacked onto.

SPEC section 2.2 lists an NVMe HAT and a 500 GB SSD as a required purchase,
with the reason stated plainly: "SDXC will not survive stacking write loads".
The reference rig is still on a 32 GB microSD (docs/FIELD-NOTES-M1.md section
6), and will be until the drive arrives.

Two separate things follow, and they are deliberately not the same thing:

* **Report.** Every startup says what the root filesystem is and how much room
  is left, so the operator is never guessing.
* **Refuse.** A stacking job will not start on removable flash. Not a warning,
  not a default-to-permissive: a refusal naming the device. A clear refusal is
  better than a corrupted card halfway through a night's stack.

There is no configuration value that turns the refusal off. NVMe is a SPEC
requirement, not a preference, and an override would exist precisely to be used
on the night the operator is in a hurry.

Classification reads sysfs, because the kernel already knows. The one
discrimination that is not obvious: an SD card and soldered eMMC both appear as
``mmcblk0``, and the only thing that tells them apart is ``device/type``,
which reads ``SD`` for a card and ``MMC`` for eMMC.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

#: Powers of two, so that what Nocturne prints matches what ``df -h`` prints.
BYTES_PER_GB: Final = 1024**3

SYS_ROOT: Final = Path("/sys")


class DeviceKind(StrEnum):
    """The medium behind a filesystem, as far as sysfs can tell."""

    NVME = "nvme"
    DISK = "disk"
    SD_CARD = "sd-card"
    EMMC = "emmc"
    USB = "usb"
    UNKNOWN = "unknown"


#: How each kind reads in a report line the operator sees.
_KIND_LABELS: Final[dict[DeviceKind, str]] = {
    DeviceKind.NVME: "NVMe SSD",
    DeviceKind.DISK: "disk",
    DeviceKind.SD_CARD: "removable SD card",
    DeviceKind.EMMC: "soldered eMMC",
    DeviceKind.USB: "USB",
    DeviceKind.UNKNOWN: "unidentified device",
}


class StorageUnsuitableError(Exception):
    """The storage behind a path must not be written to for this purpose."""


@dataclass(frozen=True, slots=True)
class BlockDevice:
    """The disk behind a filesystem."""

    name: str
    kind: DeviceKind
    removable: bool
    rotational: bool

    @property
    def is_removable_flash(self) -> bool:
        """Whether this is media that can be pulled out and will wear out.

        An SD card always is. Anything the kernel marks removable is: a USB
        stick sets the flag, and an SSD in a USB enclosure does not, which is
        the distinction that matters — refusing the enclosure would block a
        setup that is perfectly sound.
        """
        return self.kind is DeviceKind.SD_CARD or self.removable


@dataclass(frozen=True, slots=True)
class StorageReport:
    """One filesystem: what backs it, and how much of it is left."""

    path: Path
    device: str
    kind: DeviceKind
    removable: bool
    rotational: bool
    free_gb: float
    total_gb: float

    @property
    def is_removable_flash(self) -> bool:
        """See :attr:`BlockDevice.is_removable_flash`."""
        return self.kind is DeviceKind.SD_CARD or self.removable


def classify_block_device(major: int, minor: int, *, sys_root: Path = SYS_ROOT) -> BlockDevice:
    """Identify the block device with these numbers.

    Returns a device of kind :attr:`DeviceKind.UNKNOWN` when sysfs has nothing
    to say — inside a container, on an overlay, on a filesystem no block device
    backs. Unknown is reported as unknown; it is never quietly treated as good.
    """
    link = sys_root / "dev" / "block" / f"{major}:{minor}"
    try:
        node = link.resolve(strict=True)
    except OSError:
        return BlockDevice(
            name=f"{major}:{minor}",
            kind=DeviceKind.UNKNOWN,
            removable=False,
            rotational=False,
        )

    disk = node.parent if (node / "partition").exists() else node
    return BlockDevice(
        name=disk.name,
        kind=_kind_of(disk),
        removable=_read_flag(disk / "removable"),
        rotational=_read_flag(disk / "queue" / "rotational"),
    )


def _kind_of(disk: Path) -> DeviceKind:
    name = disk.name
    if name.startswith("nvme"):
        return DeviceKind.NVME
    if name.startswith("mmcblk"):
        # The only thing separating a card from soldered eMMC.
        return (
            DeviceKind.SD_CARD
            if _read_text(disk / "device" / "type") == "SD"
            else (DeviceKind.EMMC)
        )
    if "/usb" in str(disk):
        return DeviceKind.USB
    if name.startswith(("sd", "hd", "vd", "xvd")):
        return DeviceKind.DISK
    return DeviceKind.UNKNOWN


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_flag(path: Path) -> bool:
    return _read_text(path) == "1"


def inspect_storage(path: Path, *, sys_root: Path = SYS_ROOT) -> StorageReport:
    """Report on the filesystem holding ``path``.

    Raises:
        OSError: if ``path`` does not exist. A startup check that silently
            skips a missing directory is a startup check that reports nothing.
    """
    stat = os.stat(path)
    usage = os.statvfs(path)
    device = classify_block_device(
        os.major(stat.st_dev), os.minor(stat.st_dev), sys_root=sys_root
    )
    return StorageReport(
        path=path,
        device=device.name,
        kind=device.kind,
        removable=device.removable,
        rotational=device.rotational,
        free_gb=usage.f_bavail * usage.f_frsize / BYTES_PER_GB,
        total_gb=usage.f_blocks * usage.f_frsize / BYTES_PER_GB,
    )


def describe_storage(report: StorageReport) -> str:
    """One line, for the startup report and the logs."""
    return (
        f"{report.path} on /dev/{report.device} "
        f"({_KIND_LABELS[report.kind]}) — "
        f"{report.free_gb:.1f} GB free of {report.total_gb:.1f} GB"
    )


def require_stacking_storage(report: StorageReport, *, minimum_free_gb: float) -> None:
    """Refuse a stacking job the storage cannot be trusted with.

    ``minimum_free_gb`` comes from ``safety.yaml``
    ``abort_conditions.disk_free_min_gb``; there is no default here.

    Raises:
        StorageUnsuitableError: on removable flash, on unidentifiable storage,
            or with less than ``minimum_free_gb`` free.
    """
    if report.is_removable_flash:
        raise StorageUnsuitableError(
            f"{report.path} is on /dev/{report.device}, "
            f"{_KIND_LABELS[report.kind]} — removable flash. Stacking writes "
            "far more than a card will survive, and a card that fails mid-stack "
            "takes the night's data with it (SPEC section 2.2). Move the data "
            "directory to the NVMe SSD and try again."
        )
    if report.kind is DeviceKind.UNKNOWN:
        raise StorageUnsuitableError(
            f"the storage behind {report.path} could not be identified, so it "
            "cannot be confirmed as anything other than removable flash. "
            "Stacking will not start on storage Nocturne cannot see the type of."
        )
    if report.free_gb < minimum_free_gb:
        raise StorageUnsuitableError(
            f"{report.path} has {report.free_gb:.1f} GB free on /dev/{report.device}; "
            f"safety.yaml requires at least {minimum_free_gb:.1f} GB before a "
            "stacking job starts."
        )


__all__ = [
    "BYTES_PER_GB",
    "BlockDevice",
    "DeviceKind",
    "StorageReport",
    "StorageUnsuitableError",
    "classify_block_device",
    "describe_storage",
    "inspect_storage",
    "require_stacking_storage",
]
