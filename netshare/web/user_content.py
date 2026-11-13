"""Serving files that users uploaded, without letting them run code on our origin.

An uploaded ``page.html`` or ``logo.svg`` opened from /chat/download or
/file-preview would otherwise execute its scripts as *this* site — with access to
the PIN token and every API. So:

* types that can run script (HTML, XHTML, XML, JS) are served as ``text/plain``
  (the text preview still works, nothing executes);
* SVG keeps its image type — needed for ``<img>`` previews — but gets a
  ``Content-Security-Policy: sandbox`` so opening it directly runs no script;
* everything gets ``X-Content-Type-Options: nosniff`` so the browser can't
  second-guess the type, and a sandbox CSP except PDFs (Chrome's built-in PDF
  viewer refuses to load inside a sandboxed document).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from flask import Response, send_file

_ACTIVE = frozenset({"text/html", "application/xhtml+xml", "text/xml", "application/xml",
                     "text/javascript", "application/javascript", "application/x-javascript",
                     "application/ecmascript", "text/ecmascript", "application/xslt+xml"})
_ACTIVE_EXT = frozenset({".html", ".htm", ".xhtml", ".shtml", ".xht", ".xml", ".xsl", ".xslt",
                         ".js", ".mjs", ".hta", ".svgz"})
SANDBOX = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"


def safe_mimetype(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if guessed in _ACTIVE or path.suffix.lower() in _ACTIVE_EXT:
        return "text/plain"  # Flask adds "; charset=utf-8"
    return guessed


def send_user_file(path: Path, as_attachment: bool = False, **kwargs) -> Response:
    mimetype = "application/octet-stream" if as_attachment else safe_mimetype(path)
    resp = send_file(path, mimetype=mimetype, as_attachment=as_attachment, conditional=True, **kwargs)
    harden(resp)
    return resp


def harden(resp: Response) -> Response:
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if not (resp.mimetype or "").startswith("application/pdf"):
        resp.headers["Content-Security-Policy"] = SANDBOX
    return resp
