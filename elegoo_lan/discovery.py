"""UDP discovery (spec §1). Linux replacement for QUdpSocket/QNetworkInterface."""
from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import time
import warnings

from .protocol import (DISCOVERY_PORT, DISCOVERY_PROBE, IncompatiblePrinterError, Printer,
                       compatibility_warning, parse_discovery_reply)


def broadcast_for(ip: str, netmask: str) -> str:
    """broadcast = ip | ~netmask, as the app computes it."""
    i = int(ipaddress.IPv4Address(ip))
    m = int(ipaddress.IPv4Address(netmask))
    return str(ipaddress.IPv4Address((i | (~m & 0xFFFFFFFF)) & 0xFFFFFFFF))


def local_ipv4_broadcasts() -> list[str]:
    """Broadcast address of each non-loopback IPv4 interface (via iproute2 JSON)."""
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True,
                             check=True, timeout=5).stdout
        ifaces = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    result = []
    for itf in ifaces:
        if "LOOPBACK" in itf.get("flags", []):
            continue
        for a in itf.get("addr_info", []):
            if a.get("family") != "inet":
                continue
            bcast = a.get("broadcast")
            if not bcast:
                net = ipaddress.IPv4Network(f"{a['local']}/{a['prefixlen']}", strict=False)
                bcast = str(net.broadcast_address)
            result.append(bcast)
    return result


def discover(timeout: float = 2.0, targets: list[str] | None = None,
             port: int = DISCOVERY_PORT) -> list[Printer]:
    """Send the probe to every target and collect replies until `timeout`."""
    targets = targets if targets is not None else (local_ipv4_broadcasts() + ["255.255.255.255"])
    found: dict[str, Printer] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        for t in dict.fromkeys(targets):
            try:
                s.sendto(DISCOVERY_PROBE, (t, port))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            s.settimeout(left)
            try:
                data, (src, _) = s.recvfrom(65535)
            except socket.timeout:
                break
            p = parse_discovery_reply(data, src)
            if p and (p.mainboard_id or src) not in found:
                found[p.mainboard_id or src] = p
                if (msg := compatibility_warning(p)):
                    warnings.warn(msg, UserWarning, stacklevel=2)
    return list(found.values())


def probe(ip: str, timeout: float = 2.0, port: int = DISCOVERY_PORT) -> Printer | None:
    """Unicast discovery of a single printer."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        found = discover(timeout, targets=[ip], port=port)
    return next((p for p in found if p.ip == ip or p.source_ip == ip), found[0] if found else None)


def ensure_supported(ip: str, timeout: float = 2.0, port: int = DISCOVERY_PORT) -> Printer:
    """Probe `ip` and raise IncompatiblePrinterError for pre-V3 (MQTT-era) printers."""
    p = probe(ip, timeout, port)
    if p is None:
        raise IncompatiblePrinterError(f"No SDCP discovery reply from {ip}; cannot verify compatibility.")
    if (msg := compatibility_warning(p)):
        raise IncompatiblePrinterError(msg)
    return p
