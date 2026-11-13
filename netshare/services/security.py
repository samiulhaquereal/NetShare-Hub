"""Access control: PIN verification with brute-force throttling, and the
registry of connected / blocked devices."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


class PinGuard:
    """Holds the server PIN and issues/validates the access tokens derived from it.

    Tokens are HMAC(secret, pin): unforgeable without the server secret and
    automatically invalidated when the PIN changes.
    """

    FREE_ATTEMPTS = 5
    BASE_LOCK_SECONDS = 30
    MAX_LOCK_SECONDS = 15 * 60

    def __init__(self, secret: bytes, initial_pin: str = ""):
        self._secret = secret
        self._pin = initial_pin.strip()
        self._attempts: dict[str, _Attempts] = {}
        self._lock = threading.Lock()

    @property
    def pin(self) -> str:
        return self._pin

    @property
    def required(self) -> bool:
        return bool(self._pin)

    def set_pin(self, pin: str) -> None:
        with self._lock:
            self._pin = (pin or "").strip()
            self._attempts.clear()

    def token(self) -> str:
        if not self._pin:
            return ""
        return hmac.new(self._secret, f"pin:{self._pin}".encode(), hashlib.sha256).hexdigest()[:32]

    def is_authorized(self, token: str | None) -> bool:
        if not self._pin:
            return True
        return bool(token) and hmac.compare_digest(token, self.token())

    def verify(self, client: str, pin: str) -> tuple[bool, str, int]:
        """Returns (ok, token-or-error, retry_after_seconds)."""
        if not self._pin:
            return True, "", 0
        now = time.monotonic()
        with self._lock:
            st = self._attempts.setdefault(client, _Attempts())
            if st.locked_until > now:
                wait = int(st.locked_until - now) + 1
                return False, f"Too many attempts. Try again in {wait}s.", wait
            if hmac.compare_digest((pin or "").strip(), self._pin):
                self._attempts.pop(client, None)
                return True, self.token(), 0
            st.failures += 1
            if st.failures >= self.FREE_ATTEMPTS:
                over = st.failures - self.FREE_ATTEMPTS
                lock = min(self.BASE_LOCK_SECONDS * (2 ** over), self.MAX_LOCK_SECONDS)
                st.locked_until = now + lock
                return False, f"Too many attempts. Try again in {lock}s.", lock
            return False, "Invalid Access PIN", 0


@dataclass
class Device:
    ip: str
    user_agent: str
    device_type: str
    first_seen: float
    last_seen: float = field(default=0.0)


class DeviceRegistry:
    ACTIVE_WINDOW = 60  # seconds

    def __init__(self):
        self._devices: dict[str, Device] = {}
        self._blocked: set[str] = set()
        self._lock = threading.Lock()

    def track(self, ip: str, user_agent: str = "") -> None:
        now = time.time()
        with self._lock:
            dev = self._devices.get(ip)
            if dev:
                dev.last_seen = now
                return
            ua = user_agent.lower()
            mobile = any(x in ua for x in ("mobile", "android", "iphone", "ipad"))
            self._devices[ip] = Device(ip, user_agent[:60], "📱 Mobile" if mobile else "💻 Desktop", now, now)

    def is_blocked(self, ip: str) -> bool:
        return ip in self._blocked

    def set_blocked(self, ip: str, blocked: bool) -> None:
        with self._lock:
            (self._blocked.add if blocked else self._blocked.discard)(ip)

    def snapshot(self) -> list[dict]:
        now = time.time()
        with self._lock:
            out = [{"ip": d.ip, "user_agent": d.user_agent, "device_type": d.device_type,
                    "first_seen": d.first_seen, "last_seen": d.last_seen,
                    "is_blocked": d.ip in self._blocked,
                    "is_active": (now - d.last_seen) < self.ACTIVE_WINDOW}
                   for d in self._devices.values()]
        out.sort(key=lambda d: d["last_seen"], reverse=True)
        return out
