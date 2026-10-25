import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from plist_storage import ConflictError, atomic_write, fingerprint


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


if __name__ == "__main__":
    unittest.main()
