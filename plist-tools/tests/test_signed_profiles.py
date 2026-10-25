import base64
import ntpath
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import signed_profiles as signed
from mobileconfig import make_profile
from plist_core import parse_xml


def container(oid=2, indefinite=False):
    content = b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x07" + bytes([oid]) + b"\xa0\x00"
    return b"\x30\x80" + content + b"\x00\x00" if indefinite else b"\x30" + bytes([len(content)]) + content


def pem(data, label="CMS"):
    return (b"-----BEGIN " + label.encode() + b"-----\n" + base64.b64encode(data)
            + b"\n-----END " + label.encode() + b"-----\n")


class DetectionTests(unittest.TestCase):
    def test_der_ber_and_pem_content_info_oids(self):
        for oid, expected in ((2, "signed"), (3, "encrypted"), (6, "encrypted"), (1, None)):
            for indefinite in (False, True):
                raw = container(oid, indefinite)
                for data in (raw, pem(raw), pem(raw, "PKCS7")):
                    with self.subTest(oid=oid, indefinite=indefinite, pem=data[:5]):
                        self.assertEqual(expected, signed.detect_container(data))

    def test_malformed_headers_pem_and_unrelated_content(self):
        cases = [b"", b"<plist/>", "not bytes", b"\x30\x82\xff\xff", b"\x30\x85\x00\x00\x00\x00\x0b",
                 b"\x30\x80\x06\x80", b"\x31\x80" + container()[2:],
                 pem(container()).replace(b"END CMS", b"END PKCS7"),
                 b"-----BEGIN CMS-----\n!invalid\n-----END CMS-----",
                 b"-----BEGIN CMS-----\n" + b" " * signed.DEFAULT_MAX_BYTES]
        for data in cases:
            with self.subTest(data=str(data[:30])):
                self.assertIsNone(signed.detect_container(data))


