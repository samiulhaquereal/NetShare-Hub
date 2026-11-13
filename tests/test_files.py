import io
import zipfile

from tests.helpers import AppTestCase


class FileShareTests(AppTestCase):
    def _upload(self, client, name="hello.txt", data=b"hi"):
        return client.post("/upload", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")

    def test_upload_download_roundtrip(self):
        r = self._upload(self.lan(), "report (final).txt", b"payload")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["filename"], "report (final).txt")
        self.assertEqual(self.lan().get("/download/report%20(final).txt").data, b"payload")

    def test_duplicate_names_get_suffix(self):
        names = [self._upload(self.lan()).get_json()["filename"] for _ in range(3)]
        self.assertEqual(names, ["hello.txt", "hello(1).txt", "hello(2).txt"])

    def test_upload_strips_path_components(self):
        name = self._upload(self.lan(), "../../evil.txt").get_json()["filename"]
        self.assertEqual(name, "evil.txt")
        self.assertTrue((self.settings.upload_dir / "evil.txt").exists())

    def test_path_traversal_is_blocked_everywhere(self):
        (self.settings.app_dir / "secret.txt").write_text("top secret")
        c = self.lan()
        for url in ("/download/..%2Fsecret.txt", "/file-preview/..%2Fsecret.txt",
                    "/download/%2E%2E%2Fsecret.txt", "/chat/download/..%2Fsecret.txt",
                    "/download/..%5Csecret.txt"):
            r = c.get(url)
            self.assertEqual(r.status_code, 404, url)
            self.assertNotIn(b"top secret", r.data, url)
        self.assertEqual(c.delete("/delete/..%2Fsecret.txt").status_code, 404)
        self.assertTrue((self.settings.app_dir / "secret.txt").exists())

    def test_zip_ignores_outside_paths(self):
        (self.settings.app_dir / "secret.txt").write_text("top secret")
        self._upload(self.lan(), "a.txt", b"A")
        r = self.lan().post("/download-multiple", json={"files": ["a.txt", "../secret.txt"]})
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            self.assertEqual(zf.namelist(), ["a.txt"])

    def test_delete_and_listing(self):
        self._upload(self.lan(), "x.png", b"not really png")
        self.assertIn(b"x.png", self.lan().get("/files").data)
        self.assertEqual(self.lan().delete("/delete/x.png").status_code, 200)
        self.assertIn(b"No files uploaded yet", self.lan().get("/files").data)

    def test_listing_escapes_names(self):
        # clean_filename strips <>, but files can also be dropped into the folder by hand
        (self.settings.upload_dir / "a&b.txt").write_text("x")
        body = self.lan().get("/files").get_data(as_text=True)
        self.assertIn("a&amp;b.txt", body)

    def test_failed_upload_leaves_no_placeholder(self):
        class Boom(io.BytesIO):
            def read(self, *_a):
                raise OSError("disk full")
        with self.assertRaises(OSError):
            self.c.files.save_upload("x.bin", Boom())
        self.assertFalse((self.settings.upload_dir / "x.bin").exists())
