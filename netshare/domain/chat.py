"""Chat identity rules.

Internally every participant is identified by a *device key* (historically the
device MAC address). The browser only ever sees a stable *public id* derived
from it, so raw MAC/IP addresses never leave the server.
"""

from __future__ import annotations

import hashlib

GLOBAL_TARGET = "group"
CUSTOM_GROUP_PREFIX = "cg_"
PUBLIC_USER_PREFIX = "u_"
DELETED_TEXT = "🚫 _This message was deleted_"
MESSAGE_TYPES = frozenset({"text", "file"})


def is_group_target(target: str | None) -> bool:
    return bool(target) and (target == GLOBAL_TARGET or target.startswith(CUSTOM_GROUP_PREFIX))


def public_id(device_key: str | None) -> str | None:
    """Stable, non-reversible id shown to clients (u_xxxxxxxx)."""
    if not device_key or is_group_target(device_key):
        return device_key
    return PUBLIC_USER_PREFIX + hashlib.md5(device_key.encode()).hexdigest()[:8]
