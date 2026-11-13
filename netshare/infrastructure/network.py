"""Network helpers: local IP discovery, ARP lookup and QR code rendering."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time

import qrcode
from PIL import Image

from .winproc import CREATE_NO_WINDOW

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})")
PROC_ARP = "/proc/net/arp"


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def parse_proc_arp(table: str, ip: str) -> str | None:
    """MAC for `ip` from /proc/net/arp content (columns: IP, HW type, Flags, HW address, …)."""
    for line in table.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 4 and cols[0] == ip and cols[3] != "00:00:00:00:00:00":
            return cols[3].upper()
    return None


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no packet is sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def is_port_free(port: int) -> bool:
    """True only if nobody else listens on the port.

    On Windows a plain bind() can succeed even when another program (e.g. a
    Docker-published container) already listens there, because SO_REUSEADDR
    lets sockets share a port — requests then go to whichever one Windows picks.
    SO_EXCLUSIVEADDRUSE makes the test bind fail in that case; the connect
    probes also catch listeners on loopback / IPv6 only.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                if probe.connect_ex((host, port)) == 0:
                    return False  # something answered
        except OSError:
            pass  # e.g. IPv6 disabled
    return True


def find_free_port(preferred: int, attempts: int = 20) -> int | None:
    """`preferred` if free, else the next free port above it."""
    for port in range(preferred, preferred + attempts):
        if is_port_free(port):
            return port
    return None


class MacResolver:
    """Resolves LAN IP -> MAC via `arp -a`, cached because spawning arp costs
    50–200 ms and used to run on every single chat request."""

    def __init__(self, ttl: float = 600.0):
        self._ttl = ttl
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def lookup(self, ip: str) -> str:
        """Return the MAC (upper-case, colon separated) or the IP itself as fallback."""
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(ip)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        value = self._query(ip)
        with self._lock:
            self._cache[ip] = (now, value)
        return value

    @staticmethod
    def _query(ip: str) -> str:
        if os.path.exists(PROC_ARP):  # Linux: kernel table, no subprocess / net-tools needed
            return parse_proc_arp(_read_text(PROC_ARP), ip) or ip
        try:
            out = subprocess.run(["arp", "-a", ip], capture_output=True, timeout=3,
                                 creationflags=CREATE_NO_WINDOW).stdout.decode("utf-8", "ignore")
            match = _MAC_RE.search(out)
            if match:
                return match.group(0).replace("-", ":").upper()
        except (OSError, subprocess.SubprocessError):
            pass
        return ip


def make_qr_image(url: str, size: int = 200) -> Image.Image:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L,
                       box_size=6, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#7C3AED", back_color="white")
    return img.resize((size, size), Image.NEAREST)
