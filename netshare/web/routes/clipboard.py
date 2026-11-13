"""Clipboard REST endpoint and the Server-Sent Events change feed."""

from __future__ import annotations

import time

from flask import Blueprint, Response, jsonify, request, stream_with_context

from ...services.clipboard import TOPIC as CLIPBOARD_TOPIC
from .files import TOPIC as FILES_TOPIC

_KEEPALIVE_SECONDS = 15
_DIR_POLL_SECONDS = 2  # still notice files dropped into the folder by hand
_MIN_EVENT_GAP = 1.0


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
