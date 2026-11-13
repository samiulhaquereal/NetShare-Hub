import gzip
import io
import re

from netshare.web.assets import LONG_CACHE
from tests.helpers import AppTestCase

GZ = {"Accept-Encoding": "gzip, deflate, br"}


class AssetTests(AppTestCase):
    def _asset_url(self, page: str, name: str) -> str:
        html = self.host().get(page).get_data(as_text=True)
        m = re.search(rf'"(/static/{re.escape(name)}\?v=[0-9a-f]+)"', html)
        self.assertIsNotNone(m, f"{name} not referenced with a version on {page}")
        return m.group(1)

    def test_pages_use_versioned_assets_and_no_google_fonts(self):
        for page in ("/", "/screen", "/files-page", "/clipboard-page", "/adb-hub", "/messenger"):
            html = self.host().get(page).get_data(as_text=True)
            self.assertNotIn("fonts.googleapis.com", html, page)
            self.assertIsNone(re.search(r'(?:src|href)="/static/(?!fonts/)[^"?]+"', html), f"unversioned asset on {page}")
        self._asset_url("/", "css/fonts.css")

    def test_versioned_assets_are_cached_forever(self):
        r = self.host().get(self._asset_url("/", "css/base.css"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], LONG_CACHE)

    def test_fonts_are_served_locally_and_cached(self):
        r = self.host().get("/static/fonts/inter-latin.woff2")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], LONG_CACHE)
        self.assertEqual(r.data[:4], b"wOF2")
        css = self.host().get(self._asset_url("/", "css/fonts.css")).get_data(as_text=True)
        for url in set(re.findall(r"url\(\.\./fonts/([^)]+)\)", css)):
            self.assertEqual(self.host().get(f"/static/fonts/{url}").status_code, 200, url)

    def test_unversioned_static_revalidates(self):
        r = self.host().get("/static/css/base.css")
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        etag = r.headers["ETag"]
        again = self.host().get("/static/css/base.css", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)

    def test_version_changes_with_content(self):
        target = self.settings.static_dir / "css" / "base.css"
        v1 = self.c and self.app.jinja_env.globals["asset"]("css/base.css")
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n/* changed */\n")
            import os, time
            os.utime(target, (time.time() + 5, time.time() + 5))
            v2 = self.app.jinja_env.globals["asset"]("css/base.css")
        finally:
            target.write_bytes(original)
        self.assertNotEqual(v1, v2)

    def test_gzip_text_responses(self):
        for url in (self._asset_url("/", "css/base.css"), self._asset_url("/messenger", "js/pages/messenger.js"), "/messenger"):
            plain = self.host().get(url)
            zipped = self.host().get(url, headers=GZ)
            self.assertEqual(zipped.headers.get("Content-Encoding"), "gzip", url)
            self.assertIn("Accept-Encoding", zipped.headers.get("Vary", ""))
            self.assertEqual(gzip.decompress(zipped.data), plain.data, url)
            self.assertLess(len(zipped.data), len(plain.data) * 0.5, url)

    def test_gzip_conditional_request_still_304(self):
        url = self._asset_url("/", "css/base.css")
        first = self.host().get(url, headers=GZ)
        again = self.host().get(url, headers={**GZ, "If-None-Match": first.headers["ETag"]})
        self.assertEqual(again.status_code, 304)

    def test_downloads_and_binary_are_not_compressed(self):
        self.lan().post("/upload", data={"file": (io.BytesIO(b"x" * 5000), "big.txt")},
                        content_type="multipart/form-data")
        r = self.lan().get("/download/big.txt", headers=GZ)
        self.assertNotIn("Content-Encoding", r.headers)
        self.assertEqual(r.data, b"x" * 5000)
        r = self.host().get("/static/fonts/inter-latin.woff2", headers=GZ)
        self.assertNotIn("Content-Encoding", r.headers)

    def test_small_responses_not_compressed(self):
        r = self.host().get("/api/auth/status", headers=GZ)
        self.assertNotIn("Content-Encoding", r.headers)
