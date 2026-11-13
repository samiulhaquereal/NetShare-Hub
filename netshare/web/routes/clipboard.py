"""Clipboard REST endpoint and the Server-Sent Events change feed."""

from __future__ import annotations

import json
import threading
import time

from flask import Blueprint, Response, jsonify, request, stream_with_context

from ...services.clipboard import TOPIC as CLIPBOARD_TOPIC
from ...services.clipboard import ClipboardPeer, ClipboardTooLargeError
from .files import TOPIC as FILES_TOPIC

_KEEPALIVE_SECONDS = 15
_DIR_POLL_SECONDS = 2  # still notice files dropped into the folder by hand
_MIN_EVENT_GAP = 1.0


_WS_RECEIVE_TIMEOUT = 75  # client pings every 25 s; silence this long = dead connection


def create_blueprint(c, sock) -> Blueprint:
    bp = Blueprint("clipboard", __name__)

    @bp.route("/clipboard", methods=["GET", "POST"])
    def clipboard():
        """HTTP API (scripts, older pages). The clipboard page itself uses /ws/clipboard."""
        if request.method == "POST":
            if not request.is_json:  # JSON only: a cross-site <form> must not overwrite it
                return jsonify({"error": "JSON body required"}), 415
            text = (request.get_json(silent=True) or {}).get("text", "")
            try:
                version = c.clipboard.update(str(text))
            except ClipboardTooLargeError as exc:
                return jsonify({"error": str(exc)}), 413
            return jsonify({"status": "ok", "version": version})
        text, version = c.clipboard.snapshot()
        resp = jsonify({"text": text, "version": version})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ── Saved clipboards (grid page) ──────────────────────────────────
    @bp.route("/api/clips")
    def list_clips():
        resp = jsonify({"clips": c.clipboard.list()})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/api/clips", methods=["POST"])
    def create_clip():
        if not request.is_json:  # JSON only: cross-site forms can't create clipboards
            return jsonify({"error": "JSON body required"}), 415
        return jsonify({"id": c.clipboard.create()}), 201

    @bp.route("/api/clips/<clip_id>")
    def get_clip(clip_id):
        try:
            text, version = c.clipboard.snapshot(clip_id)
        except KeyError:
            return jsonify({"error": "clipboard not found"}), 404
        resp = jsonify({"id": clip_id, "text": text, "version": version})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/api/clips/<clip_id>", methods=["DELETE"])
    def delete_clip(clip_id):
        try:
            deleted = c.clipboard.delete(clip_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return (jsonify({"status": "deleted"}), 200) if deleted else (jsonify({"error": "clipboard not found"}), 404)

    @sock.route("/ws/clipboard", bp=bp)
    def ws_clipboard(ws):
        """Live clipboard (``?id=<clipboard>``, default: the shared one).
        In:  {"type":"set","text":…,"seq":n,"base":v} | {"type":"ping"}
        Out: {"type":"state"|"text","text":…,"version":v} | {"type":"ack","seq":n,"version":v[,"text"]}
             | {"type":"error","seq":n,"error":…} | {"type":"pong"} | {"type":"deleted"}"""
        board_id = request.args.get("id") or None
        peer = ClipboardPeer(ws.send)
        if not c.clipboard.add_peer(peer, board_id):
            ws.send('{"type":"deleted"}')  # unknown / deleted clipboard
            return
        sender = threading.Thread(target=peer.run_sender, daemon=True, name="ws-clipboard-sender")
        sender.start()
        try:
            while not peer.closed:
                raw = ws.receive(timeout=_WS_RECEIVE_TIMEOUT)
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "ping":
                    peer.reply('{"type":"pong"}')
                elif msg.get("type") == "set" and isinstance(msg.get("text"), str):
                    seq = msg.get("seq")
                    base = msg.get("base") if isinstance(msg.get("base"), int) else None
                    try:
                        version, text = c.clipboard.edit(msg["text"], origin=peer, base=base, board_id=board_id)
                        ack = {"type": "ack", "seq": seq, "version": version}
                        if text != msg["text"]:  # merged with someone else's edit: sender needs the result
                            ack["text"] = text
                        peer.reply(json.dumps(ack, ensure_ascii=False))
                    except ClipboardTooLargeError as exc:
                        peer.reply(json.dumps({"type": "error", "seq": seq, "error": str(exc)}))
                    except KeyError:  # deleted meanwhile
                        peer.reply('{"type":"deleted"}')
        finally:
            c.clipboard.remove_peer(peer, board_id)

    @bp.route("/events")
    def events():
        """Pushes "update" (file list changed) and "clipboard" events.

        Blocks on the change notifier instead of listing the upload folder every
        second for every open tab."""
        def stream():
            seen = c.notifier.snapshot()
            files_ver = c.files.version
            idle = 0.0
            yield "data: update\n\n"  # initial sync, like the old implementation
            if c.clipboard.version:
                yield "data: clipboard\n\n"
            while True:
                current = c.notifier.wait(seen, timeout=_DIR_POLL_SECONDS)
                c.files.reconcile()  # rate-limited safety net for hand-copied files
                ver = c.files.version  # also notices files dropped into the folder by hand
                files_changed = current.get(FILES_TOPIC) != seen.get(FILES_TOPIC) or ver != files_ver
                clip_changed = current.get(CLIPBOARD_TOPIC) != seen.get(CLIPBOARD_TOPIC)
                seen, files_ver = current, ver
                if files_changed:
                    yield "data: update\n\n"
                if clip_changed:
                    yield "data: clipboard\n\n"
                if files_changed or clip_changed:
                    idle = 0.0
                    # Coalesce: during a mass upload the folder changes hundreds of times
                    # a second; each tab needs at most one nudge per interval.
                    time.sleep(_MIN_EVENT_GAP)
                else:
                    idle += _DIR_POLL_SECONDS
                    if idle >= _KEEPALIVE_SECONDS:
                        idle = 0.0
                        yield ": keepalive\n\n"  # detects closed tabs so the thread can exit

        resp = Response(stream_with_context(stream()), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    return bp
