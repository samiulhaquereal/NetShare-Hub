import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from netshare.infrastructure import shell


class OpenFolderTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "uploads"

    @mock.patch.object(shell.sys, "platform", "win32")
    def test_windows_uses_explorer(self):
        with mock.patch.object(shell.os, "startfile", create=True) as start:
            shell.open_folder(self.dir)
        start.assert_called_once_with(str(self.dir))
        self.assertTrue(self.dir.is_dir(), "missing folder is created first")

    @mock.patch.object(shell.sys, "platform", "linux")
    def test_linux_uses_xdg_open(self):
        with mock.patch.object(shell.shutil, "which", side_effect=lambda n: "/usr/bin/xdg-open" if n == "xdg-open" else None), \
                mock.patch.object(shell.subprocess, "Popen") as popen:
            shell.open_folder(self.dir)
        self.assertEqual(popen.call_args[0][0], ["/usr/bin/xdg-open", str(self.dir)])

    @mock.patch.object(shell.sys, "platform", "linux")
    def test_linux_without_file_manager_raises(self):
        with mock.patch.object(shell.shutil, "which", return_value=None):
            with self.assertRaises(OSError):
                shell.open_folder(self.dir)

    @mock.patch.object(shell.sys, "platform", "darwin")
    def test_macos_uses_open(self):
        with mock.patch.object(shell.subprocess, "Popen") as popen:
            shell.open_folder(self.dir)
        self.assertEqual(popen.call_args[0][0], ["open", str(self.dir)])
