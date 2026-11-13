"""Composition root — builds every service once and wires their callbacks.

Nothing else in the code base constructs infrastructure objects; routes and the
UI receive this container, which keeps dependencies explicit and testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .infrastructure.adb import AdbClient, AdbScreenPool, ScrcpyLauncher
from .infrastructure.airplay import AirPlayEngine
from .infrastructure.audio import AudioCapture
from .infrastructure.capture import ScreenCapture
from .infrastructure.chat_repository import ChatRepository
from .infrastructure.cipher import TextObfuscator
from .infrastructure.config_store import JsonConfigStore
from .infrastructure.file_store import FileStore
from .infrastructure.network import MacResolver
from .infrastructure.ngrok_tunnel import NgrokTunnel
from .services.chat_service import ChatHub, ChatService
from .services.clipboard import ClipboardService
from .services.events import ChangeNotifier
from .services.identity import IdentityService
from .services.remote_control import RemoteController
from .services.security import DeviceRegistry, PinGuard
from .services.stream_hub import StreamHub


@dataclass
class Container:
    settings: Settings
    capture: ScreenCapture
    audio: AudioCapture
    stream_hub: StreamHub
    clipboard: ClipboardService
    remote: RemoteController
    notifier: ChangeNotifier
    files: FileStore
    chat_files: FileStore
    chat: ChatService
    pin: PinGuard
    devices: DeviceRegistry
    identity: IdentityService
    tunnel: NgrokTunnel
    adb: AdbClient
    adb_screens: AdbScreenPool
    scrcpy: ScrcpyLauncher
    airplay: AirPlayEngine
    prefs: JsonConfigStore


def build_container(settings: Settings) -> Container:
    s = settings
    capture = ScreenCapture(s.capture_fps, s.capture_quality, s.capture_max_width)
    audio = AudioCapture()
    hub = StreamHub()
    notifier = ChangeNotifier()

    # Capture only works while someone watches; frames/audio fan out via the hub.
    capture.has_consumers = hub.has_clients
    capture.on_frame = hub.publish_frame
    audio.on_chunk = hub.publish_audio

    chat = ChatService(ChatRepository(s.db_path), TextObfuscator(s.chat_cipher_key), ChatHub())
    chat.init()

    adb = AdbClient([s.resource_dir, s.resource_dir / "bin", s.app_dir / "bin"], s.adb_candidates)

    return Container(
        settings=s,
        capture=capture,
        audio=audio,
        stream_hub=hub,
        clipboard=ClipboardService(hub, notifier),
        remote=RemoteController(capture.get_active_rect),
        notifier=notifier,
        files=FileStore(s.upload_dir),
        chat_files=FileStore(s.chat_upload_dir),
        chat=chat,
        pin=PinGuard(s.secret_key, s.initial_pin),
        devices=DeviceRegistry(),
        identity=IdentityService(s.secret_key, MacResolver()),
        tunnel=NgrokTunnel(s.resource_dir, s.http_port),
        adb=adb,
        adb_screens=AdbScreenPool(adb),
        scrcpy=ScrcpyLauncher(s.bin_dir, s.scrcpy_candidates, s.scrcpy_download_url),
        airplay=AirPlayEngine(s.uxplay_path, s.airplay_auto_install),
        prefs=JsonConfigStore(s.config_path),
    )
