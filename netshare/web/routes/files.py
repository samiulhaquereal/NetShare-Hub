"""File sharing: upload, list, preview, download (single / ZIP) and delete."""

from __future__ import annotations

import datetime

from flask import Blueprint, Response, jsonify, render_template, request, send_file

from ...infrastructure.file_store import file_icon, human_size

TOPIC = "files"


def create_blueprint(c, _sock) -> Blueprint:
    bp = Blueprint("files", __name__)
    store = c.files

    @bp.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "No file selected"}), 400
        try:
            name = store.save_upload(file.filename, file.stream)
        except OSError as exc:
            print(f"[Upload Error] {exc}")
            return jsonify({"status": "error", "message": str(exc)}), 500
        c.notifier.bump(TOPIC)
        return jsonify({"status": "ok", "filename": name, "url": f"/download/{name}"})

    @bp.route("/download/<path:filename>")
    def download(filename):
        path = store.resolve(filename)
        if not path.is_file():
            return "File not found", 404
        return send_file(path, as_attachment=True)

    @bp.route("/file-preview/<path:filename>")
    def file_preview(filename):
        path = store.resolve(filename)
        if not path.is_file():
            return "File not found", 404
        if request.args.get("thumb") == "1":
            thumb = store.thumbnail(filename)
            if thumb:
                data, mimetype = thumb
                resp = Response(data, mimetype=mimetype)
                resp.headers["Cache-Control"] = "private, max-age=86400"
                return resp
        return send_file(path, as_attachment=False, conditional=True)

    @bp.route("/download-multiple", methods=["POST"])
    def download_multiple():
        names = (request.get_json(silent=True) or {}).get("files", [])
        if not isinstance(names, list):
            return jsonify({"error": "files must be a list"}), 400
        bundle = store.zip_to_tempfile(str(n) for n in names)
        return send_file(bundle, download_name="files.zip", as_attachment=True, mimetype="application/zip")

    @bp.route("/delete/<path:filename>", methods=["DELETE"])
    def delete_file(filename):
        if not store.delete(filename):
            return jsonify({"error": "file not found"}), 404
        c.notifier.bump(TOPIC)
        return jsonify({"status": "deleted"})

    @bp.route("/files")
    def files_list():
        entries = [{
            "name": e.name,
            "kind": e.kind,
            "icon": file_icon(e.name),
            "ext_label": e.ext.lstrip(".").upper() or "FILE",
            "date": datetime.datetime.fromtimestamp(e.mtime).strftime("%Y-%m-%d %H:%M"),
            "size": human_size(e.size),
        } for e in store.list_entries()]
        return render_template("partials/file_table.html", entries=entries)

    return bp
