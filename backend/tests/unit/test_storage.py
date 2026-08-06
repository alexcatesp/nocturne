"""Root filesystem inspection and the stacking storage gate.

docs/FIELD-NOTES-M1.md section 6: the reference rig runs from a 32 GB microSD.
SPEC section 2.2 records why that is not acceptable for stacking — "SDXC will
not survive stacking write loads" — and makes NVMe a required purchase, a
prerequisite of M6.

So Nocturne reports what it is running on at startup, and refuses to start a
stacking job on removable flash. "A clear refusal at M3 is better than a
corrupted card mid-stack."

Classification is tested against fabricated sysfs trees rather than the machine
the suite happens to run on: CI has no microSD, and a check that cannot be
exercised is a check nobody should trust.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nocturne.storage import (
    BYTES_PER_GB,
    DeviceKind,
    StorageReport,
    StorageUnsuitableError,
    classify_block_device,
    describe_storage,
    inspect_storage,
    require_stacking_storage,
)


def build_sysfs(
    root: Path,
    *,
    major: int,
    minor: int,
    disk: str,
    partition: str | None = None,
    removable: str = "0",
    rotational: str = "0",
    device_type: str | None = None,
    under_usb: bool = False,
) -> Path:
    """Write a sysfs tree shaped like the kernel's, for one block device."""
    bus = "usb1/1-1" if under_usb else "platform/soc"
    disk_dir = root / "devices" / bus / "block" / disk
    disk_dir.mkdir(parents=True)
    (disk_dir / "removable").write_text(f"{removable}\n")
    (disk_dir / "queue").mkdir()
    (disk_dir / "queue" / "rotational").write_text(f"{rotational}\n")
    if device_type is not None:
        (disk_dir / "device").mkdir()
        (disk_dir / "device" / "type").write_text(f"{device_type}\n")

    target = disk_dir
    if partition is not None:
        target = disk_dir / partition
        target.mkdir()
        (target / "partition").write_text("2\n")

    links = root / "dev" / "block"
    links.mkdir(parents=True, exist_ok=True)
    (links / f"{major}:{minor}").symlink_to(target)
    return root


class TestClassification:
    """One test per storage medium the operator could plausibly boot from."""

    def test_a_microsd_card_is_recognised_as_an_sd_card(self, tmp_path: Path) -> None:
        """The reference rig, today. mmc device/type is SD for a card."""
        build_sysfs(
            tmp_path,
            major=179,
            minor=2,
            disk="mmcblk0",
            partition="mmcblk0p2",
            device_type="SD",
        )
        device = classify_block_device(179, 2, sys_root=tmp_path)

        assert device.name == "mmcblk0"
        assert device.kind is DeviceKind.SD_CARD
        assert device.is_removable_flash

    def test_soldered_emmc_is_not_an_sd_card(self, tmp_path: Path) -> None:
        """Same driver, same node naming, different medium: device/type is MMC."""
        build_sysfs(
            tmp_path,
            major=179,
            minor=2,
            disk="mmcblk0",
            partition="mmcblk0p2",
            device_type="MMC",
        )
        device = classify_block_device(179, 2, sys_root=tmp_path)

        assert device.kind is DeviceKind.EMMC
        assert not device.is_removable_flash

    def test_the_nvme_the_spec_requires_is_recognised(self, tmp_path: Path) -> None:
        build_sysfs(tmp_path, major=259, minor=1, disk="nvme0n1", partition="nvme0n1p2")
        device = classify_block_device(259, 1, sys_root=tmp_path)

        assert device.kind is DeviceKind.NVME
        assert not device.is_removable_flash

    def test_a_usb_stick_is_removable_flash(self, tmp_path: Path) -> None:
        build_sysfs(
            tmp_path,
            major=8,
            minor=1,
            disk="sda",
            partition="sda1",
            removable="1",
            under_usb=True,
        )
        device = classify_block_device(8, 1, sys_root=tmp_path)

        assert device.kind is DeviceKind.USB
        assert device.is_removable_flash

    def test_an_ssd_in_a_usb_enclosure_is_not_removable_flash(self, tmp_path: Path) -> None:
        """An enclosure reports removable=0. Refusing it would block a real setup."""
        build_sysfs(
            tmp_path,
            major=8,
            minor=1,
            disk="sda",
            partition="sda1",
            removable="0",
            under_usb=True,
        )
        device = classify_block_device(8, 1, sys_root=tmp_path)

        assert device.kind is DeviceKind.USB
        assert not device.is_removable_flash

    def test_a_sata_disk_is_a_plain_disk(self, tmp_path: Path) -> None:
        build_sysfs(tmp_path, major=8, minor=1, disk="sda", partition="sda1")
        device = classify_block_device(8, 1, sys_root=tmp_path)

        assert device.kind is DeviceKind.DISK
        assert not device.is_removable_flash

    def test_a_whole_disk_without_a_partition_still_classifies(self, tmp_path: Path) -> None:
        build_sysfs(tmp_path, major=259, minor=0, disk="nvme0n1")
        assert classify_block_device(259, 0, sys_root=tmp_path).kind is DeviceKind.NVME

    def test_an_unknown_device_says_unknown_rather_than_guessing(self, tmp_path: Path) -> None:
        """No sysfs entry — a container, or a filesystem the kernel does not back."""
        (tmp_path / "dev" / "block").mkdir(parents=True)
        device = classify_block_device(0, 42, sys_root=tmp_path)

        assert device.kind is DeviceKind.UNKNOWN
        assert device.name == "0:42"
        assert not device.is_removable_flash


