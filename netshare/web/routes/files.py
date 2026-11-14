"""File sharing: upload, list, preview, download (single / streamed ZIP) and delete."""

from __future__ import annotations

import gzip
import time
from urllib.parse import quote, unquote

from flask import Blueprint, Response, jsonify, request, stream_with_context
from werkzeug.exceptions import ClientDisconnected

from ..user_content import send_user_file
from ...infrastructure.file_store import (FolderExistsError, IncompleteUploadError, InsufficientSpaceError,
                                          InvalidFolderNameError, OffsetMismatchError, UnsafePathError,
                                          UploadBusyError)

TOPIC = "files"
MAX_BATCH = 5000


def create_blueprint(c, _sock) -> Blueprint:
    bp = Blueprint("files", __name__)
    store = c.files

    def _stored(name: str):
        c.notifier.bump(TOPIC)
        return jsonify({"status": "ok", "filename": name, "url": f"/download/{quote(name)}"})

    def _upload_error(exc: Exception):
        if isinstance(exc, FileNotFoundError):  # the target folder was renamed or removed
            return jsonify({"status": "error", "error": "Folder not found on the host"}), 404
        if isinstance(exc, InsufficientSpaceError):
            return jsonify({"status": "error", "error": str(exc)}), 507
        if isinstance(exc, (IncompleteUploadError, ClientDisconnected)):
            return jsonify({"status": "error", "error": "Upload interrupted"}), 400
        print(f"[Upload Error] {exc}")
        return jsonify({"status": "error", "error": str(exc)}), 500

    @bp.route("/upload-stream", methods=["PUT"])
    def upload_stream():
        """Raw request body → disk in one pass (multipart would be spooled to a temp
        file first and then copied: twice the disk I/O for big files)."""
        name = unquote(request.headers.get("X-File-Name", ""))
        folder = unquote(request.headers.get("X-Folder", ""))  # sub-folder being viewed
        size = request.content_length
        if not name:
            return jsonify({"status": "error", "error": "Missing X-File-Name header"}), 400
        if size is None:
            return jsonify({"status": "error", "error": "Content-Length required"}), 411
        upload_id = request.headers.get("X-Upload-Id")
        if upload_id:
            return _resumable(upload_id, name, folder, size)
        try:
            return _stored(store.save_upload(name, request.stream, expected_size=size, folder=folder))
        except (OSError, ClientDisconnected) as exc:
            return _upload_error(exc)

    def _resumable(upload_id: str, name: str, folder: str, length: int):
        """Body = bytes [offset, offset+length) of a file of X-File-Size bytes."""
        offset = request.headers.get("X-Upload-Offset", "0")
        total = request.headers.get("X-File-Size", "")
        if not (offset.isdigit() and total.isdigit()):
            return jsonify({"status": "error", "error": "X-Upload-Offset / X-File-Size required"}), 400
        try:
            stored = store.save_resumable(upload_id, name, request.stream, int(offset), length, int(total), folder)
        except OffsetMismatchError as exc:
            return jsonify({"status": "resume", "offset": exc.have}), 409
        except UploadBusyError:
            return jsonify({"status": "busy"}), 409
        except IncompleteUploadError:
            return jsonify({"status": "error", "error": "Upload interrupted"}), 400
        except ValueError as exc:
            if isinstance(exc, UnsafePathError):
                raise
            return jsonify({"status": "error", "error": str(exc)}), 400
        except (OSError, ClientDisconnected) as exc:
            return _upload_error(exc)
        if stored is None:
            return jsonify({"status": "partial", "offset": int(offset) + length}), 202
        return _stored(stored)

    @bp.route("/upload-stream/<upload_id>")
    def upload_status(upload_id):
        """How much of a resumable upload already arrived (asked after a dropout)."""
        resp = jsonify(store.upload_status(upload_id))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/upload", methods=["POST"])
    def upload():
        """Multipart upload (kept for scripts / older clients)."""
        try:
            store.check_space(request.content_length)
            file = request.files.get("file")
            if not file or not file.filename:
                return jsonify({"status": "error", "error": "No file selected"}), 400
            return _stored(store.save_upload(file.filename, file.stream, folder=request.form.get("folder", "")))
        except (OSError, ClientDisconnected) as exc:
            return _upload_error(exc)

    @bp.route("/api/files")
    def files_api():
        """Full list, or with ``?since=<version>`` only what changed (``delta``).

        The full list is serialised and gzipped once per change and shared by all
        clients; free disk space travels in the X-Disk-Free header. ``?dir=Photos/2024``
        lists a sub-folder instead (always the full list, with an ETag)."""
        folder = request.args.get("dir", "").strip("/")
        if folder:
            return _folder_listing(folder)
        since = request.args.get("since", type=int)
        if since is not None:
            delta = store.changes_since(since)
            if delta is not None:
                version, added, removed = delta
                resp = jsonify({"version": version, "delta": True, "added": added, "removed": removed})
                resp.headers["X-Disk-Free"] = str(store.free_space())
                resp.headers["Cache-Control"] = "no-store"
                return resp
        version, body, gz = store.listing()
        etag = f'"files-{version}"'
        if etag in request.headers.get("If-None-Match", ""):
            resp = Response(status=304)
        elif "gzip" in request.headers.get("Accept-Encoding", "").lower():
            resp = Response(gz, mimetype="application/json")
            resp.headers["Content-Encoding"] = "gzip"
            resp.vary.add("Accept-Encoding")
        else:
            resp = Response(body, mimetype="application/json")
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Disk-Free"] = str(store.free_space())
        if version != store.version:
            resp.headers["X-List-Behind"] = "1"  # client should fetch the delta right away
        return resp

    def _folder_listing(folder: str):
        try:
            body, tag = store.folder_listing(folder)
        except (FileNotFoundError, NotADirectoryError):
            return jsonify({"error": "folder not found"}), 404
        etag = f'"dir-{tag}"'
        if etag in request.headers.get("If-None-Match", ""):
            resp = Response(status=304)
        elif len(body) > 1400 and "gzip" in request.headers.get("Accept-Encoding", "").lower():
            resp = Response(gzip.compress(body, compresslevel=5), mimetype="application/json")
            resp.headers["Content-Encoding"] = "gzip"
            resp.vary.add("Accept-Encoding")
        else:
            resp = Response(body, mimetype="application/json")
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Disk-Free"] = str(store.free_space())
        return resp

    @bp.route("/download/<path:filename>")
    def download(filename):
        path = store.resolve(filename)
        if not path.is_file():
            return "File not found", 404
        return send_user_file(path, as_attachment=True)

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
                # ?v=<mtime> makes the URL change with the file, so it can be cached for good
                resp.headers["Cache-Control"] = ("private, max-age=31536000, immutable" if request.args.get("v")
                                                 else "private, max-age=86400")
                return resp
        return send_user_file(path)  # HTML/SVG can't run script on our origin

    def _names() -> list[str]:
        if request.is_json:
            names = (request.get_json(silent=True) or {}).get("files", [])
        else:
            names = request.form.getlist("files")
        if not isinstance(names, list):
            return []
        return [str(n) for n in names[:MAX_BATCH]]

    @bp.route("/download-multiple", methods=["POST"])
    def download_multiple():
        """Streamed ZIP — works as a normal form POST, so the browser's own download
        manager shows progress and nothing is buffered in page memory."""
        names = _names()
        if not names:
            return jsonify({"error": "files must be a non-empty list"}), 400
        stamp = time.strftime("%Y%m%d-%H%M")
        zip_name = f"OnLAN-{stamp}.zip"
        if len(names) == 1 and not store.exists(names[0]):  # one folder → "<folder>.zip"
            zip_name = names[0].rstrip("/").rsplit("/", 1)[-1] + ".zip"
        resp = Response(stream_with_context(store.iter_zip(names)), mimetype="application/zip")
        resp.headers["Content-Disposition"] = (f'attachment; filename="OnLAN-{stamp}.zip"; '
                                               f"filename*=UTF-8''{quote(zip_name)}")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/api/folders", methods=["POST"])
    def create_folder():
        if not request.is_json:  # JSON only: a cross-site <form> must not create folders
            return jsonify({"error": "JSON body required"}), 415
        data = request.get_json(silent=True) or {}
        try:
            path = store.create_folder(str(data.get("parent", "")), str(data.get("name", "")))
        except FolderExistsError as exc:
            return jsonify({"error": str(exc)}), 409
        except InvalidFolderNameError as exc:
            return jsonify({"error": str(exc)}), 400
        except (FileNotFoundError, NotADirectoryError):
            return jsonify({"error": "Folder not found on the host"}), 404
        c.notifier.bump(TOPIC)
        return jsonify({"status": "ok", "path": path}), 201

    @bp.route("/delete/<path:filename>", methods=["DELETE"])
    def delete_file(filename):
        if not store.delete(filename):
            return jsonify({"error": "file not found"}), 404
        c.notifier.bump(TOPIC)
        return jsonify({"status": "deleted"})

    @bp.route("/delete-multiple", methods=["POST"])
    def delete_multiple():
        if not request.is_json:  # JSON only: a cross-site <form> must not be able to delete files
            return jsonify({"error": "JSON body required"}), 415
        names = _names()
        if not names:
            return jsonify({"error": "files must be a non-empty list"}), 400
        deleted, failed = store.delete_many(names)
        if deleted:
            c.notifier.bump(TOPIC)
        return jsonify({"status": "ok", "deleted": len(deleted), "failed": failed})

    return bp
