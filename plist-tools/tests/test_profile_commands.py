"""Profile command integration with real parsing/storage and small editor doubles."""
import importlib
import json
import os
import plistlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(__file__))
PACKAGE = "plisttools_profile_commands_test"


class Region:
    def __init__(self, a, b=None):
        self.a, self.b = a, a if b is None else b

    def begin(self):
        return min(self.a, self.b)


class Settings(dict):
    def set(self, key, value):
        self[key] = value

    def erase(self, key):
        self.pop(key, None)


class Selections(list):
    def add(self, region):
        self.append(region)


class View:
    def __init__(self, window, text="", path=None):
        self.owner, self.text, self.path = window, text, path
        self.config = Settings()
        self.status = {}
        self.title = ""
        self.read_only = self.scratch = self.dirty = False
        self.count = 0
        self.commands = []
        self.regions = Selections([Region(0)])
        self.shown = []

    def settings(self):
        return self.config

    def window(self):
        return self.owner

    def file_name(self):
        return self.path

    def name(self):
        return self.title

    def set_name(self, name):
        self.title = name

    def size(self):
        return len(self.text)

    def substr(self, region):
        return self.text[region.a:region.b]

    def id(self):
        return id(self)

    def buffer_id(self):
        return id(self)

    def is_valid(self):
        return True

    def is_loading(self):
        return False

    def is_dirty(self):
        return self.dirty and not self.scratch

    def is_read_only(self):
        return self.read_only

    def set_read_only(self, value):
        self.read_only = value

    def set_scratch(self, value):
        self.scratch = value

    def retarget(self, path):
        self.path = path

    def set_encoding(self, encoding):
        self.encoding = encoding

    def set_status(self, key, value):
        self.status[key] = value

    def erase_status(self, key):
        self.status.pop(key, None)

    def match_selector(self, *args):
        return False

    def change_count(self):
        return self.count

    def sel(self):
        return self.regions

    def show_at_center(self, point):
        self.shown.append(point)

    def replace(self, edit, region, text):
        if self.read_only:
            raise AssertionError("A command attempted to modify a read-only view")
        self.text = self.text[:region.a] + text + self.text[region.b:]
        self.count += 1
        self.dirty = True

    def run_command(self, command, args=None):
        self.commands.append((command, args))
        if command == "plist_tools_replace":
            self.replace(None, Region(0, self.size()), args["text"])
        elif command == "plist_tools_export_profile":
            action = self.owner.plugin.PlistToolsExportProfileCommand()
            action.view = self
            action.run(None, **(args or {}))
        elif command == "save":
            # Native close/quit saves can bypass command interception entirely.
            self.owner.plugin.PlistToolsProfileListener().on_pre_save(self)
            Path(self.path).write_bytes(self.text.encode("utf-8"))
            self.dirty = False


class Window:
    def __init__(self, plugin):
        self.plugin = plugin
        self.open_views = []
        self.active = None
        self.inputs = []
        self.panels = []

    def is_valid(self):
        return True

    def active_view(self):
        return self.active

    def new_file(self):
        view = View(self)
        self.open_views.append(view)
        self.active = view
        return view

    def views(self):
        return list(self.open_views)

    def open_file(self, path):
        view = View(self, Path(path).read_text(encoding="utf-8"), path)
        self.open_views.append(view)
        self.active = view
        return view

    def show_input_panel(self, caption, initial, on_done, on_change, on_cancel):
        self.inputs.append((caption, initial, on_done))

    def show_quick_panel(self, items, on_done):
        self.panels.append((items, on_done))


class ProfileCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sublime = types.ModuleType("sublime")
        cls.sublime.Region = Region
        cls.sublime.error_message = mock.Mock()
        cls.sublime.status_message = mock.Mock()
        cls.sublime.ok_cancel_dialog = mock.Mock(return_value=True)
        cls.sublime.load_settings = lambda _: Settings({"live_validation": False})
        cls.sublime.windows = lambda: cls.current_windows
        cls.sublime.packages_path = lambda: cls.packages_root
        cls.sublime.pending = []
        cls.sublime.workers = []
        cls.sublime.set_timeout = lambda callback, delay=0: cls.sublime.pending.append(callback)
        cls.sublime.set_timeout_async = lambda callback, delay=0: cls.sublime.workers.append(callback)
        plugin = types.ModuleType("sublime_plugin")
        for name in ("TextCommand", "WindowCommand", "EventListener"):
            setattr(plugin, name, type(name, (), {}))
        package = types.ModuleType(PACKAGE)
        package.__path__ = [ROOT]
        cls.module_patch = mock.patch.dict(sys.modules, {"sublime": cls.sublime, "sublime_plugin": plugin, PACKAGE: package})
        cls.module_patch.start()
        cls.plugin = importlib.import_module(PACKAGE + ".profile_commands")
        cls.editor = importlib.import_module(PACKAGE + ".plist_tools")

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()
        for key in list(sys.modules):
            if key == PACKAGE or key.startswith(PACKAGE + "."):
                del sys.modules[key]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sublime.pending.clear()
        self.sublime.workers.clear()
        self.sublime.error_message.reset_mock()
        self.sublime.status_message.reset_mock()
        self.sublime.ok_cancel_dialog.reset_mock()
        self.sublime.ok_cancel_dialog.side_effect = None
        self.sublime.ok_cancel_dialog.return_value = True
        self.editor._analysis.clear()
        self.window = Window(self.plugin)
        type(self).current_windows = [self.window]
        type(self).packages_root = str(Path(self.temp.name, "Packages"))
        self.signed = str(Path(self.temp.name, "Original Signed.mobileconfig"))
        self.signed_bytes = b"SIGNED CMS SNAPSHOT\x00original bytes"
        Path(self.signed).write_bytes(self.signed_bytes)
        self.profile = {
            "PayloadType": "Configuration", "PayloadVersion": 1,
            "PayloadIdentifier": "com.example.existing.profile",
            "PayloadUUID": "11111111-1111-4111-8111-111111111111",
            "PayloadDisplayName": "Existing profile", "PayloadScope": "System",
            "PayloadContent": [{
                "PayloadType": "com.example.application", "PayloadVersion": 1,
                "PayloadIdentifier": "com.example.existing.application",
                "PayloadUUID": "22222222-2222-4222-8222-222222222222",
                "UserName": "{{UserPrincipalName}}", "Serial": "$SERIALNUMBER",
                "VendorData": b"\x00\xffopaque", "VendorSetting": ["retain", 7, False],
            }],
        }
        self.xml = plistlib.dumps(self.profile, sort_keys=False).decode("utf-8")
        self.xml = self.xml.replace("<dict>", "<dict><!-- retain deployment comment -->", 1)

    def run_text(self, command_class, view, **args):
        command = command_class()
        command.view = view
        command.run(None, **args)

    def unsigned_view(self):
        return self.plugin._new_view(self.window, self.xml, "Existing profile unsigned.mobileconfig", self.signed)

    def inspect(self):
        raw = self.window.new_file()
        raw.path = self.signed
        raw.text = "raw signed container"
        with mock.patch.object(self.plugin, "decode_signed_profile", return_value=self.xml) as decode:
            self.run_text(self.plugin.PlistToolsInspectSignedProfileCommand, raw)
            self.assertEqual([raw], self.window.open_views)
            self.assertEqual(1, len(self.sublime.workers))
            self.sublime.workers.pop(0)()
            decode.assert_called_once_with(self.signed, openssl_path=None, timeout=15.0, max_bytes=16 * 1024 * 1024)
            self.assertEqual([raw], self.window.open_views)
            while self.sublime.pending:
                self.sublime.pending.pop(0)()
        return raw, self.window.active_view()

    def start_export(self, view, target="generic"):
        self.run_text(self.plugin.PlistToolsExportProfileCommand, view, target=target)
        self.assertTrue(self.window.inputs)
        return self.window.inputs[-1][2]

    def test_signed_inspection_is_detached_readonly_scratch_and_preserves_original(self):
        raw, inspection = self.inspect()
        self.assertIsNot(raw, inspection)
        self.assertIsNone(inspection.file_name())
        self.assertIsNone(self.editor._source(inspection))
        self.assertEqual(self.signed, inspection.config[self.plugin.SIGNED_SOURCE])
        self.assertTrue(inspection.config[self.plugin.INSPECTION])
        self.assertTrue(inspection.read_only)
        self.assertTrue(inspection.scratch)
        self.assertEqual(self.xml, inspection.text)
        self.assertEqual(self.signed, raw.file_name())
        self.assertEqual("raw signed container", raw.text)
        self.assertEqual([], raw.commands)
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())

    def test_unsigned_copy_is_editable_and_retains_text_identities_and_signed_origin(self):
        _, inspection = self.inspect()
        self.run_text(self.plugin.PlistToolsUnsignedProfileCopyCommand, inspection)
        copy = self.window.active_view()
        self.assertIsNot(copy, inspection)
        self.assertFalse(copy.read_only)
        self.assertFalse(copy.scratch)
        self.assertTrue(Path(copy.file_name()).is_file())
        self.assertEqual(self.plugin._drafts_dir(), str(Path(copy.file_name()).parent))
        self.assertNotIn(self.plugin.INSPECTION, copy.config)
        self.assertEqual(self.signed, copy.config[self.plugin.SIGNED_SOURCE])
        self.assertEqual(self.xml, copy.text)
        self.assertEqual(self.profile, self.plugin.parse_xml(copy.text).value)
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())

    def test_native_save_uses_private_copy_and_save_as_requires_explicit_export(self):
        _, inspection = self.inspect()
        self.run_text(self.plugin.PlistToolsUnsignedProfileCopyCommand, inspection)
        copy = self.window.active_view()
        listener = self.plugin.PlistToolsProfileListener()
        for view in (inspection, copy):
            self.window.active = view
            for name, args in (("save", None), ("save", {"prompt": True}), ("save_as", None), ("prompt_save_as", None)):
                with self.subTest(inspection=view is inspection, command=name, args=args):
                    native_copy_save = view is copy and name == "save" and not args
                    self.assertEqual(None if native_copy_save else ("plist_tools_export_profile", {}), listener.on_text_command(view, name, args))
                    self.assertEqual(None if native_copy_save else ("plist_tools_export_profile_window", {}), listener.on_window_command(self.window, name, args))
            forward = self.plugin.PlistToolsExportProfileWindowCommand()
            forward.window = self.window
            forward.run()
            self.assertEqual("plist_tools_export_profile", view.commands[-1][0])
            if view is inspection:
                self.assertIsNone(view.file_name())
            else:
                self.assertTrue(Path(view.file_name()).is_file())
        self.assertEqual(2, len(self.window.inputs))
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())

    def test_export_blocks_attached_managed_and_signed_sources(self):
        view = self.unsigned_view()
        attached = str(Path(self.temp.name, "attached.plist"))
        managed = str(Path(self.temp.name, "managed.plist"))
        Path(attached).write_bytes(b"ATTACHED ORIGINAL")
        Path(managed).write_bytes(b"MANAGED ORIGINAL")
        view.path = attached
        view.config[self.editor.SOURCE] = managed
        originals = {path: Path(path).read_bytes() for path in (attached, managed, self.signed)}
        export = self.start_export(view)
        for path in originals:
            with self.subTest(path=path):
                self.sublime.error_message.reset_mock()
                export(path)
                self.assertIn("protected", self.sublime.error_message.call_args[0][0])
                self.assertEqual(originals[path], Path(path).read_bytes())
        self.sublime.ok_cancel_dialog.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows path alias behavior")
    def test_export_protects_real_windows_aliases_of_every_source_identity(self):
        view = self.unsigned_view()
        view.path = str(Path(self.temp.name, "Attached Profile.plist"))
        managed = str(Path(self.temp.name, "Managed Source.plist"))
        Path(view.path).write_bytes(b"ATTACHED ORIGINAL")
        Path(managed).write_bytes(b"MANAGED ORIGINAL")
        view.config[self.editor.SOURCE] = managed
        export = self.start_export(view)
        for path in (view.path, managed, self.signed):
            original = Path(path).read_bytes()
            for alias in (path.swapcase(), path + ".", "\\\\?\\" + path):
                with self.subTest(path=path, alias=alias):
                    self.assertTrue(os.path.samefile(path, alias))
                    self.sublime.error_message.reset_mock()
                    export(alias)
                    self.assertIn("protected", self.sublime.error_message.call_args[0][0])
                    self.assertEqual(original, Path(path).read_bytes())
        self.sublime.ok_cancel_dialog.assert_not_called()

    def test_valid_export_preserves_exact_xml_bytes_ids_tokens_comments_and_origin(self):
        view = self.unsigned_view()
        draft = view.file_name()
        destination = Path(self.temp.name, "exported.mobileconfig")
        self.start_export(view, target="jamf")(str(destination))
        self.assertEqual(self.xml.encode("utf-8"), destination.read_bytes())
        self.assertEqual(self.profile, plistlib.loads(destination.read_bytes()))
        self.assertEqual(self.signed, view.config[self.plugin.SIGNED_SOURCE])
        self.assertEqual(draft, view.file_name())
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())
        self.sublime.error_message.assert_not_called()

    def test_intune_size_error_stops_before_destination_or_write(self):
        view = self.unsigned_view()
        large = dict(self.profile, PayloadDescription="x" * 1000000)
        view.text = plistlib.dumps(large, sort_keys=False).decode("utf-8")
        with mock.patch.object(self.plugin, "atomic_write") as write:
            self.run_text(self.plugin.PlistToolsExportProfileCommand, view, target="intune")
        self.assertEqual([], self.window.inputs)
        write.assert_not_called()
        self.assertIn("1,000,000 bytes", self.sublime.error_message.call_args[0][0])
        self.assertIn(("plist_tools_validate_profile", {"target": "intune"}), view.commands)
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())

    def test_destination_change_during_overwrite_confirmation_is_not_overwritten(self):
        view = self.unsigned_view()
        draft = view.file_name()
        destination = Path(self.temp.name, "existing.mobileconfig")
        destination.write_bytes(b"OLD DESTINATION")
        export = self.start_export(view)

        def concurrent_change(*args):
            destination.write_bytes(b"NEWER CONTENT WRITTEN WHILE CONFIRMING")
            return True

        self.sublime.ok_cancel_dialog.side_effect = concurrent_change
        export(str(destination))
        self.sublime.ok_cancel_dialog.assert_called_once()
        self.assertEqual(b"NEWER CONTENT WRITTEN WHILE CONFIRMING", destination.read_bytes())
        self.assertIn("changed", self.sublime.error_message.call_args[0][0])
        self.assertEqual(draft, view.file_name())

    def test_payload_navigation_uses_offsets_only_for_unchanged_buffer(self):
        view = self.unsigned_view()
        document = self.plugin.parse_xml(view.text)
        self.editor._analysis[view.id()] = (view.change_count(), document, None)
        self.run_text(self.plugin.PlistToolsProfilePayloadsCommand, view)
        items, choose = self.window.panels[-1]
        self.assertEqual(1, len(items))
        offset = self.plugin.profile_payloads(document)[0].offset
        choose(0)
        self.assertEqual([offset], view.shown)
        view.replace(None, Region(0, 0), "<!-- intervening edit -->")
        view.shown.clear()
        choose(0)
        self.assertEqual([], view.shown)
        self.assertIn("profile changed", self.sublime.status_message.call_args[0][0])

    def test_add_payload_rejects_readonly_inspection_even_when_invoked_directly(self):
        _, inspection = self.inspect()
        before = inspection.text
        with mock.patch.object(self.plugin, "append_payload") as append:
            self.run_text(self.plugin.PlistToolsAddPayloadCommand, inspection, payload_type="com.example.more")
        append.assert_not_called()
        self.assertEqual(before, inspection.text)
        self.assertEqual([], self.window.inputs)
        self.assertIn("editable unsigned copy", self.sublime.error_message.call_args[0][0])

    def test_private_copy_survives_native_close_save_without_touching_signed_source(self):
        view = self.unsigned_view()
        draft = Path(view.file_name())
        self.assertEqual(Path(self.plugin._drafts_dir()), draft.parent)
        metadata = json.loads(draft.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(self.signed, metadata["signed_source"])
        self.assertEqual(view.name(), metadata["display_name"])
        edited = self.xml.replace("retain deployment comment", "retain changed deployment comment")
        view.replace(None, Region(0, view.size()), edited)
        view.run_command("save")
        self.assertEqual(edited.encode("utf-8"), draft.read_bytes())
        self.assertFalse(view.is_dirty())
        self.assertEqual(str(draft), view.file_name())
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())
        self.assertEqual([], self.window.inputs)

    def test_native_save_after_unexpected_signed_source_retarget_returns_to_private_draft(self):
        view = self.unsigned_view()
        edited = self.xml.replace("retain deployment comment", "native close save retained this edit")
        view.replace(None, Region(0, view.size()), edited)
        view.retarget(self.signed)
        view.run_command("save")
        destination = Path(view.file_name())
        self.assertFalse(self.editor._same_file_path(str(destination), self.signed))
        self.assertEqual(Path(self.plugin._drafts_dir()), destination.parent)
        self.assertEqual(edited.encode("utf-8"), destination.read_bytes())
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())

    def test_native_target_is_private_even_if_redirect_snapshot_write_fails(self):
        view = self.unsigned_view()
        view.retarget(self.signed)
        with mock.patch.object(self.plugin, "atomic_write", side_effect=OSError("draft snapshot unavailable")):
            view.run_command("save")
        destination = Path(view.file_name())
        self.assertEqual(Path(self.plugin._drafts_dir()), destination.parent)
        self.assertEqual(self.xml.encode("utf-8"), destination.read_bytes())
        self.assertIn("plist_tools_profile_draft_error", view.status)
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())

    def test_internal_and_managed_operations_bypass_profile_command_interception(self):
        listener = self.plugin.PlistToolsProfileListener()
        for bypass in ("internal", "managed"):
            with self.subTest(bypass=bypass):
                view = self.unsigned_view()
                if bypass == "internal":
                    view.config[self.editor.INTERNAL] = True
                else:
                    view.config[self.editor.SOURCE] = view.file_name()
                for name, args in (("save", None), ("save", {"prompt": True}), ("save_as", None), ("prompt_save_as", None)):
                    self.assertIsNone(listener.on_text_command(view, name, args))
                    self.assertIsNone(listener.on_window_command(self.window, name, args))
                draft = view.file_name()
                listener.on_pre_save(view)
                self.assertEqual(draft, view.file_name())
                # The final native-write guard still protects an unsafe target.
                view.retarget(self.signed)
                listener.on_pre_save(view)
                self.assertFalse(self.editor._same_file_path(view.file_name(), self.signed))
                self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())

    def test_sidecar_restores_origin_for_loaded_cloned_and_managed_draft_views(self):
        original = self.unsigned_view()
        draft = original.file_name()
        name = original.name()
        listener = self.plugin.PlistToolsProfileListener()
        for kind in ("load", "clone", "managed"):
            with self.subTest(kind=kind):
                path = str(Path(self.temp.name, "managed-recovery.plist")) if kind == "managed" else draft
                restored = View(self.window, self.xml, path)
                self.window.open_views.append(restored)
                if kind == "managed":
                    restored.config[self.editor.SOURCE] = draft
                    restored.config[self.editor.META] = str(Path(self.temp.name, "managed-recovery.json"))
                (listener.on_clone if kind == "clone" else listener.on_load)(restored)
                self.assertEqual(self.signed, restored.config[self.plugin.SIGNED_SOURCE])
                self.assertEqual(path, restored.file_name())
                if kind == "managed":
                    self.assertEqual("", restored.name())  # keep the managed working view's title
                    self.assertEqual(draft, restored.config[self.editor.SOURCE])
                    self.assertEqual(str(Path(self.temp.name, "managed-recovery.json")), restored.config[self.editor.META])
                else:
                    self.assertEqual(name, restored.name())
        reloaded = View(self.window, self.xml, draft)
        self.window.open_views.append(reloaded)
        self.plugin.plugin_loaded()
        self.assertEqual(self.signed, reloaded.config[self.plugin.SIGNED_SOURCE])
        self.assertEqual(name, reloaded.name())

    def test_invalid_draft_sidecars_do_not_crash_or_bind_untrusted_origin(self):
        original = self.unsigned_view()
        draft = Path(original.file_name())
        sidecar = draft.with_suffix(".json")
        invalid = (
            "not JSON",
            "[]",
            json.dumps({"version": 1, "signed_source": None, "display_name": "Bad origin"}),
            json.dumps({"version": 1, "signed_source": self.signed, "display_name": ["not a name"]}),
        )
        listener = self.plugin.PlistToolsProfileListener()
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                sidecar.write_text(metadata, encoding="utf-8")
                restored = View(self.window, self.xml, str(draft))
                listener.on_load(restored)
                self.assertNotIn(self.plugin.SIGNED_SOURCE, restored.config)
                self.assertIn("plist_tools_profile_draft_error", restored.status)
                self.assertEqual(str(draft), restored.file_name())
                self.assertEqual(self.xml, restored.text)
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())

    def test_changed_buffer_invalidates_export_callback(self):
        view = self.unsigned_view()
        destination = Path(self.temp.name, "stale-export.mobileconfig")
        export = self.start_export(view)
        view.replace(None, Region(0, 0), "<!-- changed while choosing destination -->")
        export(str(destination))
        self.assertFalse(destination.exists())
        messages = str(self.sublime.error_message.call_args_list) + str(self.sublime.status_message.call_args_list)
        self.assertIn("changed", messages.lower())
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())

    def test_explicit_profile_commands_work_above_live_analysis_limit(self):
        view = self.unsigned_view()
        with mock.patch.object(self.editor, "settings", return_value=Settings({"max_analysis_bytes": 32, "live_validation": True})):
            self.run_text(self.plugin.PlistToolsValidateProfileCommand, view)
            self.assertIn("0 errors", view.status["plist_tools_profile"])
            self.run_text(self.plugin.PlistToolsProfilePayloadsCommand, view)
        self.assertEqual(1, len(self.window.panels[-1][0]))
        self.sublime.error_message.assert_not_called()

    def test_dirty_unsigned_copy_can_inspect_signed_source_without_losing_edits(self):
        copy = self.unsigned_view()
        copy.replace(None, Region(0, 0), "<!-- unsaved unsigned edit -->")
        edited = copy.text
        with mock.patch.object(self.plugin, "decode_signed_profile", return_value=self.xml) as decode:
            self.run_text(self.plugin.PlistToolsInspectSignedProfileCommand, copy)
            self.assertEqual(1, len(self.sublime.workers))
            self.sublime.workers.pop(0)()
            while self.sublime.pending:
                self.sublime.pending.pop(0)()
        decode.assert_called_once()
        inspection = self.window.active_view()
        self.assertIsNot(copy, inspection)
        self.assertTrue(inspection.read_only)
        self.assertEqual(self.xml, inspection.text)
        self.assertEqual(edited, copy.text)
        self.assertTrue(copy.is_dirty())
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())
        self.sublime.error_message.assert_not_called()

    def test_duplicate_decode_is_suppressed_and_unexpected_failure_allows_retry(self):
        first = self.unsigned_view()
        second = self.unsigned_view()
        with mock.patch.object(self.plugin, "decode_signed_profile", side_effect=RuntimeError("decoder exploded")) as decode:
            self.run_text(self.plugin.PlistToolsInspectSignedProfileCommand, first)
            self.run_text(self.plugin.PlistToolsInspectSignedProfileCommand, first)
            self.run_text(self.plugin.PlistToolsInspectSignedProfileCommand, second)
            self.assertEqual(1, len(self.sublime.workers))
            self.sublime.workers.pop(0)()
            while self.sublime.pending:
                self.sublime.pending.pop(0)()
        decode.assert_called_once()
        self.assertIn("decoder exploded", self.sublime.error_message.call_args[0][0])
        self.sublime.error_message.reset_mock()
        with mock.patch.object(self.plugin, "decode_signed_profile", return_value=self.xml) as retry:
            self.run_text(self.plugin.PlistToolsInspectSignedProfileCommand, second)
            self.assertEqual(1, len(self.sublime.workers))
            self.sublime.workers.pop(0)()
            while self.sublime.pending:
                self.sublime.pending.pop(0)()
        retry.assert_called_once()
        self.assertTrue(self.window.active_view().read_only)
        self.sublime.error_message.assert_not_called()
        self.assertEqual(self.signed_bytes, Path(self.signed).read_bytes())


if __name__ == "__main__":
    unittest.main()
