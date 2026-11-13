"""Clipboard REST endpoint and the Server-Sent Events change feed."""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request, stream_with_context

from ...services.clipboard import TOPIC as CLIPBOARD_TOPIC
from .files import TOPIC as FILES_TOPIC

_KEEPALIVE_SECONDS = 15
_DIR_POLL_SECONDS = 2  # still notice files dropped into the folder by hand


def create_blueprint(c, _sock) -> Blueprint:
    bp = Blueprint("clipboard", __name__)

    @bp.route("/clipboard", methods=["GET", "POST"])
    def clipboard():
        if request.method == "POST":
            text = (request.get_json(silent=True) or {}).get("text", "")
            c.clipboard.update(str(text))
            return jsonify({"status": "ok"})
        return jsonify({"text": c.clipboard.text, "version": c.clipboard.version})

    @bp.route("/events")
    def events():
        """Pushes "update" (file list changed) and "clipboard" events.

        Blocks on the change notifier instead of listing the upload folder every
        second for every open tab."""
        def stream():
            seen = c.notifier.snapshot()
            dir_sig = c.files.dir_signature()
            idle = 0.0
            yield "data: update\n\n"  # initial sync, like the old implementation
            if c.clipboard.version:
                yield "data: clipboard\n\n"
            while True:
                current = c.notifier.wait(seen, timeout=_DIR_POLL_SECONDS)
                sig = c.files.dir_signature()
                files_changed = current.get(FILES_TOPIC) != seen.get(FILES_TOPIC) or sig != dir_sig
                clip_changed = current.get(CLIPBOARD_TOPIC) != seen.get(CLIPBOARD_TOPIC)
                seen, dir_sig = current, sig
                if files_changed:
                    yield "data: update\n\n"
                if clip_changed:
                    yield "data: clipboard\n\n"
                if files_changed or clip_changed:
                    idle = 0.0
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