class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "original.mobileconfig")
        with open(self.path, "wb") as handle:
            handle.write(container())
        self.profile = make_profile().encode("utf-8")
        self.resolver = mock.patch.object(signed, "_resolve_decoder", return_value=("openssl", "/tool/openssl"))
        self.resolve = self.resolver.start()
        self.addCleanup(self.resolver.stop)

    def runner(self, content=None, stderr=b"", returncode=0):
        def run(args, **kwargs):
            output = args[args.index("-out") + 1] if "-out" in args else args[args.index("-o") + 1]
            with open(output, "wb") as handle:
                handle.write(self.profile if content is None else content)
            kwargs["stderr"].write(stderr)
            return subprocess.CompletedProcess(args, returncode)
        return run

    def test_signature_checking_command_snapshot_and_original_preserved(self):
        seen = {}
        def run(args, **kwargs):
            snapshot = args[args.index("-in") + 1]
            self.assertNotEqual(self.path, snapshot)
            with open(snapshot, "rb") as handle:
                self.assertEqual(container(), handle.read())
            seen["snapshot"] = snapshot
            self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
            self.assertEqual(subprocess.DEVNULL, kwargs["stdout"])
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(15, kwargs["timeout"])
            self.assertIn("-verify", args)
            self.assertIn("-noverify", args)
            self.assertNotIn("-nosigs", args)
            self.assertNotIn("-no_content_verify", args)
            return self.runner()(args, **kwargs)
        with mock.patch.object(signed.subprocess, "run", side_effect=run):
            self.assertEqual(self.profile.decode(), signed.decode_signed_profile(self.path))
        self.assertFalse(os.path.exists(seen["snapshot"]))
        with open(self.path, "rb") as handle:
            self.assertEqual(container(), handle.read())

    def test_source_mutation_cannot_change_snapshot_after_inspection(self):
        def resolve(_):
            with open(self.path, "wb") as handle:
                handle.write(container(3))
            return "openssl", "/tool/openssl"
        def run(args, **kwargs):
            with open(args[args.index("-in") + 1], "rb") as handle:
                self.assertEqual(container(), handle.read())
            return self.runner()(args, **kwargs)
        self.resolve.side_effect = resolve
        with mock.patch.object(signed.subprocess, "run", side_effect=run):
            signed.decode_signed_profile(self.path)

    def test_encrypted_and_unknown_containers_do_not_resolve_or_invoke_decoder(self):
        for data in (container(3), container(6, True), pem(container(3)), b"not CMS"):
            with open(self.path, "wb") as handle:
                handle.write(data)
            with mock.patch.object(signed.subprocess, "run") as run:
                with self.assertRaises(signed.SignedProfileError):
                    signed.decode_signed_profile(self.path)
                run.assert_not_called()
        self.resolve.assert_not_called()

    def test_pem_input_sets_pem_format_and_ber_is_accepted(self):
        for data, expected in ((pem(container()), "PEM"), (container(indefinite=True), "DER")):
            with open(self.path, "wb") as handle:
                handle.write(data)
            with mock.patch.object(signed.subprocess, "run", side_effect=self.runner()) as run:
                signed.decode_signed_profile(self.path)
                args = run.call_args[0][0]
                self.assertEqual(expected, args[args.index("-inform") + 1])

    def test_input_output_and_stderr_have_bounds(self):
        with self.assertRaisesRegex(signed.SignedProfileError, "input exceeds"):
            signed.decode_signed_profile(self.path, max_bytes=5)
        self.resolve.assert_not_called()
        for output, stderr, expected in ((b"x" * 101, b"", "output exceeds"),
                                          (self.profile, b"x" * (signed.MAX_STDERR_BYTES + 1), "stderr exceeds")):
            with mock.patch.object(signed.subprocess, "run", side_effect=self.runner(output, stderr)):
                with self.assertRaisesRegex(signed.SignedProfileError, expected):
                    signed.decode_signed_profile(self.path, max_bytes=100)

    def test_invalid_or_nonprofile_xml_is_rejected(self):
        for data in (b"not XML", b"<plist><string>hello</string></plist>", b"\xff\x00invalid"):
            with mock.patch.object(signed.subprocess, "run", side_effect=self.runner(data)):
                with self.assertRaises(signed.SignedProfileError):
                    signed.decode_signed_profile(self.path)

    def test_utf16_xml_output_is_accepted(self):
        text = self.profile.decode().replace('encoding="UTF-8"', 'encoding="UTF-16"')
        with mock.patch.object(signed.subprocess, "run", side_effect=self.runner(text.encode("utf-16"))):
            self.assertEqual(text, signed.decode_signed_profile(self.path))

    def test_literal_newlines_normalize_but_encoded_carriage_return_is_preserved(self):
        text = self.profile.decode().replace("New Configuration Profile", "Line one\r\nLine two\rLine three&#13;End")
        text = text.replace("</key>\n", "</key>\r\n")
        with mock.patch.object(signed.subprocess, "run", side_effect=self.runner(text.encode("utf-8"))):
            decoded = signed.decode_signed_profile(self.path)
        self.assertNotIn("\r", decoded)
        self.assertIn("Line one\nLine two\nLine three&#13;End", decoded)
        self.assertEqual("Line one\nLine two\nLine three\rEnd", parse_xml(decoded).value["PayloadDisplayName"])
        self.assertEqual(parse_xml(text).value, parse_xml(decoded).value)

    def test_bad_signature_does_not_fall_back_and_diagnostics_are_bounded(self):
        with mock.patch.object(signed.subprocess, "run", side_effect=self.runner(stderr=b"bad signature " + b"x" * 3000, returncode=4)) as run:
            with self.assertRaisesRegex(signed.SignedProfileError, "bad signature") as raised:
                signed.decode_signed_profile(self.path)
            self.assertLess(len(str(raised.exception)), 2200)
            self.assertEqual(1, run.call_count)

    def test_timeouts_and_process_errors_become_decoder_errors(self):
        for error in (subprocess.TimeoutExpired("openssl", 0.1), OSError("cannot launch"), subprocess.SubprocessError("failed")):
            with mock.patch.object(signed.subprocess, "run", side_effect=error):
                with self.assertRaises(signed.SignedProfileError):
                    signed.decode_signed_profile(self.path, timeout=0.1)
        self.resolve.side_effect = signed.SignedProfileError("No CMS decoder found")
        with self.assertRaisesRegex(signed.SignedProfileError, "No CMS decoder"):
            signed.decode_signed_profile(self.path)

    def test_windows_is_hidden_and_security_uses_distinct_arguments(self):
        with mock.patch.object(signed.sys, "platform", "win32"), mock.patch.object(signed.subprocess, "run", side_effect=self.runner()) as run:
            signed.decode_signed_profile(self.path)
            self.assertEqual(0x08000000, run.call_args[1]["creationflags"])
        self.resolve.return_value = ("security", "/usr/bin/security")
        with mock.patch.object(signed.subprocess, "run", side_effect=self.runner()) as run:
            signed.decode_signed_profile(self.path)
            args = run.call_args[0][0]
            self.assertEqual(["/usr/bin/security", "cms", "-D", "-i"], args[:4])