class TestTheStackingGate:
    """FIELD-NOTES-M1 section 6, and SPEC section 2.2."""

    def report(self, kind: DeviceKind, *, removable: bool, free_gb: float) -> StorageReport:
        return StorageReport(
            path=Path("/"),
            device="mmcblk0",
            kind=kind,
            removable=removable,
            rotational=False,
            free_gb=free_gb,
            total_gb=29.0,
        )

    def test_a_microsd_is_refused_however_much_space_is_free(self) -> None:
        report = self.report(DeviceKind.SD_CARD, removable=True, free_gb=10_000.0)
        with pytest.raises(StorageUnsuitableError, match="removable flash"):
            require_stacking_storage(report, minimum_free_gb=20.0)

    def test_the_refusal_names_the_device_and_says_what_to_do(self) -> None:
        report = self.report(DeviceKind.SD_CARD, removable=True, free_gb=25.0)
        with pytest.raises(StorageUnsuitableError) as raised:
            require_stacking_storage(report, minimum_free_gb=20.0)

        message = str(raised.value)
        assert "mmcblk0" in message
        assert "NVMe" in message

    def test_nvme_with_room_is_accepted(self) -> None:
        report = self.report(DeviceKind.NVME, removable=False, free_gb=400.0)
        require_stacking_storage(report, minimum_free_gb=20.0)

    def test_nvme_without_room_is_refused(self) -> None:
        """safety.yaml abort_conditions.disk_free_min_gb, applied before the job."""
        report = self.report(DeviceKind.NVME, removable=False, free_gb=19.9)
        with pytest.raises(StorageUnsuitableError, match=r"19\.9 GB free"):
            require_stacking_storage(report, minimum_free_gb=20.0)

    def test_unknown_storage_is_refused_rather_than_assumed_good(self) -> None:
        """If we cannot tell what we are writing to, we do not write to it."""
        report = self.report(DeviceKind.UNKNOWN, removable=False, free_gb=400.0)
        with pytest.raises(StorageUnsuitableError, match="could not be identified"):
            require_stacking_storage(report, minimum_free_gb=20.0)

    def test_the_threshold_comes_from_the_caller_not_from_a_constant(self) -> None:
        report = self.report(DeviceKind.NVME, removable=False, free_gb=50.0)
        require_stacking_storage(report, minimum_free_gb=20.0)
        with pytest.raises(StorageUnsuitableError):
            require_stacking_storage(report, minimum_free_gb=100.0)


class TestInspectingTheRealFilesystem:
    """A smoke test on whatever the suite is running on. It must never raise."""

    def test_the_root_filesystem_can_always_be_inspected(self) -> None:
        report = inspect_storage(Path("/"))

        assert report.total_gb > 0.0
        assert report.free_gb >= 0.0
        assert report.free_gb <= report.total_gb
        assert isinstance(report.kind, DeviceKind)

    def test_it_reports_the_path_it_was_asked_about(self, tmp_path: Path) -> None:
        assert inspect_storage(tmp_path).path == tmp_path

    def test_the_description_is_one_line_an_operator_can_read(self) -> None:
        line = describe_storage(inspect_storage(Path("/")))

        assert "\n" not in line
        assert "GB free" in line

    def test_a_missing_path_fails_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            inspect_storage(tmp_path / "no-such-directory")


class TestUnits:
    def test_a_gigabyte_is_a_power_of_two_gigabyte(self) -> None:
        """Matching what df -h and the operator's file manager report."""
        assert BYTES_PER_GB == 1024**3
