"""Meaningful save/routing tests without an installed Sublime process."""
import importlib
import json
import os
import plistlib
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(__file__))


class Region:
    def __init__(self, a, b=None):
        self.a, self.b = a, a if b is None else b


class Settings(dict):
    def set(self, key, value):
        self[key] = value

    def erase(self, key):
        self.pop(key, None)


class View:
    def __init__(self, path, text, plugin):
        self.path, self.text, self.plugin = path, text, plugin
        self.values, self.status = Settings(), {}
        self.dirty = True
        self.commands = []
        self.closed = False
        self.read_only = False
        self._window = None
        self.undo_baseline = text

    def settings(self):
        return self.values

    def file_name(self):
        return self.path

    def substr(self, region):
        return self.text[region.a:region.b]

    def size(self):
        return len(self.text)

    def id(self):
        return id(self)

    def buffer_id(self):
        return id(self)

    def is_dirty(self):
        return self.dirty

    def is_valid(self):
        return not self.closed

    def is_loading(self):
        return False

    def set_read_only(self, value):
        self.read_only = value

    def is_read_only(self):
        return self.read_only

    def window(self):
        return self._window

    def clones(self):
        return []

    def set_scratch(self, value):
        self.scratch = value

    def close(self):
        self.closed = True
        return True

    def retarget(self, path):
        self.path = path

    def match_selector(self, *args):
        return False

    def set_status(self, key, text):
        self.status[key] = text

    def erase_status(self, key):
        self.status.pop(key, None)

    def set_name(self, name):
        self.name = name

    def set_encoding(self, encoding):
        self.encoding = encoding

    def run_command(self, command, args=None):
        self.commands.append((command, args))
        if command == "save":
            listener = self.plugin.PlistToolsListener()
            listener.on_pre_save(self)
            with open(self.path, "w", encoding="utf-8", newline="") as handle:
                handle.write(self.text)
            self.dirty = False
            listener.on_post_save(self)
        elif command == "plist_tools_mark_dirty":
            self.dirty = True
        elif command == "revert":
            with open(self.path, encoding="utf-8") as handle:
                self.text = handle.read()
            self.dirty = False
        elif command == "plist_tools_replace":
            self.text = args["text"]
            self.dirty = True
        elif command == "undo":
            self.text = self.undo_baseline
        elif command == "plist_tools_convert":
            action = self.plugin.PlistToolsConvertCommand()
            action.view = self
            action.run(None, **args)