class ResolutionTests(unittest.TestCase):
    def test_configured_absolute_executable_is_used_before_discovery(self):
        with mock.patch.object(signed, "_executable", return_value=True), mock.patch.object(signed.shutil, "which") as which:
            self.assertEqual(("openssl", "/configured/openssl"), signed._resolve_decoder("/configured/openssl"))
            which.assert_not_called()
        with mock.patch.object(signed, "_executable", return_value=False):
            with self.assertRaises(signed.SignedProfileError):
                signed._resolve_decoder("relative/openssl")

    def test_path_precedes_windows_git_and_macos_fallback(self):
        with mock.patch.object(signed.shutil, "which", return_value="/path/openssl"), mock.patch.object(signed, "_executable", return_value=True):
            self.assertEqual(("openssl", "/path/openssl"), signed._resolve_decoder())
        with mock.patch.object(signed.shutil, "which", return_value=None), mock.patch.object(signed.sys, "platform", "win32"), mock.patch.dict(signed.os.environ, {"ProgramFiles": "GITROOT"}, clear=True), mock.patch.object(signed, "_executable", side_effect=lambda path: path == os.path.join("GITROOT", "Git", "usr", "bin", "openssl.exe")):
            self.assertEqual("openssl", signed._resolve_decoder()[0])
        with mock.patch.object(signed.shutil, "which", return_value=None), mock.patch.object(signed.sys, "platform", "darwin"), mock.patch.object(signed, "_executable", side_effect=lambda path: path == "/usr/bin/security"):
            self.assertEqual(("security", "/usr/bin/security"), signed._resolve_decoder())
        with mock.patch.object(signed.shutil, "which", return_value=None), mock.patch.object(signed.sys, "platform", "linux"), mock.patch.object(signed, "_executable", return_value=False):
            with self.assertRaises(signed.SignedProfileError):
                signed._resolve_decoder()

    def test_git_on_path_precedes_standard_install_locations(self):
        git_root = r"D:\PortableGit"
        expected = ntpath.join(git_root, "usr", "bin", "openssl.exe")
        for git_subdirectory in ("cmd", "bin", r"mingw64\bin"):
            git_path = ntpath.join(git_root, git_subdirectory, "git.exe")
            with mock.patch.object(signed.sys, "platform", "win32"), mock.patch.object(signed.shutil, "which", side_effect=lambda name: git_path if name == "git" else None), mock.patch.object(signed, "_executable", side_effect=lambda path: path == expected), mock.patch.object(signed, "_known_folder") as folder:
                self.assertEqual(("openssl", expected), signed._resolve_decoder())
                folder.assert_not_called()

    def test_empty_environment_uses_windows_known_folder_defaults(self):
        folders = {0x26: "PROGRAMFILES", 0x2a: "PROGRAMFILESX86", 0x1c: "LOCALAPPDATA"}
        for folder_id, suffix in ((0x26, ("Git", "usr", "bin", "openssl.exe")),
                                  (0x2a, ("Git", "usr", "bin", "openssl.exe")),
                                  (0x1c, ("Programs", "Git", "usr", "bin", "openssl.exe"))):
            expected = os.path.join(folders[folder_id], *suffix)
            with mock.patch.object(signed.sys, "platform", "win32"), mock.patch.dict(signed.os.environ, {}, clear=True), mock.patch.object(signed.shutil, "which", return_value=None), mock.patch.object(signed, "_known_folder", side_effect=folders.get) as folder, mock.patch.object(signed, "_executable", side_effect=lambda path: path == expected):
                self.assertEqual(("openssl", expected), signed._resolve_decoder())
                folder.assert_any_call(folder_id)

    def test_invalid_configured_path_types_are_decoder_errors(self):
        for value in (123, [], {}, b"/binary/path"):
            with self.subTest(value=value), self.assertRaises(signed.SignedProfileError):
                signed._resolve_decoder(value)


if __name__ == "__main__":
    unittest.main()
