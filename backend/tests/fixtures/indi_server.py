"""Run a real ``indiserver`` with the INDI simulator drivers.

SPEC section 13.1: the entire test suite runs against the simulator drivers, no
hardware required. This helper starts an isolated indiserver — its own TCP port
and its own local socket path, so several can run at once — and can kill one
driver out from under it to reproduce the failure M1 has to survive.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

#: The five device classes of SPEC section 14, M1, and the drivers that
#: simulate them (SPEC section 13.1).
SIMULATOR_DRIVERS: dict[str, str] = {
    "mount": "indi_simulator_telescope",
    "imaging_camera": "indi_simulator_ccd",
    "guide_camera": "indi_simulator_guide",
    "focuser": "indi_simulator_focus",
    "filter_wheel": "indi_simulator_wheel",
}

#: The device name each simulator announces on the wire.
SIMULATOR_DEVICES: dict[str, str] = {
    "mount": "Telescope Simulator",
    "imaging_camera": "CCD Simulator",
    "guide_camera": "Guide Simulator",
    "focuser": "Focuser Simulator",
    "filter_wheel": "Filter Simulator",
}


def simulators_available() -> bool:
    """Whether indiserver and all five simulator drivers are on PATH."""
    return shutil.which("indiserver") is not None and all(
        shutil.which(driver) is not None for driver in SIMULATOR_DRIVERS.values()
    )


def free_port() -> int:
    """An unused TCP port on the loopback interface."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return port


class IndiServerProcess:
    """An isolated ``indiserver`` running the simulator drivers."""

    def __init__(
        self,
        drivers: Sequence[str] | None = None,
        *,
        port: int | None = None,
        max_driver_restarts: int = 10,
    ) -> None:
        self.drivers = list(drivers or SIMULATOR_DRIVERS.values())
        self.port = port or free_port()
        self._max_driver_restarts = max_driver_restarts
        # indiserver also binds a local socket, at /tmp/indiserver by default.
        # Without a unique path here a second instance fails with EADDRINUSE
        # however free its TCP port is.
        self._socket_path = f"/tmp/indiserver-nocturne-{os.getpid()}-{self.port}"  # noqa: S108
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> IndiServerProcess:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def pid(self) -> int:
        """The indiserver process id."""
        if self._process is None:
            raise RuntimeError("indiserver is not running")
        return self._process.pid

    @property
    def is_running(self) -> bool:
        """Whether indiserver is still up."""
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        """Start indiserver and wait for its port to accept connections."""
        command = [
            "indiserver",
            "-p",
            str(self.port),
            "-u",
            self._socket_path,
            "-r",
            str(self._max_driver_restarts),
            *self.drivers,
        ]
        self._process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._wait_for_port()

    def stop(self) -> None:
        """Stop indiserver and every driver it spawned."""
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self._process.kill()
            self._process.wait(timeout=10)
        self._process = None

    def _wait_for_port(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError("indiserver exited during startup")
            with socket.socket() as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(("127.0.0.1", self.port))
                except OSError:
                    time.sleep(0.1)
                    continue
            return
        raise TimeoutError(f"indiserver did not open port {self.port}")

    def driver_pids(self) -> dict[str, int]:
        """Driver executable name to process id, for the drivers it spawned.

        Matching is done through ``/proc`` rather than by name: the driver
        executables are longer than the fifteen characters ``pkill`` compares
        against, and indiserver's own command line contains every driver name,
        so a name-based kill takes down the server instead of the driver.
        """
        if self._process is None:
            return {}
        found: dict[str, int] = {}
        listing = subprocess.run(
            ["pgrep", "-P", str(self._process.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        for raw in listing.stdout.split():
            child = int(raw)
            try:
                cmdline = Path(f"/proc/{child}/cmdline").read_bytes()
            except OSError:  # pragma: no cover - the child exited underneath us
                continue
            executable = cmdline.split(b"\0")[0].decode(errors="replace")
            if executable:
                found[os.path.basename(executable)] = child
        return found

    def kill_driver(self, driver: str) -> int:
        """SIGKILL one driver. indiserver respawns it; the client must cope."""
        pid = self.driver_pids().get(driver)
        if pid is None:
            raise RuntimeError(f"{driver} is not running under this indiserver")
        os.kill(pid, signal.SIGKILL)
        return pid
