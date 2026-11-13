"""Messenger REST API, chat attachments and the /ws/chat socket."""

from __future__ import annotations

import json
import time

from flask import Blueprint, jsonify, request, send_file

from ...domain.chat import public_id
from ...services.chat_service import ChatConnection
from ..context import device_key

_AVATAR_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})


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
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "No file"}), 400
        name = f"avatar_{public_id(me())}_{int(time.time())}.jpg"
        store.save_upload(name, file.stream)
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
                                     request.args.get("limit", 20, type=int)))

    @bp.route("/api/search")
    def api_search():
        return jsonify(chat.search(me(), request.args.get("q", "")))

    @bp.route("/api/messages/<int:msg_id>/react", methods=["POST", "DELETE"])
    def api_react(msg_id):
        emoji = (request.get_json(silent=True) or {}).get("emoji")
        chat.react(me(), msg_id, emoji, remove=request.method == "DELETE")
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
            name = store.save_upload(file.filename, file.stream)
        except OSError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500
        return jsonify({"status": "ok", "filename": name, "url": f"/chat/download/{name}",
                        "is_voice": request.form.get("msg_type") == "voice"})

    @bp.route("/chat/download/<path:filename>")
    def chat_download(filename):
        path = store.resolve(filename)
        if not path.is_file():
            return jsonify({"error": "file not found"}), 404
        return send_file(path, conditional=True)

    # ── Real-time ─────────────────────────────────────────────────────
    @sock.route("/ws/chat", bp=bp)
    def ws_chat(ws):
        key = me()
        conn = ChatConnection(ws)
        chat.connect(key, conn)
        try:
            while True:
                raw = ws.receive()
                if raw is None:
                    break
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(data, dict):
                    chat.handle_event(key, data)
        finally:
            chat.disconnect(key, conn)

    return bp
