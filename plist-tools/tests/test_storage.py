import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from plist_storage import ConflictError, atomic_write, fingerprint, text_hash, _replace_windows


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "test.plist")
        with open(self.path, "wb") as handle:
            handle.write(b"original")

    def read(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def test_roundtrip_and_mode(self):
        initial = fingerprint(self.path)
        updated = atomic_write(self.path, b"updated", initial)
        self.assertEqual(b"updated", self.read())
        self.assertEqual(initial["mode"], updated["mode"])
        self.assertEqual(updated, fingerprint(self.path))
        self.assertEqual(["test.plist"], os.listdir(self.directory.name))

    def test_external_change_never_overwritten(self):
        initial = fingerprint(self.path)
        with open(self.path, "wb") as handle:
            handle.write(b"external")
        with self.assertRaises(ConflictError):
            atomic_write(self.path, b"mine", initial)
        self.assertEqual(b"external", self.read())

    def test_failure_preserves_original_and_cleans_temp(self):
        with mock.patch("plist_storage.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_write(self.path, b"mine", fingerprint(self.path))
        self.assertEqual(b"original", self.read())
        self.assertEqual(["test.plist"], os.listdir(self.directory.name))

    def test_new_file_and_missing_parent(self):
        new = os.path.join(self.directory.name, "new.plist")
        self.assertIsNotNone(atomic_write(new, b"new", None))
        with self.assertRaises(OSError):
            atomic_write(os.path.join(self.directory.name, "missing", "new"), b"new", None)

    def test_readonly_refused(self):
        os.chmod(self.path, stat.S_IREAD)
        try:
            with self.assertRaises(PermissionError):
                atomic_write(self.path, b"mine", fingerprint(self.path))
        finally:
            os.chmod(self.path, stat.S_IWRITE | stat.S_IREAD)
        self.assertEqual(b"original", self.read())

    def test_hardlink_refused(self):
        other = os.path.join(self.directory.name, "linked")
        os.link(self.path, other)
        with self.assertRaises(OSError):
            fingerprint(self.path)

    def test_text_hash_uses_logical_line_endings(self):
        self.assertEqual(text_hash("one\ntwo\n"), text_hash("one\r\ntwo\r\n"))
        self.assertEqual(text_hash("one\ntwo\n"), text_hash("one\rtwo\r"))
        self.assertNotEqual(text_hash("&#13;"), text_hash("\n"))

    def windows_failure(self, code, effect):
        replacement = os.path.join(self.directory.name, "replacement")
        with open(replacement, "wb") as handle:
            handle.write(b"new")
        failure = OSError(code, "simulated Windows replacement failure")
        call = mock.Mock(side_effect=effect)
        dll = mock.Mock(ReplaceFileW=call)
        with mock.patch("ctypes.WinDLL", return_value=dll, create=True), \
                mock.patch("ctypes.get_last_error", return_value=code, create=True), \
                mock.patch("ctypes.WinError", return_value=failure, create=True):
            with self.assertRaises(OSError):
                _replace_windows(self.path, replacement)
        self.assertIsNotNone(call.call_args[0][2])
        return replacement

    def test_windows_failed_move_keeps_original_name_with_backup(self):
        def fail(path, replacement, backup, *args):
            # Windows error 1176 retains both names when a backup was requested.
            self.assertTrue(backup)
            return False
        replacement = self.windows_failure(1176, fail)
        self.assertEqual(b"original", self.read())
        self.assertTrue(os.path.exists(replacement))

    def test_windows_partial_move_restores_original_from_backup(self):
        def fail(path, replacement, backup, *args):
            os.replace(path, backup)
            return False
        replacement = self.windows_failure(1177, fail)
        self.assertEqual(b"original", self.read())
        self.assertFalse(os.path.exists(replacement + ".original"))

    def test_windows_partial_move_does_not_overwrite_concurrent_destination(self):
        def fail(path, replacement, backup, *args):
            os.replace(path, backup)
            with open(path, "wb") as handle:
                handle.write(b"concurrent")
            return False
        replacement = self.windows_failure(1177, fail)
        self.assertEqual(b"concurrent", self.read())
        with open(replacement + ".original", "rb") as handle:
            self.assertEqual(b"original", handle.read())


if __name__ == "__main__":
    unittest.main()
