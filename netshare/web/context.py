"""Request-scoped helpers shared by every blueprint."""

from __future__ import annotations

from functools import wraps

from flask import g, jsonify, request

from ..infrastructure.network import LOOPBACK, get_local_ip
from ..services.identity import COOKIE_NAME, ClientInfo

PIN_HEADER = "X-Server-PIN-Token"
PIN_COOKIE = "pin_token"


def client_info() -> ClientInfo:
    """Real client address. ngrok's agent connects from loopback and appends the
    visitor IP to X-Forwarded-For; the header is only trusted in that case so a
    LAN client can't spoof its address to dodge an IP block."""
    cached = getattr(g, "_client_info", None)
    if cached:
        return cached
    peer = request.remote_addr or ""
    loopback = peer in LOOPBACK
    forwarded = request.headers.get("X-Forwarded-For", "")
    if loopback and forwarded:
        info = ClientInfo(ip=forwarded.split(",")[-1].strip() or peer, is_proxied=True, is_loopback=True)
    else:
        info = ClientInfo(ip=peer, is_proxied=False, is_loopback=loopback)
    g._client_info = info
    return info


def is_host() -> bool:
    """Request made on the host machine itself (not through the tunnel)."""
    info = client_info()
    return info.is_loopback and not info.is_proxied


def host_only(fn):
    """Route decorator: 403 unless the request comes from the host PC itself."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_host():
            return jsonify({"error": "This action is only available on the host PC."}), 403
        return fn(*args, **kwargs)
    return wrapper


def pin_token() -> str | None:
    return request.headers.get(PIN_HEADER) or request.args.get(PIN_COOKIE) or request.cookies.get(PIN_COOKIE)


def device_key(container) -> str:
    """Chat identity of the caller; a fresh cookie is set by an after_request hook."""
    ident = getattr(g, "_identity", None)
    if ident is None:
        ident = container.identity.resolve(request.cookies.get(COOKIE_NAME), client_info())
        g._identity = ident
    return ident.key


def base_urls(container) -> tuple[str, str]:
    """(shareable http url, websocket url for *this* client).

    Direct LAN/localhost visitors stream straight from the host even when the
    tunnel is up; only tunnelled visitors use the public wss:// endpoint.
    """
    port = container.settings.http_port
    tunnel = container.tunnel
    http_url = tunnel.public_url or f"http://{get_local_ip()}:{port}"
    if tunnel.public_url and client_info().is_proxied:
        ws_url = f"{tunnel.public_ws_url}/ws/stream"
    else:
        scheme = "wss" if request.is_secure else "ws"
        ws_url = f"{scheme}://{request.host}/ws/stream"
    return http_url, ws_url
