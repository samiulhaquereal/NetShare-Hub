"""Messenger REST API, chat attachments and the /ws/chat socket."""

from __future__ import annotations

import io
import json
import time

from flask import Blueprint, jsonify, request
from PIL import Image, ImageOps

from ...domain.chat import public_id
from ...infrastructure.file_store import InsufficientSpaceError
from ...services.chat_service import ChatConnection
from ..context import device_key
from ..user_content import send_user_file

MAX_AVATAR_BYTES = 8 * 1024 * 1024
AVATAR_SIZE = 256
_WS_RECEIVE_TIMEOUT = 75  # the page pings every 25 s; silence this long = phone gone → offline


def _avatar_jpeg(data: bytes) -> bytes | None:
    """Decode an uploaded picture and re-encode it as a small square JPEG.
    Anything that isn't a real image is rejected; metadata (GPS…) is dropped."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.draft("RGB", (AVATAR_SIZE * 2, AVATAR_SIZE * 2))
            img = ImageOps.exif_transpose(img)
            img = ImageOps.fit(img.convert("RGB"), (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, "JPEG", quality=85, optimize=True)
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def create_blueprint(c, sock) -> Blueprint:
    bp = Blueprint("chat", __name__)
    chat, store = c.chat, c.chat_files

    def me() -> str:
        return device_key(c)

    # ── Profile & users ───────────────────────────────────────────────
    @bp.route("/api/user", methods=["GET", "POST"])
    def api_user():
        if request.method == "POST":
            name = (request.get_json(silent=True) or {}).get("username")
            return jsonify(chat.set_username(me(), str(name or "")))
        return jsonify(chat.profile(me()))

    @bp.route("/api/users")
    def api_users():
        return jsonify(chat.users())

    @bp.route("/api/avatar", methods=["POST"])
    def upload_avatar():
        if (request.content_length or 0) > MAX_AVATAR_BYTES + 64 * 1024:
            return jsonify({"error": "Picture is too large (max 8 MB)"}), 413
        if not chat.profile(me())["username"]:
            return jsonify({"error": "Choose a name first"}), 400
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "No file"}), 400
        jpeg = _avatar_jpeg(file.stream.read(MAX_AVATAR_BYTES + 1)[:MAX_AVATAR_BYTES + 1])
        if jpeg is None:
            return jsonify({"error": "That file isn't a picture (use JPG, PNG, WEBP or GIF)"}), 400
        name = store.save_upload(f"avatar_{public_id(me())}_{int(time.time())}.jpg", io.BytesIO(jpeg))
        url = f"/chat/download/{name}"
        chat.set_avatar(me(), url)
        return jsonify({"avatar": url})

    # ── Groups ────────────────────────────────────────────────────────
    @bp.route("/api/groups", methods=["GET", "POST"])
    def api_groups():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            gid = chat.create_group(me(), str(data.get("name", "")), list(data.get("members") or []))
            return jsonify({"status": "ok", "id": gid})
        return jsonify(chat.groups(me()))

    @bp.route("/api/groups/<group_id>", methods=["PUT"])
    def update_group(group_id):
        data = request.get_json(silent=True) or {}
        if not chat.update_group(me(), group_id, data.get("name"), list(data.get("members") or [])):
            return jsonify({"error": "Unauthorized"}), 403
        return jsonify({"status": "ok"})

    @bp.route("/api/groups/<group_id>/members")
    def group_members(group_id):
        members = chat.group_members(me(), group_id)
        if members is None:
            return jsonify({"error": "Unauthorized"}), 403
        return jsonify(members)

    # ── Conversations ─────────────────────────────────────────────────
    @bp.route("/api/sidebar")
    def api_sidebar():
        return jsonify(chat.sidebar(me(), request.args.get("page", 0, type=int),
                                    request.args.get("limit", 20, type=int)))

    @bp.route("/api/messages")
    def api_messages():
        return jsonify(chat.messages(me(), request.args.get("target", "group"),
                                     request.args.get("before_id", type=int),
                                     request.args.get("limit", 20, type=int),
                                     after_id=request.args.get("after_id", type=int)))

    @bp.route("/api/search")
    def api_search():
        return jsonify(chat.search(me(), request.args.get("q", "")))

    @bp.route("/api/messages/<int:msg_id>/react", methods=["POST", "DELETE"])
    def api_react(msg_id):
        emoji = (request.get_json(silent=True) or {}).get("emoji")
        if not chat.react(me(), msg_id, emoji, remove=request.method == "DELETE"):
            return jsonify({"error": "message not found"}), 404  # also: not yours to see
        return jsonify({"status": "ok"})

    @bp.route("/api/pins", methods=["POST", "DELETE"])
    def api_pins():
        target = (request.get_json(silent=True) or {}).get("target")
        if not target:
            return jsonify({"error": "No target"}), 400
        chat.set_pinned(me(), target, pinned=request.method == "POST")
        return jsonify({"status": "ok"})

    # ── Attachments ───────────────────────────────────────────────────
    @bp.route("/chat/upload", methods=["POST"])
    def chat_upload():
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "No file"}), 400
        try:
            store.check_space(request.content_length)
            name = store.save_upload(file.filename, file.stream)
        except InsufficientSpaceError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 507
        except OSError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500
        return jsonify({"status": "ok", "filename": name, "url": f"/chat/download/{name}",
                        "is_voice": request.form.get("msg_type") == "voice"})

    @bp.route("/chat/download/<path:filename>")
    def chat_download(filename):
        path = store.resolve(filename)
        if not path.is_file():
            return jsonify({"error": "file not found"}), 404
        return send_user_file(path)  # an uploaded .html/.svg must not run script on our origin

    # ── Real-time ─────────────────────────────────────────────────────
    @sock.route("/ws/chat", bp=bp)
    def ws_chat(ws):
        key = me()
        conn = ChatConnection(ws)
        chat.connect(key, conn)
        try:
            while not conn.closed:
                # A phone that vanished (Wi-Fi off, battery) never closes the socket;
                # without a timeout it would show "online" forever.
                raw = ws.receive(timeout=_WS_RECEIVE_TIMEOUT)
                if raw is None:
                    break
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "ping":
                    conn.send('{"type":"pong"}')
                else:
                    chat.handle_event(key, data)
        finally:
            chat.disconnect(key, conn)

    return bp