class Window:
    def __init__(self, plugin, original):
        self.plugin = plugin
        self._views = [original]
        self.active = original
        self.index = (1, 2)
        original._window = self

    def views(self):
        return [view for view in self._views if view.is_valid()]

    def active_view(self):
        return self.active

    def get_view_index(self, view):
        return self.index

    def set_view_index(self, view, group, index):
        self.index = (group, index)

    def focus_view(self, view):
        self.active = view

    def open_file(self, path, group=-1):
        with open(path, encoding="utf-8", newline="") as handle:
            view = View(path, handle.read(), self.plugin)
        view.dirty = False
        view._window = self
        self._views.append(view)
        return view


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sublime = types.ModuleType("sublime")
        sublime.Region = Region
        sublime.error_message = mock.Mock()
        sublime.status_message = mock.Mock()
        sublime.load_settings = lambda _: Settings({"live_validation": False})
        sublime.windows = lambda: []
        sublime.set_timeout = lambda callback, delay=0: callback()
        sublime_plugin = types.ModuleType("sublime_plugin")
        for name in ("TextCommand", "WindowCommand", "EventListener"):
            setattr(sublime_plugin, name, type(name, (), {}))
        cls.module_patch = mock.patch.dict(sys.modules, {"sublime": sublime, "sublime_plugin": sublime_plugin})
        cls.module_patch.start()
        package = types.ModuleType("plisttools_test")
        package.__path__ = [ROOT]
        sys.modules[package.__name__] = package
        cls.plugin = importlib.import_module("plisttools_test.plist_tools")
        cls.storage = importlib.import_module("plisttools_test.plist_storage")

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()
        for key in list(sys.modules):
            if key.startswith("plisttools_test"):
                del sys.modules[key]

    def setUp(self):
        self.plugin._recovery_initialized.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.recovery_patch = mock.patch.object(self.plugin, "_recovery_dir", return_value=os.path.join(self.temp.name, "recovery"))
        self.recovery_patch.start()
        self.addCleanup(self.recovery_patch.stop)
        self.source = os.path.join(self.temp.name, "source.plist")
        self.work = os.path.join(self.temp.name, "working.plist")
        self.meta = os.path.join(self.temp.name, "working.json")
        with open(self.source, "wb") as handle:
            handle.write(plistlib.dumps({"old": True}, fmt=plistlib.FMT_BINARY))
        xml = plistlib.dumps({"edited": [7, b"data", False]}, sort_keys=False).decode()
        with open(self.work, "w", encoding="utf-8", newline="") as handle:
            handle.write(xml)
        self.view = View(self.work, xml, self.plugin)
        self.view.values[self.plugin.SOURCE] = self.source
        self.view.values[self.plugin.META] = self.meta
        self.storage.write_metadata(self.meta, {"source": self.source, "format": "binary",
            "fingerprint": self.storage.fingerprint(self.source), "pending": False, "saved_text_hash": "initial"})

    def source_value(self):
        with open(self.source, "rb") as handle:
            return plistlib.load(handle)

    def test_save_commits_binary_then_native_working_copy(self):
        self.assertTrue(self.plugin._save(self.view))
        self.assertEqual({"edited": [7, b"data", False]}, self.source_value())
        self.assertFalse(self.view.is_dirty())
        self.assertEqual(self.work, self.view.file_name())
        self.assertFalse(self.plugin._meta(self.view)["pending"])
        self.assertEqual("save", self.view.commands[0][0])

    def test_invalid_save_keeps_original_and_dirty_recovery(self):
        self.view.text = '<plist><dict><key>missing</key></dict></plist>'
        self.assertFalse(self.plugin._save(self.view))
        self.assertEqual({"old": True}, self.source_value())
        self.assertTrue(self.view.is_dirty())
        self.assertEqual([], self.view.commands)
        self.assertTrue(self.plugin._meta(self.view)["pending"])
        with open(self.plugin._snapshot_path(self.view), encoding="utf-8") as handle:
            self.assertEqual(self.view.text, handle.read())

    def test_external_source_change_blocks_save_and_retains_edits(self):
        with open(self.source, "wb") as handle:
            handle.write(plistlib.dumps({"external": 9}, fmt=plistlib.FMT_BINARY))
        self.assertFalse(self.plugin._save(self.view))
        self.assertEqual({"external": 9}, self.source_value())
        self.assertTrue(self.view.is_dirty())
        self.assertTrue(self.plugin._meta(self.view)["pending"])

    def test_commit_failure_never_native_saves_or_cleans_buffer(self):
        original_write = self.plugin.atomic_write

        def failing(path, data, expected):
            if path == self.source:
                raise PermissionError("source is locked")
            return original_write(path, data, expected)

        with mock.patch.object(self.plugin, "atomic_write", side_effect=failing):
            self.assertFalse(self.plugin._save(self.view))
        self.assertEqual({"old": True}, self.source_value())
        self.assertTrue(self.view.is_dirty())
        self.assertEqual([], self.view.commands)

    def test_native_close_save_failure_retains_pending_working_copy(self):
        self.view.text = "unfinished XML"
        self.view.run_command("save")  # models save bypassing on_text_command
        self.assertEqual({"old": True}, self.source_value())
        self.assertTrue(self.view.is_dirty())
        self.assertTrue(self.plugin._meta(self.view)["pending"])

    def test_save_saveas_revert_routing_and_internal_bypass(self):
        listener = self.plugin.PlistToolsListener()
        self.assertEqual(("plist_tools_save", {}), listener.on_text_command(self.view, "save", None))
        self.assertEqual(("plist_tools_save_as", {}), listener.on_text_command(self.view, "save", {"prompt": True}))
        self.assertEqual(("plist_tools_save_as", {}), listener.on_text_command(self.view, "prompt_save_as", None))
        self.assertEqual(("plist_tools_revert", {}), listener.on_text_command(self.view, "revert", None))
        self.view.values[self.plugin.INTERNAL] = True
        self.assertIsNone(listener.on_text_command(self.view, "save", None))

    def test_undo_redo_do_not_write_source(self):
        listener = self.plugin.PlistToolsListener()
        for command in ("undo", "redo"):
            self.assertIsNone(listener.on_text_command(self.view, command, None))
        self.assertEqual({"old": True}, self.source_value())

    def test_save_all_intercept_only_for_managed_views(self):
        listener = self.plugin.PlistToolsListener()
        window = types.SimpleNamespace(views=lambda: [self.view], active_view=lambda: self.view)
        self.assertEqual(("plist_tools_save_all", {}), listener.on_window_command(window, "save_all", None))
        self.view.values.pop(self.plugin.SOURCE)
        self.assertIsNone(listener.on_window_command(window, "save_all", None))

    def test_conversion_changes_encoding_without_retargeting_original(self):
        self.plugin._save_source(self.view, output_format="xml")
        with open(self.source, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"<?xml"))
        self.assertEqual("xml", self.plugin._meta(self.view)["format"])
        self.assertEqual(self.work, self.view.file_name())

    def test_open_detaches_before_loading_xml_and_reopens_comments(self):
        raw = View(self.source, "raw binary bytes", self.plugin)
        raw.dirty = False
        window = Window(self.plugin, raw)
        recovery = os.path.join(self.temp.name, "recovery")
        os.mkdir(recovery)
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=recovery):
            self.plugin._prepare(raw)
            working = window.active_view()
            self.assertTrue(raw.closed)
            self.assertEqual(self.source, raw.file_name())
            self.assertEqual("raw binary bytes", raw.text)
            self.assertEqual([], raw.commands)
            self.assertNotEqual(raw.buffer_id(), working.buffer_id())
            self.assertEqual(self.source, working.values[self.plugin.SOURCE])
            self.assertTrue(working.text.startswith("<?xml"))
            self.assertFalse(working.dirty)
            working.text = working.text.replace("<dict>", "<dict><!-- keep -->")
            working.dirty = True
            self.assertTrue(self.plugin._save(working))
            reopened = View(self.source, "raw binary bytes", self.plugin)
            reopened.dirty = False
            second_window = Window(self.plugin, reopened)
            self.plugin._prepare(reopened)
        self.assertIn("<!-- keep -->", second_window.active_view().text)
        self.assertEqual((1, 2), window.index)
        with open(self.source, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"bplist00"))

    def test_session_restore_reattaches_without_overwriting_unsaved_text(self):
        self.view.values.clear()
        self.view.text = "unsaved hot exit text"
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=self.temp.name):
            self.plugin._prepare(self.view)
        self.assertEqual(self.source, self.view.values[self.plugin.SOURCE])
        self.assertEqual("unsaved hot exit text", self.view.text)
        self.assertTrue(self.view.dirty)

    def test_modified_raw_binary_is_not_replaced(self):
        raw = View(self.source, "unsaved raw bytes", self.plugin)
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=self.temp.name):
            self.plugin._prepare(raw)
        self.assertEqual("unsaved raw bytes", raw.text)
        self.assertEqual(self.source, raw.file_name())
        self.assertTrue(raw.read_only)

    def test_pending_snapshot_restore_is_visible_and_preserves_failed_edit(self):
        self.view.text = "unfinished XML"
        self.assertFalse(self.plugin._save(self.view))
        with open(self.work, encoding="utf-8") as handle:
            reopened = View(self.work, handle.read(), self.plugin)
        reopened.dirty = False
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=self.temp.name):
            self.plugin._prepare(reopened)
        self.assertEqual("unfinished XML", reopened.text)
        self.assertIn("pending", reopened.name)
        self.assertTrue(reopened.dirty)

    def test_activation_and_clone_do_not_reapply_failed_save_after_undo(self):
        baseline = self.view.text
        self.view.text = "unfinished XML"
        self.assertFalse(self.plugin._save(self.view))
        # Undo returns the shared buffer to its clean recovery-file revision.
        self.view.text = baseline
        self.view.dirty = False
        clone = View(self.work, baseline, self.plugin)
        clone.dirty = False
        listener = self.plugin.PlistToolsListener()
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=self.temp.name), \
                mock.patch.object(clone, "buffer_id", return_value=self.view.buffer_id()):
            listener.on_activated(self.view)
            listener.on_activated(self.view)
            listener.on_clone(clone)
        self.assertEqual(baseline, self.view.text)
        self.assertFalse(self.view.dirty)
        self.assertEqual(baseline, clone.text)
        self.assertFalse(clone.dirty)
        self.assertEqual(self.source, clone.values[self.plugin.SOURCE])
        self.assertFalse(any(command == "plist_tools_replace" for command, _ in self.view.commands + clone.commands))

    def test_pending_snapshot_does_not_overwrite_dirty_session_restore(self):
        self.view.text = "failed older edit"
        self.assertFalse(self.plugin._save(self.view))
        restored = View(self.work, "newer hot exit edit", self.plugin)
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=self.temp.name):
            self.plugin._prepare(restored)
        self.assertEqual("newer hot exit edit", restored.text)
        self.assertTrue(restored.dirty)
        self.assertEqual(self.source, restored.values[self.plugin.SOURCE])

    def test_clean_pathless_session_restores_pending_snapshot_after_rebinding(self):
        baseline = self.view.text
        self.view.text = "pending pathless edit"
        self.assertFalse(self.plugin._save(self.view))
        self.view.path = None
        self.view.text = baseline
        self.view.dirty = False
        self.plugin._recovery_initialized.clear()  # models a plugin/session reload
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=self.temp.name):
            self.plugin._prepare(self.view)
        self.assertEqual(self.work, self.view.path)
        self.assertEqual("pending pathless edit", self.view.text)
        self.assertTrue(self.view.dirty)
        self.assertEqual(self.source, self.view.values[self.plugin.SOURCE])

    def test_lazy_source_binding_does_not_suppress_initial_pending_restore(self):
        baseline = self.view.text
        self.view.text = "pending lazy-bound edit"
        self.assertFalse(self.plugin._save(self.view))
        restored = View(self.work, baseline, self.plugin)
        restored.dirty = False
        self.plugin._recovery_initialized.clear()
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=self.temp.name):
            self.assertEqual(self.source, self.plugin._source(restored))
            self.plugin._prepare(restored)
        self.assertEqual("pending lazy-bound edit", restored.text)
        self.assertTrue(restored.dirty)

    def test_clone_without_settings_routes_save_to_binary_source(self):
        clone = View(self.work, self.view.text, self.plugin)
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=self.temp.name):
            result = self.plugin.PlistToolsListener().on_text_command(clone, "save", None)
        self.assertEqual(("plist_tools_save", {}), result)
        self.assertEqual(self.source, clone.values[self.plugin.SOURCE])

    def test_missing_ordinary_text_file_is_ignored(self):
        for suffix in (".txt", ".plist", ".entitlements", ".mobileconfig"):
            with self.subTest(suffix=suffix):
                ordinary = View(os.path.join(self.temp.name, "deleted" + suffix), "my edits", self.plugin)
                with mock.patch.object(self.plugin, "_error") as error:
                    self.plugin._prepare(ordinary)
                error.assert_not_called()
                self.assertFalse(ordinary.read_only)

    def test_unreadable_binary_identity_error_is_not_repeated(self):
        raw = View(self.source, "raw bytes", self.plugin)
        raw.dirty = False
        with mock.patch.object(self.plugin, "fingerprint", side_effect=OSError("Unsupported link")), \
                mock.patch.object(self.plugin, "_error") as error:
            self.plugin._prepare(raw)
            self.plugin._prepare(raw)
        self.assertEqual(1, error.call_count)
        self.assertTrue(raw.read_only)

    def test_initial_conversion_rejects_clones_before_creating_recovery(self):
        path = os.path.join(self.temp.name, "cloned.plist")
        xml = plistlib.dumps({"value": 1}).decode()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(xml)
        original = View(path, xml, self.plugin)
        Window(self.plugin, original)
        with mock.patch.object(original, "clones", return_value=[original]), \
                mock.patch.object(self.plugin, "_paths") as paths, \
                mock.patch.object(self.plugin, "_error") as error:
            original.run_command("plist_tools_convert", {"output_format": "binary"})
        paths.assert_not_called()
        error.assert_called_once()
        self.assertFalse(original.closed)
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(xml, handle.read())

    def test_rejected_binary_error_is_memoized(self):
        with open(self.source, "wb") as handle:
            handle.write(plistlib.dumps(plistlib.UID(7), fmt=plistlib.FMT_BINARY))
        raw = View(self.source, "raw", self.plugin)
        raw.dirty = False
        with mock.patch.object(self.plugin, "_error") as error:
            self.plugin._prepare(raw)
            self.plugin._prepare(raw)
        self.assertEqual(1, error.call_count)
        self.assertTrue(raw.read_only)

    def test_unexpected_retarget_never_commits_old_source(self):
        self.view.path = self.source
        self.view.run_command("save")
        self.assertEqual({"old": True}, self.source_value())
        self.assertEqual(self.work, self.view.path)
        self.assertNotIn(self.plugin.SOURCE, self.view.values)

    def test_pathless_session_preserves_edits_and_recovers_working_target(self):
        self.view.path = None
        self.view.text = "unsaved restored XML"
        self.plugin._prepare(self.view)
        self.assertEqual(self.work, self.view.path)
        self.assertEqual("unsaved restored XML", self.view.text)
        self.assertTrue(self.view.dirty)

    def test_native_prompt_save_as_window_command_is_intercepted(self):
        window = types.SimpleNamespace(views=lambda: [self.view], active_view=lambda: self.view)
        result = self.plugin.PlistToolsListener().on_window_command(window, "prompt_save_as", None)
        self.assertEqual(("plist_tools_window_save", {"save_as": True}), result)

    def test_fresh_xml_buffer_has_no_raw_binary_undo_history(self):
        raw = View(self.source, "6270 6c69 7374 3030 RAW HEX", self.plugin)
        raw.dirty = False
        window = Window(self.plugin, raw)
        os.makedirs(os.path.join(self.temp.name, "recovery"))
        self.plugin._prepare(raw)
        working = window.active_view()
        self.assertIsNot(raw, working)
        working.text = working.text.replace("<true/>", "<false/>")
        working.dirty = True
        self.assertTrue(self.plugin._save(working))
        working.run_command("undo")
        self.assertTrue(working.text.startswith("<?xml"))
        self.assertEqual(working.path, self.plugin._working_path(working))
        self.assertEqual([], raw.commands)
        self.assertTrue(raw.closed)

    def test_initial_xml_conversion_uses_fresh_view_before_binary_commit(self):
        xml_path = os.path.join(self.temp.name, "original-xml.plist")
        xml = plistlib.dumps({"version": 7}).decode()
        with open(xml_path, "w", encoding="utf-8") as handle:
            handle.write(xml)
        original = View(xml_path, xml.replace("7", "9"), self.plugin)
        window = Window(self.plugin, original)
        os.makedirs(os.path.join(self.temp.name, "recovery"))
        original.run_command("plist_tools_convert", {"output_format": "binary"})
        working = window.active_view()
        self.assertTrue(original.closed)
        self.assertIsNot(original, working)
        self.assertEqual(xml_path, original.path)
        self.assertTrue(working.undo_baseline.startswith("<?xml"))
        with open(xml_path, "rb") as handle:
            self.assertEqual({"version": 9}, plistlib.load(handle))
        self.assertEqual(xml_path, working.values[self.plugin.SOURCE])

    def test_xml_conversion_does_not_require_safe_binary_serialization(self):
        xml_path = os.path.join(self.temp.name, "signed-zero.plist")
        xml = plistlib.dumps([0.0, -0.0]).decode()
        with open(xml_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(xml)
        original = View(xml_path, xml, self.plugin)
        window = Window(self.plugin, original)
        os.makedirs(os.path.join(self.temp.name, "recovery"))
        with mock.patch.object(self.plugin, "binary_from_xml", side_effect=AssertionError("XML must not validate through binary")) as binary:
            original.run_command("plist_tools_convert", {"output_format": "xml"})
        binary.assert_not_called()
        self.assertTrue(original.closed)
        working = window.active_view()
        self.assertEqual("xml", self.plugin._meta(working)["format"])
        with open(xml_path, "rb") as handle:
            self.assertEqual(xml.encode("utf-8"), handle.read())

    def test_crlf_xml_revert_and_save_leave_no_false_pending_recovery(self):
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict><key>line</key><string>a&#13;b</string></dict>\n</plist>\n'
        with open(self.source, "wb") as handle:
            handle.write(xml.replace("\n", "\r\n").encode("utf-8"))
        self.view.dirty = False
        action = self.plugin.PlistToolsRevertCommand()
        action.view = self.view
        action.run(None)
        self.assertEqual(xml, self.view.text)
        with open(self.work, "rb") as handle:
            self.assertEqual(xml.encode("utf-8"), handle.read())
        self.assertTrue(self.plugin._save(self.view))
        self.assertEqual(self.storage.text_hash(self.view.text), self.plugin._meta(self.view)["saved_text_hash"])
        self.assertEqual({"line": "a\rb"}, self.source_value())
        recovery = self.plugin.PlistToolsRecoverCommand()
        recovery.window = types.SimpleNamespace(show_quick_panel=mock.Mock())
        with mock.patch.object(self.plugin, "_recovery_dir", return_value=self.temp.name):
            recovery.run()
        recovery.window.show_quick_panel.assert_not_called()


    def test_revert_rejects_original_source_retarget_before_writing(self):
        before = self.storage.fingerprint(self.source)
        self.view.path = self.source
        action = self.plugin.PlistToolsRevertCommand()
        action.view = self.view
        with mock.patch.object(self.plugin.sublime, "ok_cancel_dialog", create=True) as confirm, \
                mock.patch.object(self.plugin, "_error") as error:
            action.run(None)
        confirm.assert_not_called()
        error.assert_called_once()
        self.assertEqual(before, self.storage.fingerprint(self.source))
        self.assertEqual(self.work, self.view.path)
        self.assertNotIn(self.plugin.SOURCE, self.view.values)
        self.assertTrue(self.view.dirty)
        self.assertEqual([], self.view.commands)

    def test_revert_rejects_unrelated_retarget_before_writing(self):
        unrelated = os.path.join(self.temp.name, "unrelated.txt")
        with open(unrelated, "wb") as handle:
            handle.write(b"keep this file")
        before = self.storage.fingerprint(unrelated)
        source_before = self.storage.fingerprint(self.source)
        self.view.path = unrelated
        action = self.plugin.PlistToolsRevertCommand()
        action.view = self.view
        with mock.patch.object(self.plugin.sublime, "ok_cancel_dialog", create=True) as confirm, \
                mock.patch.object(self.plugin, "_error") as error:
            action.run(None)
        confirm.assert_not_called()
        error.assert_called_once()
        self.assertEqual(before, self.storage.fingerprint(unrelated))
        self.assertEqual(source_before, self.storage.fingerprint(self.source))
        self.assertEqual(unrelated, self.view.path)
        self.assertNotIn(self.plugin.SOURCE, self.view.values)
        self.assertTrue(self.view.dirty)

    def test_pathless_revert_uses_working_path_and_clears_failed_save_state(self):
        self.view.text = "unfinished XML"
        self.assertFalse(self.plugin._save(self.view))
        self.view.path = None
        self.plugin._failed_save[self.view.buffer_id()] = "old native save error"
        before = self.storage.fingerprint(self.source)
        action = self.plugin.PlistToolsRevertCommand()
        action.view = self.view
        with mock.patch.object(self.plugin.sublime, "ok_cancel_dialog", return_value=True, create=True):
            action.run(None)
        self.assertEqual(self.work, self.view.path)
        self.assertEqual(before, self.storage.fingerprint(self.source))
        self.assertEqual({"old": True}, plistlib.loads(self.view.text.encode("utf-8")))
        self.assertFalse(self.view.dirty)
        self.assertNotIn("plist_tools_save_error", self.view.status)
        self.assertNotIn(self.view.buffer_id(), self.plugin._failed_save)
        meta = self.plugin._meta(self.view)
        self.assertFalse(meta["pending"])
        self.assertFalse(meta["working_pending"])
        self.assertNotIn("last_error", meta)

    def test_revert_prompts_for_clean_working_pending_snapshot(self):
        self.view.dirty = False
        meta = self.plugin._meta(self.view)
        meta["working_pending"] = True
        self.storage.write_metadata(self.meta, meta)
        before = self.storage.fingerprint(self.work)
        action = self.plugin.PlistToolsRevertCommand()
        action.view = self.view
        with mock.patch.object(self.plugin.sublime, "ok_cancel_dialog", return_value=False, create=True) as confirm:
            action.run(None)
        confirm.assert_called_once()
        self.assertEqual(before, self.storage.fingerprint(self.work))
        self.assertEqual([], self.view.commands)

    def test_revert_reports_invalid_metadata_without_discarding_edits(self):
        self.view.dirty = False
        before = self.storage.fingerprint(self.source)
        original_text = self.view.text
        with open(self.meta, "w", encoding="utf-8") as handle:
            handle.write("[]")
        action = self.plugin.PlistToolsRevertCommand()
        action.view = self.view
        with mock.patch.object(self.plugin.sublime, "ok_cancel_dialog", create=True) as confirm, \
                mock.patch.object(self.plugin, "_error") as error:
            action.run(None)
        error.assert_called_once()
        confirm.assert_not_called()
        self.assertEqual(original_text, self.view.text)
        self.assertEqual(before, self.storage.fingerprint(self.source))
        self.assertEqual([], self.view.commands)

    def test_save_all_continues_after_missing_or_invalid_managed_metadata(self):
        valid = self.plugin._meta(self.view)
        before = self.storage.fingerprint(self.source)
        original_text = self.view.text
        for dirty in (False, True):
            for failure in ("missing_setting", "missing_file", "invalid_json", "non_mapping"):
                with self.subTest(dirty=dirty, failure=failure):
                    self.view.dirty = dirty
                    self.view.values[self.plugin.META] = self.meta
                    self.storage.write_metadata(self.meta, valid)
                    if failure == "missing_setting":
                        self.view.values.pop(self.plugin.META)
                    elif failure == "missing_file":
                        os.unlink(self.meta)
                    else:
                        with open(self.meta, "w", encoding="utf-8") as handle:
                            handle.write("{" if failure == "invalid_json" else "[]")
                    ordinary = View(os.path.join(self.temp.name, "ordinary.txt"), "ordinary edits", self.plugin)
                    action = self.plugin.PlistToolsSaveAllCommand()
                    action.window = types.SimpleNamespace(views=lambda: [self.view, ordinary])
                    with mock.patch.object(self.plugin, "_error") as error:
                        action.run()
                    error.assert_called_once()
                    self.assertFalse(ordinary.dirty)
                    with open(ordinary.path, encoding="utf-8") as handle:
                        self.assertEqual("ordinary edits", handle.read())
                    self.assertEqual(original_text, self.view.text)
                    self.assertEqual(dirty, self.view.dirty)
                    self.assertEqual([], self.view.commands)
                    self.assertIn("plist_tools_save_error", self.view.status)
                    self.assertEqual(before, self.storage.fingerprint(self.source))

    def test_save_rejects_malformed_metadata_fields_and_preserves_dirty_text(self):
        valid = self.plugin._meta(self.view)
        invalid = [None, [], {}, dict(valid, source=[]), dict(valid, format="unknown"),
                   dict(valid, fingerprint="invalid"), dict(valid, pending="false")]
        invalid.extend({key: value for key, value in valid.items() if key != missing}
                       for missing in ("source", "format", "fingerprint", "saved_text_hash"))
        before = self.storage.fingerprint(self.source)
        original_text = self.view.text
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                with open(self.meta, "w", encoding="utf-8") as handle:
                    json.dump(metadata, handle)
                with mock.patch.object(self.plugin, "_error"):
                    self.assertFalse(self.plugin._save(self.view))
                self.assertTrue(self.view.dirty)
                self.assertEqual(original_text, self.view.text)
                self.assertEqual([], self.view.commands)
                self.assertEqual(before, self.storage.fingerprint(self.source))

    def test_original_binary_reopen_restores_pending_as_dirty_undoable_edit(self):
        raw = View(self.source, "raw binary bytes", self.plugin)
        raw.dirty = False
        window = Window(self.plugin, raw)
        os.makedirs(os.path.join(self.temp.name, "recovery"))
        before = self.storage.fingerprint(self.source)
        self.plugin._prepare(raw)
        working = window.active_view()
        baseline = working.text
        work_before = self.storage.fingerprint(working.path)
        working.text = "unfinished XML from failed save"
        working.dirty = True
        self.assertFalse(self.plugin._save(working))
        working.close()
        self.plugin.PlistToolsListener().on_close(working)
        reopened = View(self.source, "raw binary bytes", self.plugin)
        reopened.dirty = False
        second_window = Window(self.plugin, reopened)
        self.plugin._prepare(reopened)
        recovered = second_window.active_view()
        self.assertEqual("unfinished XML from failed save", recovered.text)
        self.assertTrue(recovered.dirty)
        self.assertIn("pending", recovered.name)
        self.assertEqual(work_before, self.storage.fingerprint(recovered.path))
        self.assertEqual(before, self.storage.fingerprint(self.source))
        self.assertEqual(1, sum(command == "plist_tools_replace" for command, _ in recovered.commands))
        recovered.run_command("undo")
        self.plugin.PlistToolsListener().on_activated(recovered)
        self.assertEqual(baseline, recovered.text)
        self.assertEqual(1, sum(command == "plist_tools_replace" for command, _ in recovered.commands))

    def test_export_rejects_attached_path_with_windows_case_alias(self):
        target = self.work.swapcase()
        self.view._window = types.SimpleNamespace(show_input_panel=lambda caption, initial, done, changed, cancel: done(target))
        action = self.plugin.PlistToolsSaveAsCommand()
        action.view = self.view
        with mock.patch.object(self.plugin.os.path, "normcase", side_effect=lambda path: path.lower()), \
                mock.patch.object(self.plugin.sublime, "ok_cancel_dialog", create=True) as confirm, \
                mock.patch.object(self.plugin, "atomic_write") as write, \
                mock.patch.object(self.plugin, "_error") as error:
            action.run(None, output_format="binary")
        error.assert_called_once()
        confirm.assert_not_called()
        write.assert_not_called()

    def test_export_rejects_existing_file_identity_alias_of_working_or_source(self):
        alias = os.path.join(self.temp.name, "export-alias.plist")
        self.view._window = types.SimpleNamespace(show_input_panel=lambda caption, initial, done, changed, cancel: done(alias))
        action = self.plugin.PlistToolsSaveAsCommand()
        action.view = self.view
        for target in (self.work, self.source):
            with self.subTest(target=target):
                try:
                    os.link(target, alias)
                except OSError as error:
                    self.skipTest("Filesystem cannot create hard links: {}".format(error))
                try:
                    with mock.patch.object(self.plugin.sublime, "ok_cancel_dialog", create=True) as confirm, \
                            mock.patch.object(self.plugin, "atomic_write") as write, \
                            mock.patch.object(self.plugin, "_error") as error:
                        action.run(None, output_format="binary")
                    error.assert_called_once()
                    confirm.assert_not_called()
                    write.assert_not_called()
                finally:
                    os.unlink(alias)

    def test_export_new_file_remains_a_separate_copy(self):
        target = os.path.join(self.temp.name, "new-copy.plist")
        self.view._window = types.SimpleNamespace(show_input_panel=lambda caption, initial, done, changed, cancel: done(target))
        action = self.plugin.PlistToolsSaveAsCommand()
        action.view = self.view
        before = self.storage.fingerprint(self.source)
        action.run(None, output_format="binary")
        with open(target, "rb") as handle:
            self.assertEqual({"edited": [7, b"data", False]}, plistlib.load(handle))
        self.assertEqual(before, self.storage.fingerprint(self.source))
        self.assertEqual(self.work, self.view.path)



    def test_non_xml_mobileconfig_stays_read_only_with_inspection_hint(self):
        path = os.path.join(self.temp.name, "signed.mobileconfig")
        with open(path, "wb") as handle:
            handle.write(b"signed profile bytes")
        raw = View(path, "signed profile bytes", self.plugin)
        raw.dirty = False
        before = self.storage.fingerprint(path)
        self.plugin._prepare(raw)
        self.assertTrue(raw.read_only)
        self.assertIn("Plist Tools: Inspect Signed Profile", raw.status["plist_tools_error"])
        self.assertEqual(before, self.storage.fingerprint(path))
        self.assertEqual([], raw.commands)

    def test_convert_rejects_read_only_and_signed_original_views_even_when_run_directly(self):
        cases = [(self.work, True, None), (None, True, self.source),
                 (None, False, self.source), (self.source, False, self.source)]
        for path, read_only, signed_source in cases:
            with self.subTest(path=path, read_only=read_only, signed_source=signed_source):
                view = View(path, self.view.text, self.plugin)
                view.read_only = read_only
                if signed_source:
                    view.values["plist_tools_signed_source"] = signed_source
                action = self.plugin.PlistToolsConvertCommand()
                action.view = view
                with mock.patch.object(self.plugin, "atomic_write") as write, \
                        mock.patch.object(self.plugin, "_error") as error:
                    self.assertFalse(action.is_enabled("binary"))
                    action.run(None, output_format="binary")
                error.assert_called_once()
                write.assert_not_called()
                self.assertEqual([], view.commands)

    def test_unsigned_copy_conversion_preserves_signed_origin_export_guard(self):
        path = os.path.join(self.temp.name, "unsigned-copy.mobileconfig")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.view.text)
        unsigned = View(path, self.view.text, self.plugin)
        unsigned.dirty = False
        unsigned.values["plist_tools_signed_source"] = self.source
        window = Window(self.plugin, unsigned)
        os.makedirs(os.path.join(self.temp.name, "recovery"))
        before = self.storage.fingerprint(self.source)
        action = self.plugin.PlistToolsConvertCommand()
        action.view = unsigned
        self.assertTrue(action.is_enabled("binary"))
        action.run(None, output_format="binary")
        working = window.active_view()
        self.assertEqual(path, working.values[self.plugin.SOURCE])
        self.assertEqual(self.source, working.values["plist_tools_signed_source"])
        self.assertEqual(before, self.storage.fingerprint(self.source))
        with open(path, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"bplist00"))

    def test_export_rejects_signed_origin_case_alias_from_detached_inspection(self):
        target = self.source.swapcase()
        view = View(None, self.view.text, self.plugin)
        view.values["plist_tools_signed_source"] = self.source
        view.read_only = True
        view._window = types.SimpleNamespace(show_input_panel=lambda caption, initial, done, changed, cancel: done(target))
        action = self.plugin.PlistToolsSaveAsCommand()
        action.view = view
        with mock.patch.object(self.plugin.os.path, "normcase", side_effect=lambda path: path.lower()), \
                mock.patch.object(self.plugin.sublime, "ok_cancel_dialog", create=True) as confirm, \
                mock.patch.object(self.plugin, "atomic_write") as write, \
                mock.patch.object(self.plugin, "_error") as error:
            action.run(None, output_format="xml")
        error.assert_called_once()
        confirm.assert_not_called()
        write.assert_not_called()



    def test_managed_save_never_commits_to_signed_original(self):
        self.view.values["plist_tools_signed_source"] = self.source
        before = self.storage.fingerprint(self.source)
        text = self.view.text
        with mock.patch.object(self.plugin, "_error") as error:
            self.assertFalse(self.plugin._save(self.view))
        error.assert_called_once()
        self.assertIn("signed original is protected", str(error.call_args[0][0]))
        self.assertEqual(before, self.storage.fingerprint(self.source))
        self.assertEqual(text, self.view.text)
        self.assertTrue(self.view.dirty)
        self.assertEqual([], self.view.commands)
        self.assertTrue(self.plugin._meta(self.view)["pending"])
        with open(self.plugin._snapshot_path(self.view), encoding="utf-8") as handle:
            self.assertEqual(text, handle.read())



if __name__ == "__main__":
    unittest.main()
