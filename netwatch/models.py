"""Core domain types shared by the monitoring engine, storage and UI."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class ConnState(str, Enum):
    """Connection / coverage state of a period of time.

    ONLINE, NO_INTERNET and NOT_CONNECTED are network measurements.  UNKNOWN
    means the app was running but had insufficient evidence.  PAUSED and
    NOT_RUNNING are coverage states: nothing was measured.
    """

    ONLINE = "online"
    NO_INTERNET = "no_internet"
    NOT_CONNECTED = "not_connected"
    UNKNOWN = "unknown"
    PAUSED = "paused"
    NOT_RUNNING = "not_running"

    @property
    def label(self) -> str:
        return STATE_LABELS[self]

    @property
    def short_label(self) -> str:
        return STATE_SHORT_LABELS[self]

    @property
    def is_down(self) -> bool:
        return self in DOWN_STATES

    @property
    def is_monitored(self) -> bool:
        return self in MONITORED_STATES


STATE_LABELS = {
    ConnState.ONLINE: "Online",
    ConnState.NO_INTERNET: "No Internet (network connected)",
    ConnState.NOT_CONNECTED: "Not Connected",
    ConnState.UNKNOWN: "Unknown",
    ConnState.PAUSED: "Monitoring Paused",
    ConnState.NOT_RUNNING: "App Not Running",
}

STATE_SHORT_LABELS = {
    ConnState.ONLINE: "Online",
    ConnState.NO_INTERNET: "No Internet",
    ConnState.NOT_CONNECTED: "Not Connected",
    ConnState.UNKNOWN: "Unknown",
    ConnState.PAUSED: "Paused",
    ConnState.NOT_RUNNING: "App Not Running",
}

DOWN_STATES = frozenset({ConnState.NO_INTERNET, ConnState.NOT_CONNECTED})
MONITORED_STATES = frozenset({ConnState.ONLINE, ConnState.NO_INTERNET, ConnState.NOT_CONNECTED})


class Lifecycle(str, Enum):
    """State of the monitoring engine itself (independent of connectivity)."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class ErrorCategory(str, Enum):
    OK = "ok"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    DNS = "dns"
    NETWORK_DOWN = "network_down"
    PERMISSION = "permission"
    MISSING_DEPENDENCY = "missing_dependency"
    HTTP_ERROR = "http_error"
    OTHER = "other"

    @property
    def label(self) -> str:
        return ERROR_LABELS[self]

    @property
    def is_diagnostic(self) -> bool:
        """Errors that say nothing about the network, only about this machine."""
        return self in (ErrorCategory.PERMISSION, ErrorCategory.MISSING_DEPENDENCY)


ERROR_LABELS = {
    ErrorCategory.OK: "OK",
    ErrorCategory.TIMEOUT: "Timed out",
    ErrorCategory.UNREACHABLE: "Destination unreachable",
    ErrorCategory.DNS: "Name resolution failed",
    ErrorCategory.NETWORK_DOWN: "Network unreachable",
    ErrorCategory.PERMISSION: "Permission denied",
    ErrorCategory.MISSING_DEPENDENCY: "Required tool missing",
    ErrorCategory.HTTP_ERROR: "HTTP error",
    ErrorCategory.OTHER: "Other error",
}


@dataclass
class Target:
    id: Optional[int]
    name: str
    address: str
    check_type: str = "icmp"
    enabled: bool = True
    position: int = 0

    @property
    def display(self) -> str:
        return f"{self.name} ({self.address})" if self.name and self.name != self.address else self.address


@dataclass
class CheckResult:
    target_id: Optional[int]
    address: str
    check_type: str
    success: bool
    timestamp: float
    latency_ms: Optional[float] = None
    error: ErrorCategory = ErrorCategory.OK
    detail: str = ""
    packets_sent: int = 1
    packets_received: int = 0


@dataclass
class WifiInfo:
    ssid: Optional[str] = None
    frequency_mhz: Optional[int] = None
    channel: Optional[int] = None
    strength_pct: Optional[int] = None
    signal_dbm: Optional[int] = None
    standard: Optional[str] = None
    security: Optional[str] = None

    @property
    def band(self) -> Optional[str]:
        f = self.frequency_mhz
        if not f:
            return None
        if f < 3000:
            return "2.4 GHz"
        if f < 5925:
            return "5 GHz"
        return "6 GHz"


@dataclass
class LocalNetworkStatus:
    """What the local machine knows about its own network attachment."""

    connected: Optional[bool]  # None = cannot tell
    source: str = "unknown"  # networkmanager | sysfs | unknown
    interface: Optional[str] = None
    kind: Optional[str] = None  # wifi | ethernet | other
    connection_name: Optional[str] = None
    nm_connectivity: Optional[str] = None  # none | portal | limited | full | unknown
    wifi: Optional[WifiInfo] = None
    detail: str = ""

    def describe(self) -> str:
        """Human readable interface description, using only observed fields."""
        if self.kind == "wifi":
            parts: list[str] = []
            w = self.wifi or WifiInfo()
            if w.ssid:
                parts.append(w.ssid)
            if w.channel:
                parts.append(f"Ch {w.channel}")
            if w.band:
                parts.append(w.band)
            if w.standard:
                parts.append(w.standard)
            if w.signal_dbm is not None:
                parts.append(f"{w.signal_dbm} dBm")
            elif w.strength_pct is not None:
                parts.append(f"signal {w.strength_pct}%")
            inner = ", ".join(parts)
            return f"Wi-Fi ({inner})" if inner else "Wi-Fi"
        if self.kind == "ethernet":
            name = self.connection_name or self.interface
            return f"Ethernet ({name})" if name else "Ethernet"
        if self.interface:
            return self.connection_name or self.interface
        return ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: Optional[str]) -> Optional["LocalNetworkStatus"]:
        if not raw:
            return None
        try:
            data = json.loads(raw)
            wifi = data.pop("wifi", None)
            status = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            if wifi:
                status.wifi = WifiInfo(**{k: v for k, v in wifi.items() if k in WifiInfo.__dataclass_fields__})
            return status
        except (ValueError, TypeError):
            return None


@dataclass
class Transition:
    previous: ConnState
    new: ConnState
    effective_at: float  # timestamp of the first check supporting the new state
    detected_at: float  # timestamp when the threshold was met
    reason: str = ""


@dataclass
class EngineSnapshot:
    """Live view of the engine published to the UI after every tick."""

    lifecycle: Lifecycle = Lifecycle.STOPPED
    state: ConnState = ConnState.UNKNOWN
    state_since: Optional[float] = None
    session_id: Optional[int] = None
    session_started: Optional[float] = None
    last_check_at: Optional[float] = None
    last_success_at: Optional[float] = None
    last_latency_ms: Optional[float] = None
    last_results: list = field(default_factory=list)
    local: Optional[LocalNetworkStatus] = None
    outage_id: Optional[int] = None
    pending_state: Optional[ConnState] = None
    message: str = ""
