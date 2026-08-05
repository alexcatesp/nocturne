"""Executor transport settings.

CLAUDE.md section 6: every threshold, timeout and tolerance is a validated
configuration value, never a number typed into a comparison. SPEC section 5
defines three configuration files and none of them covers transport timing, so
these live in a validated model with documented defaults rather than in a
fourth YAML file. See docs/decisions/0005-executor-transport-settings.md.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from nocturne.executor.indi.protocol import MAX_MESSAGE_BYTES
from nocturne.schemas import StrictModel

PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class IndiSettings(StrictModel):
    """Where indiserver is and how patient the client is with it."""

    host: str = Field(default="localhost", min_length=1)
    #: The IANA-registered INDI port.
    port: Annotated[int, Field(gt=0, le=65535)] = 7624

    #: Opening the TCP connection. indiserver is on the loopback interface.
    connect_timeout_s: PositiveFloat = 10.0
    #: Waiting for a driver to define a property after it is asked to.
    property_timeout_s: PositiveFloat = 30.0
    #: Waiting for a written vector to leave the Busy state.
    write_timeout_s: PositiveFloat = 60.0
    #: Waiting for a device to answer CONNECTION.
    device_connect_timeout_s: PositiveFloat = 30.0

    #: Reconnection backoff after the server or a driver goes away.
    reconnect_initial_delay_s: PositiveFloat = 0.5
    reconnect_max_delay_s: PositiveFloat = 30.0
    reconnect_backoff_factor: Annotated[float, Field(ge=1.0)] = 2.0
    #: None means keep trying for as long as the process lives. A night is long
    #: and a USB hub that re-enumerates should not end it.
    reconnect_max_attempts: PositiveInt | None = None

    read_chunk_bytes: PositiveInt = 65536
    max_message_bytes: PositiveInt = MAX_MESSAGE_BYTES

    def backoff_delays(self) -> list[float]:
        """The reconnection delay sequence, for logging and for tests."""
        delays: list[float] = []
        delay = self.reconnect_initial_delay_s
        attempts = self.reconnect_max_attempts or 10
        for _ in range(attempts):
            delays.append(delay)
            delay = min(delay * self.reconnect_backoff_factor, self.reconnect_max_delay_s)
        return delays
