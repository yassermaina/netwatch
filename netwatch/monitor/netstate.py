"""Local network attachment probe.

Prefers NetworkManager over the system D-Bus (synchronous Properties.GetAll
calls, safe from a worker thread).  Falls back to /sys/class/net and the
kernel routing table when NetworkManager is unavailable.  Wi-Fi details are
only reported when a system interface actually provides them.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from netwatch.models import LocalNetworkStatus, WifiInfo
from netwatch.monitor.process import ProcessRunner

log = logging.getLogger(__name__)

NM_BUS = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_STATE_CONNECTED = {50, 60, 70}  # local, site, global
NM_STATE_DISCONNECTED = {10, 20, 30, 40}  # asleep, disconnected, disconnecting, connecting
NM_CONNECTIVITY = {0: "unknown", 1: "none", 2: "portal", 3: "limited", 4: "full"}


def frequency_to_channel(freq: int) -> Optional[int]:
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq == 2484:
        return 14
    if 5150 <= freq <= 5925:
        return (freq - 5000) // 5
    if 5955 <= freq <= 7115:
        return (freq - 5950) // 5
    return None


def parse_iw_link(output: str) -> dict:
    """Parse ``iw dev <if> link`` output for signal and PHY standard."""
    info: dict = {}
    ssid = re.search(r"^\s*SSID:\s*(.+?)\s*$", output, re.M)
    if ssid:
        info["ssid"] = ssid.group(1)
    m = re.search(r"signal:\s*(-?\d+)\s*dBm", output)
    if m:
        info["signal_dbm"] = int(m.group(1))
    if re.search(r"\bEHT-MCS", output):
        info["standard"] = "802.11be"
    elif re.search(r"\bHE-MCS", output):
        info["standard"] = "802.11ax"
    elif re.search(r"\bVHT-MCS", output):
        info["standard"] = "802.11ac"
    elif re.search(r"\bMCS \d+", output):
        info["standard"] = "802.11n"
    return info


class LocalNetworkProbe:
    DETAIL_TTL = 30.0

    def __init__(self, runner: Optional[ProcessRunner] = None, sys_root: str = "/"):
        self.runner = runner or ProcessRunner()
        self.sys_root = Path(sys_root)
        self._bus = None
        self._bus_failed = False
        self._iw = shutil.which("iw")
        self._detail_cache: tuple[float, Optional[str], dict] = (0.0, None, {})
        self.last_error = ""

    # -- public ------------------------------------------------------------
    def probe(self) -> LocalNetworkStatus:
        status = self._probe_nm()
        if status is None:
            status = self._probe_sysfs()
        return status

    def describe_backend(self) -> str:
        if self._nm_bus() is not None and self._nm_running():
            return "NetworkManager (D-Bus)"
        return "Kernel interfaces (/sys/class/net, /proc/net/route)"

    # -- NetworkManager ---------------------------------------------------------
    def _nm_bus(self):
        if self._bus is not None or self._bus_failed:
            return self._bus
        try:
            from gi.repository import Gio  # noqa: PLC0415

            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except Exception as exc:  # no gi, no system bus
            self.last_error = f"system bus unavailable: {exc}"
            self._bus_failed = True
        return self._bus

    def _get_all(self, path: str, iface: str) -> Optional[dict]:
        bus = self._nm_bus()
        if bus is None:
            return None
        try:
            from gi.repository import Gio, GLib  # noqa: PLC0415

            reply = bus.call_sync(
                NM_BUS,
                path,
                "org.freedesktop.DBus.Properties",
                "GetAll",
                GLib.Variant("(s)", (iface,)),
                GLib.VariantType("(a{sv})"),
                Gio.DBusCallFlags.NO_AUTO_START,
                1500,
                None,
            )
            return reply.unpack()[0]
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def _nm_running(self) -> bool:
        return self._get_all(NM_PATH, "org.freedesktop.NetworkManager") is not None

    def _probe_nm(self) -> Optional[LocalNetworkStatus]:
        props = self._get_all(NM_PATH, "org.freedesktop.NetworkManager")
        if props is None:
            return None
        state = int(props.get("State", 0))
        connectivity = NM_CONNECTIVITY.get(int(props.get("Connectivity", 0)), "unknown")
        if state in NM_STATE_CONNECTED:
            connected: Optional[bool] = True
        elif state in NM_STATE_DISCONNECTED:
            connected = False
        else:
            connected = None
        status = LocalNetworkStatus(connected=connected, source="networkmanager", nm_connectivity=connectivity)
        primary = props.get("PrimaryConnection") or "/"
        if connected and primary != "/":
            self._fill_nm_connection(status, primary)
        status.detail = f"NetworkManager state {state}, connectivity {connectivity}"
        return status

    def _fill_nm_connection(self, status: LocalNetworkStatus, active_path: str) -> None:
        active = self._get_all(active_path, "org.freedesktop.NetworkManager.Connection.Active")
        if not active:
            return
        status.connection_name = active.get("Id")
        ctype = active.get("Type", "")
        devices = active.get("Devices") or []
        if devices:
            dev = self._get_all(devices[0], "org.freedesktop.NetworkManager.Device") or {}
            status.interface = dev.get("Interface") or dev.get("IpInterface")
        if ctype == "802-11-wireless":
            status.kind = "wifi"
            status.wifi = self._nm_wifi(devices[0] if devices else None, status.interface)
        elif ctype == "802-3-ethernet":
            status.kind = "ethernet"
        else:
            status.kind = "other"

    def _nm_wifi(self, device_path: Optional[str], ifname: Optional[str]) -> WifiInfo:
        info = WifiInfo()
        if device_path:
            wdev = self._get_all(device_path, "org.freedesktop.NetworkManager.Device.Wireless") or {}
            ap_path = wdev.get("ActiveAccessPoint") or "/"
            if ap_path != "/":
                ap = self._get_all(ap_path, "org.freedesktop.NetworkManager.AccessPoint") or {}
                ssid = ap.get("Ssid")
                if ssid:
                    info.ssid = bytes(ssid).decode("utf-8", "replace")
                freq = ap.get("Frequency")
                if freq:
                    info.frequency_mhz = int(freq)
                    info.channel = frequency_to_channel(int(freq))
                if ap.get("Strength") is not None:
                    info.strength_pct = int(ap["Strength"])
                rsn, wpa = int(ap.get("RsnFlags", 0)), int(ap.get("WpaFlags", 0))
                if rsn & 0x400:  # NM_802_11_AP_SEC_KEY_MGMT_SAE
                    info.security = "WPA3"
                elif rsn:
                    info.security = "WPA2"
                elif wpa:
                    info.security = "WPA"
                elif int(ap.get("Flags", 0)) & 0x1:
                    info.security = "WEP"
                else:
                    info.security = "Open"
        self._fill_iw(info, ifname)
        return info

    def _fill_iw(self, info: WifiInfo, ifname: Optional[str]) -> None:
        if not self._iw or not ifname:
            return
        now = time.monotonic()
        ts, cached_if, data = self._detail_cache
        if cached_if != ifname or now - ts > self.DETAIL_TTL:
            try:
                res = self.runner.run([self._iw, "dev", ifname, "link"], timeout=2)
                data = parse_iw_link(res.stdout) if res.returncode == 0 else {}
            except OSError:
                data = {}
            self._detail_cache = (now, ifname, data)
        info.ssid = info.ssid or data.get("ssid")  # NetworkManager's value wins when present
        info.signal_dbm = data.get("signal_dbm", info.signal_dbm)
        info.standard = data.get("standard", info.standard)

    # -- sysfs fallback -----------------------------------------------------------
    def _probe_sysfs(self) -> LocalNetworkStatus:
        net = self.sys_root / "sys/class/net"
        route_file = self.sys_root / "proc/net/route"
        route6_file = self.sys_root / "proc/net/ipv6_route"
        try:
            ifaces = [p.name for p in net.iterdir() if p.name != "lo"]
        except OSError:
            return LocalNetworkStatus(connected=None, source="unknown", detail="cannot read /sys/class/net")
        up = [
            i for i in ifaces if _read(net / i / "operstate") in ("up", "unknown") and _read(net / i / "carrier") == "1"
        ]
        default_ifaces: list[str] = []
        try:
            for line in route_file.read_text().splitlines()[1:]:
                cols = line.split()
                if len(cols) > 3 and cols[1] == "00000000" and int(cols[3], 16) & 0x1:
                    default_ifaces.append(cols[0])
        except (OSError, ValueError):
            pass
        try:
            for line in route6_file.read_text().splitlines():
                cols = line.split()
                if len(cols) >= 10 and cols[0] == "0" * 32 and cols[1] == "00" and cols[9] != "lo":
                    default_ifaces.append(cols[9])
        except OSError:
            pass
        usable = [i for i in default_ifaces if i in up]
        if usable:
            iface = usable[0]
            kind = (
                "wifi"
                if (net / iface / "wireless").exists() or (net / iface / "phy80211").exists()
                else (
                    "ethernet"
                    if _read(net / iface / "type") == "1" and not iface.startswith(("tun", "wg", "veth"))
                    else "other"
                )
            )
            status = LocalNetworkStatus(
                connected=True, source="sysfs", interface=iface, kind=kind, detail=f"default route via {iface}"
            )
            if kind == "wifi":
                status.wifi = WifiInfo()
                self._fill_iw(status.wifi, iface)
            return status
        if not up:
            return LocalNetworkStatus(connected=False, source="sysfs", detail="no network interface is up")
        if not default_ifaces:
            return LocalNetworkStatus(connected=False, source="sysfs", detail="no default route")
        return LocalNetworkStatus(
            connected=None, source="sysfs", detail="default route on an interface without carrier"
        )


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def is_networkmanager_present() -> bool:
    return os.path.exists("/run/NetworkManager") or os.path.exists("/var/run/NetworkManager")
