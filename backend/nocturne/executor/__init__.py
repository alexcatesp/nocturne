"""Executor — SPEC section 3, layers 0 and 1.

The Ekos DBus bridge, direct INDI access, and the facade that will not perform
anything the safety governor has not approved.
"""

from __future__ import annotations

from .ekos import (
    BridgeState,
    EkosBridge,
    EkosError,
    EkosInterfaceError,
    EkosUnavailableError,
)
from .executor import Executor
from .indi.client import (
    ConnectionState,
    DeviceAppeared,
    DeviceVanished,
    DriverMessage,
    IndiClient,
    IndiConnectionError,
    IndiError,
    IndiEvent,
    IndiTimeoutError,
    PropertyChanged,
    ServerConnected,
    ServerDisconnected,
)
from .indi.protocol import (
    Property,
    PropertyKind,
    PropertyPermission,
    PropertyState,
    SwitchRule,
)
from .settings import IndiSettings

__all__ = [
    "BridgeState",
    "ConnectionState",
    "DeviceAppeared",
    "DeviceVanished",
    "DriverMessage",
    "EkosBridge",
    "EkosError",
    "EkosInterfaceError",
    "EkosUnavailableError",
    "Executor",
    "IndiClient",
    "IndiConnectionError",
    "IndiError",
    "IndiEvent",
    "IndiSettings",
    "IndiTimeoutError",
    "Property",
    "PropertyChanged",
    "PropertyKind",
    "PropertyPermission",
    "PropertyState",
    "ServerConnected",
    "ServerDisconnected",
    "SwitchRule",
]
