import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import (Collector, GitError, Options, Piece, Request, canonical, code_block,
                  excluded, git_diff, git_paths, git_root, git_run, ignored_paths, inside, line_count, has_symlink)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name

    def write(self, name, content):
        path = Path(self.root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return str(path)

    def pack(self, requests, **kwargs):
        return Collector([self.root], kwargs.pop("options", None)).build(requests, **kwargs)

    def test_buffer_beats_saved_content_without_writing(self):
        path = self.write("hello.py", "saved\n")
        text = self.pack([Request(path=path)], buffers={canonical(path): Request(path=path, text="unsaved λ", modified=True)})
        self.assertIn("unsaved λ", text)
        self.assertIn("unsaved buffer", text)
        self.assertNotIn("saved\n", text)
        self.assertEqual(Path(path).read_text(), "saved\n")

    def test_equally_named_untitled_requests_keep_both_buffers(self):
        first = Request(text="FIRST_BUFFER_CONTENT", label="Notes", modified=True)
        second = Request(text="SECOND_BUFFER_CONTENT", label="Notes", modified=True)
        text = self.pack([first, second, first])
        self.assertIn("2 item(s)", text)
        self.assertEqual(1, text.count("FIRST_BUFFER_CONTENT"))
        self.assertEqual(1, text.count("SECOND_BUFFER_CONTENT"))

    def test_selection_preserves_multiple_ranges_and_fences(self):
        path = self.write("source.py", "disk")
        pieces = [Piece("```\n``````", 2, 3), Piece("second\n", 9, 9)]
        text = self.pack([Request(path=path, text="", pieces=pieces)])
        self.assertIn("Lines 2–3", text)
        self.assertIn("Lines 9–9", text)
        self.assertIn("```````python", text)
        self.assertIn("Source has no final newline", text)
        self.assertNotIn("disk", text)

    def test_line_endings_empty_and_final_newline(self):
        for content, count in [("", 0), ("a", 1), ("a\n", 1), ("a\r\nb\r", 2), ("\n", 1)]:
            self.assertEqual(line_count(content), count)
        self.assertEqual(code_block("a\r\nb\r"), "```\na\nb\n```\n")
        self.assertEqual(code_block(""), "```\n```\n")

    def test_sensitive_globs_and_env_examples(self):
        options = Options()
        for name in (".env", ".env.local", "cert.pem", "id_rsa", ".aws/config", "node_modules/a.js"):
            self.assertIsNotNone(excluded(os.path.join(self.root, name), self.root, options), name)
        self.assertIsNone(excluded(os.path.join(self.root, ".env.example"), self.root, options))
        options.allow_env_examples = False
        self.assertIsNotNone(excluded(os.path.join(self.root, ".env.example"), self.root, options))
        options.exclude_globs = []
        self.assertIsNotNone(excluded(os.path.join(self.root, ".git/config"), self.root, options))

    def test_sensitive_project_root_cannot_bypass_directory_exclusion(self):
        for name in (".ssh", ".env", "node_modules"):
            root = os.path.join(self.root, name)
            self.assertIsNotNone(excluded(os.path.join(root, "config"), root, Options()))

    def test_sensitive_ancestors_and_additive_user_globs(self):
        options = Options.from_dict({"exclude_globs": ["custom-secret.txt"], "extra_exclude_globs": ["private/*"]})
        names = [".npmrc", ".pypirc", ".netrc", ".git-credentials", ".envrc", "prod.env",
                 "id_ecdsa", "id_dsa.pub", "key.ppk", "db.kdbx", "sign.jks", "app.keystore",
                 "main.tfstate.backup", "main.tfvars", ".htpasswd", "custom-secret.txt", "private/a.txt"]
        for name in names:
            self.assertIsNotNone(excluded(os.path.join(self.root, name), self.root, options), name)
        for parent in (".ssh", ".aws", ".kube", ".gnupg"):
            root = os.path.join(self.root, parent, "nested-project")
            self.assertIsNotNone(excluded(os.path.join(root, "config"), root, options), parent)
        self.assertIsNone(excluded(os.path.join(self.root, ".env.example"), self.root, options))

    def test_missing_git_with_marker_fails_closed_for_disk_and_buffer(self):
        Path(self.root, ".git").mkdir(exist_ok=True)
        path = self.write("secret.local", "DISK_SECRET")
        self.write(".gitignore", "secret.local\n")
        with mock.patch("core.subprocess.Popen", side_effect=FileNotFoundError("Git missing")):
            with self.assertRaises(GitError):
                git_root(self.root, Options())
            text = self.pack([Request(path=path)])
            self.assertNotIn("DISK_SECRET", text)
            self.assertIn("ignore rules could not be verified", text)
            text = self.pack([Request(path=path, text="BUFFER_SECRET", modified=True)])
            self.assertNotIn("BUFFER_SECRET", text)
        # Explicitly disabling Git ignore checks remains an opt-out.
        with mock.patch("core.git_root", side_effect=GitError("Git missing")):
            text = self.pack([Request(path=path)], options=Options(respect_gitignore=False))
            self.assertIn("DISK_SECRET", text)

    def test_git_failure_during_ignore_check_fails_closed(self):
        Path(self.root, ".git").mkdir(exist_ok=True)
        path = self.write("secret.local", "SECRET_CONTENT")
        with mock.patch("core.git_root", return_value=self.root), mock.patch("core.ignored_paths", side_effect=GitError("Git timed out")):
            text = self.pack([Request(path=path)])
        self.assertNotIn("SECRET_CONTENT", text)
        self.assertIn("ignore verification failed", text)

    def test_discovery_skipped_labels_are_relative(self):
        self.write("node_modules/secret.txt", "SECRET_CONTENT")
        self.write(".env", "SECRET_CONTENT")
        collector = Collector([self.root])
        requests = collector.expand([self.root])
        text = collector.build(requests)
        self.assertNotIn(self.root, text)
        self.assertNotIn(self.root.replace("\\", "/"), text)
        self.assertNotIn("SECRET_CONTENT", text)
        self.assertTrue(collector.skipped)
        self.assertTrue(all(not os.path.isabs(label) for label, reason in collector.skipped))

    def test_path_alias_difference_is_conservatively_rejected(self):
        path = os.path.join(self.root, "file.txt")
        with mock.patch("core.os.path.realpath", return_value=os.path.join(self.root, "resolved", "file.txt")):
            self.assertTrue(has_symlink(path))

    def test_git_calls_disable_fsmonitor_without_a_shell(self):
        process = mock.Mock(returncode=0)
        with mock.patch("core.subprocess.Popen", return_value=process) as popen:
            self.assertEqual(git_run(self.root, ["status", "--porcelain"], Options()), b"")
        argv = popen.call_args[0][0]
        self.assertIn("core.fsmonitor=false", argv)
        self.assertEqual(argv[argv.index("core.fsmonitor=false") - 1], "-c")
        self.assertIs(popen.call_args[1]["shell"], False)

    def test_binary_and_unknown_encoding_skipped(self):
        paths = [self.write("binary.bin", b"hello\0world"), self.write("legacy.txt", b"\xff\xfe")]
        text = self.pack([Request(path=p) for p in paths])
        self.assertIn("binary/control", text)
        self.assertIn("not UTF", text)
        self.assertIn("0 item(s)", text)

    def test_unicode_byte_limit_and_whole_item_skip(self):
        text = self.pack([Request(label="untitled", text="λ" * 10)], options=Options(max_file_bytes=10))
        self.assertIn("max\\_file\\_bytes", text)
        self.assertNotIn("λ", text)

    def test_selected_bytes_are_bounded_and_binary_checked(self):
        request = Request(label="untitled", text="", pieces=[Piece("x" * 20, 1, 1)])
        self.assertIn("0 item(s)", self.pack([request], options=Options(max_file_bytes=10)))
        request.pieces = [Piece("\0", 1, 1)]
        self.assertIn("binary/control", self.pack([request]))

    def test_pack_budget_never_slices_code(self):
        options = Options(max_pack_bytes=4096, max_file_bytes=4096)
        text = self.pack([Request(label="first", text="x" * 900), Request(label="second", text="y" * 900)], options=options)
        self.assertIn("x" * 900, text)
        self.assertNotIn("y" * 10, text)
        self.assertLessEqual(len(text.encode("utf-8")), 4096)

    def test_skipped_report_is_also_bounded(self):
        text = self.pack([Request(path=os.path.join(self.root, ".env." + str(n))) for n in range(1000)], options=Options(max_pack_bytes=4096))
        self.assertLessEqual(len(text.encode("utf-8")), 4096)
        self.assertIn("more skipped entries omitted", text)

    def test_native_exclusions_relative_to_nested_project(self):
        path = self.write("nested/docs/a.txt", "hidden")
        collector = Collector([self.root], native_patterns={os.path.join(self.root, "nested"): ["docs/*"]})
        self.assertIn("Sublime project/preferences", collector.build([Request(path=path)]))
        self.assertNotIn("hidden", collector.build([Request(path=path)]))

    def test_duplicates_and_max_files(self):
        a = self.write("a.txt", "first")
        b = self.write("b.txt", "second")
        text = self.pack([Request(path=a), Request(path=a), Request(path=b)], options=Options(max_files=1))
        self.assertEqual(text.count("## a\\.txt"), 1)
        self.assertIn("max\\_files", text)

    def test_discovery_budget_includes_empty_directories(self):
        for n in range(20):
            Path(self.root, "directory" + str(n)).mkdir()
        collector = Collector([self.root], Options(max_scan_files=3))
        self.assertEqual(collector.expand([self.root]), [])
        self.assertTrue(collector.notes)

    def test_nonrepo_discovery_does_not_spawn_git_per_directory(self):
        self.write("a/b/c.txt", "hello")
        with mock.patch("core.git_root") as finder:
            collector = Collector([self.root])
            requests = collector.expand([self.root])
            for request in requests:
                collector.root_for(request.path)
            finder.assert_not_called()

    def test_symlink_rejected_and_not_traversed(self):
        target = self.write("real.txt", "secret")
        link = os.path.join(self.root, "alias.txt")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertIn("symlink or junction", self.pack([Request(path=link)]))
        self.assertNotIn("secret", self.pack([Request(path=link)]))

    def test_path_boundary_is_not_a_string_prefix(self):
        self.assertFalse(inside(self.root + "-other/a", self.root))


@unittest.skipUnless(shutil.which("git"), "Git required")
class GitTests(CoreTests):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "user.email", "context-pack@example.invalid")
        self.git("config", "user.name", "Context Pack Tests")

    def git(self, *args):
        return subprocess.check_output(
            ["git"] + list(args), cwd=self.root, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "fixture")

    def test_tracked_picker_changed_paths_and_ignore(self):
        tracked = self.write("tracked.txt", "before\n")
        self.write(".gitignore", "ignored.txt\n")
        self.commit()
        self.write("tracked.txt", "after\n")
        untracked = self.write("new.txt", "new\n")
        ignored = self.write("ignored.txt", "secret\n")
        options = Options()
        self.assertIn(tracked, git_paths(self.root, options))
        self.assertNotIn(untracked, git_paths(self.root, options))
        changed = git_paths(self.root, options, changed=True)
        self.assertIn(tracked, changed)
        self.assertIn(untracked, changed)
        self.assertNotIn(ignored, changed)
        self.assertEqual(ignored_paths(self.root, [ignored], options), {canonical(ignored)})

    def test_git_ignored_file_explicitly_selected_still_skipped(self):
        self.write(".gitignore", "ignored.txt\n")
        path = self.write("ignored.txt", "private content")
        self.assertNotIn("private content", self.pack([Request(path=path)]))
        self.assertIn("Git ignored", self.pack([Request(path=path)]))

    def test_staged_unstaged_and_deleted_diffs(self):
        path = self.write("file.txt", "original\n")
        deleted = self.write("deleted.txt", "gone\n")
        self.commit()
        self.write("file.txt", "staged\n")
        self.git("add", "file.txt")
        self.write("file.txt", "unstaged\n")
        os.remove(deleted)
        text = self.pack([Request(path=path), Request(path=deleted)], include_diff=True)
        self.assertIn("-original", text)
        self.assertIn("+unstaged", text)
        self.assertIn("-gone", text)
        self.assertIn("Missing on disk", text)

    def test_unborn_repository(self):
        path = self.write("new.txt", "new\n")
        self.git("add", "new.txt")
        self.assertIn(path, git_paths(self.root, Options(), changed=True))
        self.assertIn("+new", git_diff(self.root, path, Options()))

    def test_diff_pathspec_is_literal(self):
        literal = self.write("[a].txt", "literal\n")
        other = self.write("a.txt", "other\n")
        self.commit()
        self.write("[a].txt", "changed literal\n")
        self.write("a.txt", "changed other\n")
        diff = git_diff(self.root, literal, Options())
        self.assertIn("+changed literal", diff)
        self.assertNotIn("changed other", diff)

    def test_sensitive_changed_file_has_no_diff(self):
        path = self.write(".env", "OLD_SECRET\n")
        self.commit()
        self.write(".env", "NEW_SECRET\n")
        text = self.pack([Request(path=path)], include_diff=True)
        self.assertNotIn("OLD_SECRET", text)
        self.assertNotIn("NEW_SECRET", text)

    def test_diff_limit_does_not_fall_back_to_staged_diff(self):
        path = self.write("file.txt", "before\n")
        self.commit()
        self.write("file.txt", "x" * 1000)
        with self.assertRaises(GitError):
            git_diff(self.root, path, Options(max_file_bytes=40))

    def test_nonrepo_discovery_does_not_spawn_git_per_directory(self):
        # This fixture deliberately is a repository; the base test covers fallback.
        self.assertEqual(canonical(git_root(self.root, Options())), canonical(self.root))


if __name__ == "__main__":
    unittest.main()
