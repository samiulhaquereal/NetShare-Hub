import io
import threading
import time
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from PIL import Image

from netshare.infrastructure import file_store
from netshare.infrastructure.file_store import FileStore, human_size
from tests.helpers import AppTestCase


class FileShareTests(AppTestCase):
    def _upload(self, client, name="hello.txt", data=b"hi"):
        return client.post("/upload", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")

    def _put(self, client, name, data=b"hi", **headers):
        return client.put("/upload-stream", data=data, headers={"X-File-Name": quote(name), **headers})

    def _visible(self):
        """Files in the upload folder, ignoring the hidden .incoming staging folder."""
        return [p for p in self.settings.upload_dir.iterdir() if not p.name.startswith(".")]

    def _names(self, client=None):
        return [f["name"] for f in (client or self.lan()).get("/api/files").get_json()["files"]]

    def test_upload_download_roundtrip(self):
        r = self._upload(self.lan(), "report (final).txt", b"payload")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["filename"], "report (final).txt")
        self.assertEqual(self.lan().get("/download/report%20(final).txt").data, b"payload")

    def test_stream_upload_roundtrip_with_unicode_name(self):
        r = self._put(self.lan(), "ছবি ২০২৬.txt", b"x" * 300_000)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()["filename"], "ছবি ২০২৬.txt")
        self.assertEqual(len(self.lan().get("/download/" + quote("ছবি ২০২৬.txt")).data), 300_000)

    def test_stream_upload_requires_name(self):
        self.assertEqual(self.lan().put("/upload-stream", data=b"x").status_code, 400)

    def test_duplicate_names_get_suffix(self):
        names = [self._put(self.lan(), "hello.txt").get_json()["filename"] for _ in range(3)]
        self.assertEqual(names, ["hello.txt", "hello(1).txt", "hello(2).txt"])

    def test_upload_strips_path_components(self):
        for client_name in ("../../evil.txt", "..\\..\\evil2.txt"):
            name = self._put(self.lan(), client_name).get_json()["filename"]
            self.assertTrue(name.startswith("evil"), name)
            self.assertTrue((self.settings.upload_dir / name).exists())

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
        r = c.post("/delete-multiple", json={"files": ["../secret.txt"]})
        self.assertEqual(r.get_json()["failed"], ["../secret.txt"])
        self.assertTrue((self.settings.app_dir / "secret.txt").exists())

    def test_zip_is_streamed_and_ignores_outside_paths(self):
        (self.settings.app_dir / "secret.txt").write_text("top secret")
        self._put(self.lan(), "a.txt", b"A" * 3_000_000)
        self._put(self.lan(), "b.txt", b"B")
        r = self.lan().post("/download-multiple", json={"files": ["a.txt", "../secret.txt", "b.txt", "a.txt"]})
        self.assertTrue(r.is_streamed)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            self.assertEqual(zf.namelist(), ["a.txt", "b.txt"])
            self.assertEqual(zf.read("a.txt"), b"A" * 3_000_000)
            self.assertIsNone(zf.testzip())

    def test_zip_accepts_a_plain_form_post(self):
        self._put(self.lan(), "a.txt", b"A")
        r = self.lan().post("/download-multiple", data={"files": ["a.txt"]})
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            self.assertEqual(zf.namelist(), ["a.txt"])

    def test_listing_api_and_bulk_delete(self):
        for n in ("x.png", "y.pdf", "z.txt"):
            self._put(self.lan(), n, b"data")
        r = self.lan().get("/api/files")
        data = r.get_json()
        kinds = {f["name"]: f["kind"] for f in data["files"]}
        self.assertEqual(kinds, {"x.png": "image", "y.pdf": "pdf", "z.txt": "text"})
        self.assertGreater(int(r.headers["X-Disk-Free"]), 0)
        self.assertEqual(set(data["files"][0]), {"name", "size", "mtime", "kind", "icon", "ext"})

        r = self.lan().post("/delete-multiple", json={"files": ["x.png", "y.pdf", "missing.bin"]})
        self.assertEqual(r.get_json()["deleted"], 2)
        self.assertEqual(r.get_json()["failed"], ["missing.bin"])
        self.assertEqual(self._names(), ["z.txt"])

    def test_bulk_delete_rejects_form_posts(self):
        """A cross-site <form> can't send JSON, so it can't delete anything."""
        self._put(self.lan(), "keep.txt")
        r = self.lan().post("/delete-multiple", data={"files": ["keep.txt"]})
        self.assertEqual(r.status_code, 415)
        self.assertEqual(self._names(), ["keep.txt"])

    def test_failed_upload_leaves_nothing_behind(self):
        class Boom(io.BytesIO):
            def read(self, *_a):
                raise OSError("disk full")
        with self.assertRaises(OSError):
            self.c.files.save_upload("x.bin", Boom())
        self.assertEqual(self._visible(), [])

    def test_truncated_upload_is_rejected(self):
        with self.assertRaises(file_store.IncompleteUploadError):
            self.c.files.save_upload("x.bin", io.BytesIO(b"abc"), expected_size=10)
        self.assertEqual(self._visible(), [])

    def test_in_progress_upload_is_invisible(self):
        """Other devices must never see or download a half-written file."""
        gate, started = threading.Event(), threading.Event()

        class Slow(io.BytesIO):
            def read(self, n=-1):
                started.set()
                gate.wait(5)
                return super().read(n)

        t = threading.Thread(target=self.c.files.save_upload, args=("big.bin", Slow(b"x" * 10)))
        t.start()
        started.wait(5)
        try:
            self.assertEqual(self._names(), [])
            self.assertEqual(self.lan().get("/download/big.bin").status_code, 404)
        finally:
            gate.set()
            t.join(5)
        self.assertEqual(self._names(), ["big.bin"])

    def test_disk_full_answers_507_before_writing(self):
        with mock.patch.object(FileStore, "free_space", return_value=1024):
            r = self._put(self.lan(), "huge.bin", b"x" * 2048)
        self.assertEqual(r.status_code, 507)
        self.assertIn("space", r.get_json()["error"])
        self.assertEqual(self._visible(), [])

    def test_thumbnail_is_cached_on_disk(self):
        buf = io.BytesIO()
        Image.new("RGB", (1600, 1200), "red").save(buf, "JPEG")
        self._put(self.lan(), "p.jpg", buf.getvalue())
        r1 = self.lan().get("/file-preview/p.jpg?thumb=1&v=1")
        self.assertEqual(r1.mimetype, "image/jpeg")
        self.assertIn("immutable", r1.headers["Cache-Control"])
        with Image.open(io.BytesIO(r1.data)) as im:
            self.assertLessEqual(max(im.size), 160)
        cached = list((self.settings.app_dir / "cache" / "thumbs").iterdir())
        self.assertEqual(len(cached), 1)
        with mock.patch.object(file_store.Image, "open", side_effect=AssertionError("re-decoded")):
            self.assertEqual(self.lan().get("/file-preview/p.jpg?thumb=1&v=1").data, r1.data)

    def test_text_preview_supports_range(self):
        self._put(self.lan(), "log.txt", b"0123456789" * 1000)
        r = self.lan().get("/file-preview/log.txt", headers={"Range": "bytes=0-99"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(len(r.data), 100)


class ListSyncTests(AppTestCase):
    """Hundreds of devices must get small deltas, not the whole list on every change."""

    def test_delta_contains_only_changes(self):
        self._put = FileShareTests._put.__get__(self)
        self._put(self.lan(), "a.txt")
        v1 = self.lan().get("/api/files").get_json()["version"]
        self._put(self.lan(), "b.txt")
        self.lan().post("/delete-multiple", json={"files": ["a.txt"]})
        d = self.lan().get(f"/api/files?since={v1}").get_json()
        self.assertTrue(d["delta"])
        self.assertEqual([f["name"] for f in d["added"]], ["b.txt"])
        self.assertEqual(d["removed"], ["a.txt"])
        same = self.lan().get(f"/api/files?since={d['version']}").get_json()
        self.assertEqual((same["added"], same["removed"]), ([], []))

    def test_upload_then_delete_nets_out(self):
        self._put = FileShareTests._put.__get__(self)
        v = self.c.files.version
        self._put(self.lan(), "tmp.txt")
        self.c.files.delete("tmp.txt")
        _, added, removed = self.c.files.changes_since(v)
        self.assertEqual(added, [])
        self.assertEqual(removed, ["tmp.txt"])

    def test_external_change_forces_full_list(self):
        v = self.c.files.version
        time.sleep(0.02)
        (self.settings.upload_dir / "dropped-by-hand.txt").write_text("x")
        self.assertIsNone(self.c.files.changes_since(v))
        body = self.lan().get(f"/api/files?since={v}").get_json()
        self.assertNotIn("delta", body)
        self.assertEqual([f["name"] for f in body["files"]], ["dropped-by-hand.txt"])

    def test_trimmed_journal_still_gives_exact_deltas(self):
        store = FileStore(self.settings.app_dir / "j", journal_size=5)
        versions = []
        for i in range(23):
            versions.append(store.version)
            store.save_upload(f"f{i}.txt", io.BytesIO(b"x"))
        self.assertIsNone(store.changes_since(versions[0]), "too old: trimmed away")
        for k in (15, 18, 22):
            _, added, removed = store.changes_since(versions[k])
            self.assertEqual([e["name"] for e in added], [f"f{i}.txt" for i in range(k, 23)])
            self.assertEqual(removed, [])

    def test_big_listing_is_reused_briefly_and_flagged_behind(self):
        (self.settings.upload_dir / "a.txt").write_text("x")
        store = self.c.files
        self.lan().get("/api/files")
        store._build_cost = 2.0  # pretend the folder is huge
        time.sleep(0.05)  # next file-time tick, so the folder mtime moves
        (self.settings.upload_dir / "b.txt").write_text("x")  # external change → journal reset
        self.assertEqual(len(self.lan().get("/api/files").get_json()["files"]), 2, "out of journal range: rebuilt")
        store._build_cost = 2.0
        self._put = FileShareTests._put.__get__(self)
        self._put(self.lan(), "c.txt")
        r = self.lan().get("/api/files")
        self.assertEqual(len(r.get_json()["files"]), 2, "recent snapshot reused instead of a slow rebuild")
        self.assertEqual(r.headers.get("X-List-Behind"), "1")
        d = self.lan().get(f"/api/files?since={r.get_json()['version']}").get_json()
        self.assertEqual([f["name"] for f in d["added"]], ["c.txt"])

    def test_file_count_catches_changes_the_mtime_missed(self):
        store = self.c.files
        self.lan().get("/api/files")          # learns the count (0)
        v = store.version
        (self.settings.upload_dir / "sneaky.txt").write_text("x")
        store._own_sig = store.dir_signature()  # simulate: mtime didn't move
        self.assertIsNotNone(store.changes_since(v), "within the reconcile interval")
        store._reconciled_at -= 60
        self.assertIsNone(store.changes_since(v), "count mismatch → full list")

    def test_unknown_or_future_version_gets_full_list(self):
        self.assertNotIn("delta", self.lan().get("/api/files?since=1").get_json())
        self.assertNotIn("delta", self.lan().get("/api/files?since=99999999999999").get_json())

    def test_full_list_is_built_once_and_served_gzipped_with_etag(self):
        (self.settings.upload_dir / "a.txt").write_text("x" * 10)
        with mock.patch.object(FileStore, "scan", wraps=self.c.files.scan) as scans:
            r1 = self.lan().get("/api/files", headers={"Accept-Encoding": "gzip"})
            r2 = self.lan().get("/api/files", headers={"Accept-Encoding": "gzip"})
            self.assertEqual(scans.call_count, 1, "second client reuses the cached listing")
        self.assertEqual(r1.headers["Content-Encoding"], "gzip")
        self.assertEqual(r1.data, r2.data)
        r3 = self.lan().get("/api/files", headers={"If-None-Match": r1.headers["ETag"]})
        self.assertEqual(r3.status_code, 304)

    def test_in_flight_uploads_reserve_disk_space(self):
        """Parallel uploads must not each see the same free space and overfill the disk."""
        free = 10 * 1024 ** 2 + file_store.FREE_SPACE_RESERVE
        with mock.patch.object(FileStore, "free_space", return_value=free):
            first = self.c.files._reserve(6 * 1024 ** 2)
            with self.assertRaises(file_store.InsufficientSpaceError):
                self.c.files._reserve(6 * 1024 ** 2)
            self.c.files._release(first)
            self.c.files._release(self.c.files._reserve(6 * 1024 ** 2))

    def test_partial_uploads_do_not_touch_the_folder(self):
        sig = self.c.files.dir_signature()
        with self.assertRaises(file_store.IncompleteUploadError):
            self.c.files.save_upload("x.bin", io.BytesIO(b"abc"), expected_size=10)
        self.assertEqual(self.c.files.dir_signature(), sig)


class StoreUnitTests(AppTestCase):
    def test_human_size_units(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(2048), "2.0 KB")
        self.assertEqual(human_size(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(human_size(3 * 1024 ** 3), "3.0 GB")

    def test_stale_part_files_are_swept_on_start(self):
        root = Path(self.settings.upload_dir) / ".incoming"
        old = root / ".upload-dead.part"
        old.write_bytes(b"x")
        old_time = time.time() - 7 * 3600
        import os
        os.utime(old, (old_time, old_time))
        fresh = root / ".upload-live.part"
        fresh.write_bytes(b"x")
        FileStore(root.parent)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists(), "an upload still in progress must survive")


class FolderTests(AppTestCase):
    """Sub-folders the host creates by hand inside the upload folder."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.settings.upload_dir)
        notes = self.root / "Class Notes"
        (notes / "Week 1").mkdir(parents=True)
        (notes / "ছবি.txt").write_text("bangla", encoding="utf-8")
        (notes / "Week 1" / "intro.txt").write_text("hello")
        (notes / ".secret").write_text("hidden")
        (self.root / "top.txt").write_text("top")

    def test_root_listing_shows_folders(self):
        data = self.lan().get("/api/files").get_json()
        self.assertEqual([f["name"] for f in data["folders"]], ["Class Notes"])
        self.assertNotIn(".incoming", [f["name"] for f in data["folders"]])
        self.assertEqual([f["name"] for f in data["files"]], ["top.txt"])

    def test_subfolder_listing_and_download(self):
        r = self.lan().get("/api/files?dir=" + quote("Class Notes"))
        data = r.get_json()
        self.assertEqual(data["dir"], "Class Notes")
        self.assertEqual([f["name"] for f in data["folders"]], ["Week 1"])
        self.assertEqual([f["name"] for f in data["files"]], ["ছবি.txt"])  # dot files hidden
        self.assertEqual(self.lan().get("/api/files?dir=Class%20Notes",
                                        headers={"If-None-Match": r.headers["ETag"]}).status_code, 304)
        self.assertEqual(self.lan().get("/download/" + quote("Class Notes/Week 1/intro.txt")).data, b"hello")
        self.assertEqual(self.lan().get("/file-preview/" + quote("Class Notes/ছবি.txt")).status_code, 200)

    def test_bad_folder_paths_rejected(self):
        c = self.lan()
        self.assertEqual(c.get("/api/files?dir=Nope").status_code, 404)
        for bad in ("..", "Class Notes/..", ".incoming", "Class Notes/../..", "top.txt"):
            self.assertIn(c.get("/api/files?dir=" + quote(bad)).status_code, (400, 404), bad)
        for bad in ("Class Notes/.secret", ".incoming/x", "Class Notes/../../x"):
            self.assertIn(c.get("/download/" + quote(bad)).status_code, (400, 404), bad)

    def test_upload_into_subfolder(self):
        r = self.lan().put("/upload-stream", data=b"new",
                           headers={"X-File-Name": "a.txt", "X-Folder": quote("Class Notes/Week 1")})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()["filename"], "Class Notes/Week 1/a.txt")
        self.assertEqual((self.root / "Class Notes" / "Week 1" / "a.txt").read_bytes(), b"new")
        self.assertNotIn("a.txt", [f["name"] for f in self.lan().get("/api/files").get_json()["files"]])

    def test_upload_into_missing_or_unsafe_folder_fails(self):
        r = self.lan().put("/upload-stream", data=b"x", headers={"X-File-Name": "a.txt", "X-Folder": "Gone"})
        self.assertEqual(r.status_code, 404)
        r = self.lan().put("/upload-stream", data=b"x", headers={"X-File-Name": "a.txt", "X-Folder": "../.."})
        self.assertEqual(r.status_code, 404)  # unsafe paths look like missing ones
        self.assertFalse(any(self.root.parent.glob("a.txt")))

    def test_delete_file_in_subfolder_but_never_a_folder(self):
        c = self.lan()
        r = c.post("/delete-multiple", json={"files": ["Class Notes/Week 1/intro.txt", "Class Notes"]})
        self.assertEqual(r.get_json()["deleted"], 1)
        self.assertEqual(r.get_json()["failed"], ["Class Notes"])
        self.assertTrue((self.root / "Class Notes").is_dir())
        self.assertFalse((self.root / "Class Notes" / "Week 1" / "intro.txt").exists())

    def test_folder_downloads_as_zip_tree(self):
        r = self.lan().post("/download-multiple", json={"files": ["Class Notes"]})
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            self.assertEqual(sorted(zf.namelist()), ["Class Notes/Week 1/intro.txt", "Class Notes/ছবি.txt"])
        self.assertIn("Class%20Notes.zip", r.headers["Content-Disposition"])

    def test_subfolder_changes_do_not_reset_root_journal(self):
        v = self.c.files.version
        self.c.files.save_upload("b.txt", io.BytesIO(b"x"), folder="Class Notes")
        self.assertEqual(self.c.files.changes_since(v), (v, [], []))

    def test_create_folder_from_web(self):
        c = self.lan()
        r = c.post("/api/folders", json={"parent": "", "name": "  নতুন Folder  "})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.get_json()["path"], "নতুন Folder")
        self.assertIn("নতুন Folder", [f["name"] for f in c.get("/api/files").get_json()["folders"]])
        r = c.post("/api/folders", json={"parent": "Class Notes/Week 1", "name": "Day 1"})
        self.assertEqual(r.get_json()["path"], "Class Notes/Week 1/Day 1")
        self.assertTrue((self.root / "Class Notes" / "Week 1" / "Day 1").is_dir())

    def test_create_folder_rejects_bad_input(self):
        c = self.lan()
        self.assertEqual(c.post("/api/folders", json={"name": "Movies"}).status_code, 201)
        self.assertEqual(c.post("/api/folders", json={"name": "Movies"}).status_code, 409)
        self.assertEqual(c.post("/api/folders", json={"name": "top.txt"}).status_code, 409)
        for bad in ("", "...", "///", "x" * 101):
            self.assertEqual(c.post("/api/folders", json={"name": bad}).status_code, 400, bad)
        self.assertEqual(c.post("/api/folders", json={"name": "../evil"}).get_json()["path"], "evil")
        self.assertFalse((self.root.parent / "evil").exists())
        self.assertEqual(c.post("/api/folders", json={"parent": "Nope", "name": "a"}).status_code, 404)
        self.assertEqual(c.post("/api/folders", json={"parent": "..", "name": "a"}).status_code, 404)
        self.assertEqual(c.post("/api/folders", data={"name": "a"}).status_code, 415)  # no cross-site forms

    def test_new_top_level_folder_makes_clients_refetch(self):
        v = self.c.files.version
        self.c.files.create_folder("", "Fresh")
        self.assertIsNone(self.c.files.changes_since(v), "folder list changed → full list")
        v = self.c.files.version
        self.c.files.create_folder("Fresh", "Inner")
        self.assertEqual(self.c.files.changes_since(v), (v, [], []))


class ResumableUploadTests(AppTestCase):
    ID = "0123456789abcdef0123456789abcdef"

    def _put(self, data, offset, total, name="video.mp4", upload_id=None, **extra):
        return self.lan().put("/upload-stream", data=data, content_length=len(data), headers={
            "X-File-Name": name, "X-Upload-Id": upload_id or self.ID,
            "X-Upload-Offset": str(offset), "X-File-Size": str(total), **extra})

    def test_resume_after_dropped_connection(self):
        payload = bytes(range(256)) * 400  # 102,400 bytes

        class Cut(io.BytesIO):  # connection dies after 40,000 bytes
            def read(self, n=-1):
                if self.tell() >= 40_000:
                    raise ConnectionResetError("wifi gone")
                return super().read(min(n, 40_000 - self.tell()) if n and n > 0 else 40_000 - self.tell())

        with self.assertRaises(file_store.IncompleteUploadError):
            self.c.files.save_resumable(self.ID, "video.mp4", Cut(payload), 0, len(payload), len(payload))
        self.assertEqual(self.lan().get(f"/upload-stream/{self.ID}").get_json(), {"offset": 40_000})
        self.assertEqual(self._put(payload, 0, len(payload)).status_code, 409, "stale offset is refused")
        r = self._put(payload[40_000:], 40_000, len(payload))
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((self.settings.upload_dir / "video.mp4").read_bytes(), payload)
        status = self.lan().get(f"/upload-stream/{self.ID}").get_json()
        self.assertEqual(status, {"done": True, "filename": "video.mp4"}, "lost reply → no duplicate re-upload")
        self.assertEqual(self._put(b"", len(payload), len(payload)).get_json()["filename"], "video.mp4")
        self.assertEqual(len(list(self.settings.upload_dir.glob("video*.mp4"))), 1)

    def test_partial_upload_is_invisible_and_resumable_in_pieces(self):
        r = self._put(b"a" * 10, 0, 25)
        self.assertEqual((r.status_code, r.get_json()["offset"]), (202, 10))
        self.assertEqual(self.lan().get("/api/files").get_json()["files"], [])
        self.assertEqual(self._put(b"b" * 15, 10, 25).status_code, 200)
        self.assertEqual((self.settings.upload_dir / "video.mp4").read_bytes(), b"a" * 10 + b"b" * 15)

    def test_resumable_into_folder_and_bad_input(self):
        (self.settings.upload_dir / "Videos").mkdir()
        r = self._put(b"xyz", 0, 3, **{"X-Folder": "Videos"})
        self.assertEqual(r.get_json()["filename"], "Videos/video.mp4")
        self.assertEqual(self._put(b"x", 0, 1, upload_id="../../etc").status_code, 404)
        self.assertEqual(self.lan().get("/upload-stream/..%2F..%2Fx").status_code, 404)
        self.assertEqual(self._put(b"xyz", 0, 2, upload_id="f" * 32).status_code, 400, "range past the end")
        self.assertEqual(self.lan().put("/upload-stream", data=b"x", headers={
            "X-File-Name": "a", "X-Upload-Id": "e" * 32}).status_code, 400, "size headers required")

    def test_concurrent_request_for_same_upload_is_busy(self):
        self.c.files._busy.add(self.ID)
        r = self._put(b"x", 0, 1)
        self.assertEqual((r.status_code, r.get_json()["status"]), (409, "busy"))
