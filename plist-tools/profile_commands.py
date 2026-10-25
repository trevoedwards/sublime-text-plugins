"""Apple profile workflows. Decoding and all filesystem exports are explicit."""
import json
import os
import re
import uuid

import sublime
import sublime_plugin

from . import plist_tools as editor
from .mobileconfig import COMMON_KEYS, analyze_profile, append_payload, is_profile, make_profile, profile_payloads
from .plist_core import parse_xml, xml_bytes
from .plist_storage import atomic_write, fingerprint, write_metadata
from .signed_profiles import SignedProfileError, decode_signed_profile


SIGNED_SOURCE = "plist_tools_signed_source"
INSPECTION = "plist_tools_signed_inspection"
DRAFT = "plist_tools_profile_draft"
MAX_DRAFT_METADATA_BYTES = 64 * 1024
_decoding = set()


def _profile_view(view):
    return bool(editor._is_plist(view) or view.settings().get(SIGNED_SOURCE))


def _drafts_dir(create=True):
    path = os.path.join(sublime.packages_path(), "User", "PlistTools", "ProfileCopies")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _draft_metadata(path):
    with open(os.path.splitext(path)[0] + ".json", "rb") as handle:
        data = handle.read(MAX_DRAFT_METADATA_BYTES + 1)
    if len(data) > MAX_DRAFT_METADATA_BYTES:
        raise ValueError("Unsigned profile draft metadata exceeds its size limit.")
    meta = json.loads(data.decode("utf-8"))
    if (not isinstance(meta, dict) or type(meta.get("version")) is not int or meta["version"] != 1
            or not isinstance(meta.get("signed_source"), str) or not meta["signed_source"]
            or not os.path.isabs(meta["signed_source"])
            or not isinstance(meta.get("display_name"), str) or not meta["display_name"]):
        raise ValueError("Unsigned profile draft metadata is invalid.")
    return meta


def _write_draft_metadata(path, signed_source, display_name):
    if not isinstance(signed_source, str) or not signed_source:
        raise ValueError("The signed profile source path is missing.")
    meta = {"version": 1, "signed_source": os.path.abspath(signed_source),
            "display_name": str(display_name or "Unsigned Profile.mobileconfig")[:4096]}
    encoded = json.dumps(meta, ensure_ascii=True).encode("utf-8")
    if len(encoded) > MAX_DRAFT_METADATA_BYTES:
        raise ValueError("Unsigned profile draft metadata exceeds its size limit.")
    write_metadata(os.path.splitext(path)[0] + ".json", meta)


def _create_draft(text, signed_source, display_name):
    payload = xml_bytes(text)
    path = os.path.join(_drafts_dir(), str(uuid.uuid4()) + ".mobileconfig")
    atomic_write(path, payload, None)
    _write_draft_metadata(path, signed_source, display_name)
    return path


def _bind_draft(view):
    try:
        candidates = (view.file_name(), editor._source(view), view.settings().get(DRAFT))
        directory = _drafts_dir(create=False)
        for path in candidates:
            if (not isinstance(path, str) or not path.lower().endswith(".mobileconfig")
                    or not editor._same_file_path(os.path.dirname(os.path.abspath(path)), directory)):
                continue
            meta = _draft_metadata(path)
            view.settings().set(SIGNED_SOURCE, meta["signed_source"])
            view.settings().set(DRAFT, os.path.abspath(path))
            view.settings().set("syntax", editor.SYNTAX)
            if editor._same_file_path(path, view.file_name()):
                view.set_name(meta["display_name"])
            view.set_status("plist_tools_signed", "Unsigned profile draft · original signature is not included · existing identifiers retained")
            view.erase_status("plist_tools_profile_draft_error")
            return True
    except (OSError, ValueError, TypeError, RecursionError) as error:
        view.set_status("plist_tools_profile_draft_error", "Unsigned draft metadata unavailable: " + str(error)[:300])
    return False


def _redirect_signed_native_save(view):
    original = view.settings().get(SIGNED_SOURCE)
    if not original or not editor._same_file_path(view.file_name(), original):
        return
    # A native save cannot be cancelled. Move its target first, so even a failed
    # recovery-directory or metadata write cannot leave it on the signed source.
    path = os.path.join(_drafts_dir(create=False), str(uuid.uuid4()) + ".mobileconfig")
    view.retarget(path)
    view.settings().set(DRAFT, path)
    view.set_encoding("UTF-8")
    try:
        _drafts_dir()
        atomic_write(path, editor._text(view).encode("utf-8"), None)
        _write_draft_metadata(path, original, view.name())
        view.set_status("plist_tools_signed", "Native save redirected to a private unsigned draft · signed original unchanged")
    except (OSError, ValueError, TypeError, RecursionError) as error:
        view.set_status("plist_tools_profile_draft_error", "Signed original protected; draft recovery failed: " + str(error)[:300])


def _new_view(window, text, name, signed_source=None, inspection=False):
    draft = None
    if signed_source and not inspection:
        text = xml_bytes(text).decode("utf-8")
        draft = _create_draft(text, signed_source, name)
    view = window.new_file()
    view.settings().set("syntax", editor.SYNTAX)
    view.settings().set("default_extension", "mobileconfig")
    view.set_name(name)
    view.run_command("plist_tools_replace", {"text": text})
    if draft:
        view.retarget(draft)
        view.set_encoding("UTF-8")
        view.settings().set(DRAFT, draft)
    if signed_source:
        view.settings().set(SIGNED_SOURCE, signed_source)
    if inspection:
        view.settings().set(INSPECTION, True)
        view.set_scratch(True)
        view.set_read_only(True)
        view.set_status("plist_tools_signed", "Signed profile inspection · signer trust not established · original unchanged")
    elif signed_source:
        view.set_status("plist_tools_signed", "Unsigned profile draft · Save updates this private copy · existing identifiers retained")
    return view


def _document(view):
    document = editor._document(view)
    if document is None:
        # Explicit profile operations may exceed the live-analysis byte cap.
        try:
            document = parse_xml(editor._text(view))
        except (ValueError, RecursionError) as error:
            editor._error(error)
    return document


def _jump_result(view, count, offsets, index):
    if index >= 0 and view.is_valid():
        if view.change_count() != count:
            sublime.status_message("Plist Tools: profile changed; run the command again")
        else:
            editor._goto(view, offsets[index])


class PlistToolsNewProfileCommand(sublime_plugin.WindowCommand):
    def run(self):
        def create(name):
            try:
                name = name.strip() or "New Configuration Profile"
                prefix = editor.settings().get("profile_identifier_prefix", "com.example.profile")
                identifier = str(prefix).rstrip(".") + "." + str(uuid.uuid4()).lower()
                text = make_profile(name, identifier,
                                    editor.settings().get("profile_organization", ""),
                                    editor.settings().get("profile_scope", "System"))
                filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(". ") or "New Profile"
                _new_view(self.window, text, filename + ".mobileconfig")
                sublime.status_message("Plist Tools: profile scaffold created; add and configure payloads before deployment")
            except (ValueError, TypeError, OverflowError) as error:
                editor._error(error)
        self.window.show_input_panel("Profile display name", "New Configuration Profile", create, None, None)


class PlistToolsValidateProfileCommand(sublime_plugin.TextCommand):
    def run(self, edit, target="generic"):
        document = _document(self.view)
        if document is None:
            return
        try:
            issues = analyze_profile(document, target)
        except (ValueError, RecursionError) as error:
            editor._error(error)
            return
        errors = sum(issue.severity == "error" for issue in issues)
        warnings = sum(issue.severity == "warning" for issue in issues)
        self.view.set_status("plist_tools_profile", "Profile common keys: {} errors · {} warnings".format(errors, warnings))
        if not issues:
            sublime.status_message("Plist Tools: profile common keys valid; payload settings and MDM deployment require separate checks")
            return
        count = self.view.change_count()
        self.view.window().show_quick_panel(
            [[issue.severity.upper() + " · " + issue.message, issue.path] for issue in issues],
            lambda index: _jump_result(self.view, count, [issue.offset for issue in issues], index))

    def is_enabled(self, **kwargs):
        return _profile_view(self.view)


class PlistToolsProfilePayloadsCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        document = _document(self.view)
        if document is None:
            return
        payloads = profile_payloads(document)
        if not payloads:
            sublime.status_message("Plist Tools: no plaintext payload dictionaries; run Check Profile to inspect the envelope")
            return
        count = self.view.change_count()
        items = [[item.display_name or item.payload_type or "Unnamed payload",
                  "{} · {}".format(item.payload_type or "Missing type", item.identifier or "Missing identifier"),
                  "UUID: " + (item.uuid or "Missing UUID")] for item in payloads]
        self.view.window().show_quick_panel(items,
            lambda index: _jump_result(self.view, count, [item.offset for item in payloads], index))

    def is_enabled(self):
        return _profile_view(self.view)


class PlistToolsAddPayloadCommand(sublime_plugin.TextCommand):
    def run(self, edit, payload_type=None):
        if self.view.is_read_only():
            editor._error("Create an editable unsigned copy before adding a payload.")
            return
        if payload_type is None:
            self.view.window().show_input_panel("Payload domain (common-key scaffold)", "com.example.application",
                lambda value: self.view.run_command("plist_tools_add_payload", {"payload_type": value.strip()}), None, None)
            return
        try:
            text = append_payload(editor._text(self.view), payload_type, payload_type)
            self.view.replace(edit, sublime.Region(0, self.view.size()), text)
            sublime.status_message("Plist Tools: payload metadata added; configure the domain's settings before deployment")
        except (ValueError, TypeError, RecursionError) as error:
            editor._error(error)

    def is_enabled(self, **kwargs):
        return _profile_view(self.view) and not self.view.is_read_only()


class PlistToolsInsertUuidCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if self.view.is_read_only():
            return
        for region in reversed(list(self.view.sel())):
            self.view.replace(edit, region, str(uuid.uuid4()).upper())

    def is_enabled(self):
        return _profile_view(self.view) and not self.view.is_read_only()


class PlistToolsExportProfileCommand(sublime_plugin.TextCommand):
    def run(self, edit, target="generic"):
        view = self.view
        try:
            source = editor._text(view)
            document = parse_xml(source)
            errors = [issue for issue in analyze_profile(document, target) if issue.severity == "error"]
            if errors:
                editor._error(errors[0].message)
                view.run_command("plist_tools_validate_profile", {"target": target})
                return
            payload = xml_bytes(source)
            count = view.change_count()
        except (ValueError, RecursionError) as error:
            editor._error(error)
            return
        initial = view.settings().get(SIGNED_SOURCE) or editor._source(view) or view.file_name() or view.name() or "profile.mobileconfig"
        initial = os.path.splitext(initial)[0] + "-unsigned-copy.mobileconfig"

        def export(path):
            try:
                if not view.is_valid() or view.change_count() != count:
                    raise ValueError("The profile changed while choosing the destination. Run Export again to validate and export the current text.")
                path = os.path.abspath(os.path.expanduser(path))
                protected = (view.file_name(), editor._source(view), view.settings().get(SIGNED_SOURCE))
                if any(editor._same_file_path(path, other) for other in protected):
                    raise ValueError("Choose a separate export destination. The current source and signed original are protected.")
                # Capture the destination before confirmation so concurrent changes
                # while the dialog is open cannot silently replace newer contents.
                before = fingerprint(path)
                if before is not None and not sublime.ok_cancel_dialog("Replace {}?".format(path), "Replace"):
                    return
                atomic_write(path, payload, before, private=False)
                sublime.status_message("Plist Tools: exported unsigned XML profile · identifiers retained · " + path)
            except (OSError, ValueError) as error:
                editor._error(error)
        view.window().show_input_panel("Export unsigned {} XML profile to".format(target), initial, export, None, None)

    def is_enabled(self, **kwargs):
        return _profile_view(self.view)


class PlistToolsInspectSignedProfileCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        path = view.settings().get(SIGNED_SOURCE) or view.file_name()
        window = view.window()
        if not path or not window:
            editor._error("Open the signed .mobileconfig file from disk before inspecting it.")
            return
        if (view.is_dirty() and not view.settings().get(INSPECTION)
                and editor._same_file_path(path, view.file_name())):
            editor._error("The raw file has unsaved edits. Preserve those edits before decoding the file on disk.")
            return
        decoder = editor.settings().get("openssl_path") or None
        try:
            limit = int(editor.settings().get("max_signed_profile_bytes", 16 * 1024 * 1024))
            timeout = float(editor.settings().get("signed_decode_timeout", 15))
            key = os.path.normcase(os.path.abspath(path))
        except (TypeError, ValueError) as error:
            editor._error(error)
            return
        if key in _decoding:
            sublime.status_message("Plist Tools: this signed profile is already being decoded")
            return
        _decoding.add(key)
        view.set_status("plist_tools_signed", "Decoding signed profile…")

        def finish(text=None, message=None):
            try:
                if message is not None:
                    editor._error(message)
                elif window.is_valid():
                    _new_view(window, text, os.path.basename(path) + " [signed inspection]", path, True)
            except Exception as error:
                editor._error("Signed profile inspection failed: " + str(error)[:2000])
            finally:
                _decoding.discard(key)
                if view.is_valid():
                    view.erase_status("plist_tools_signed")

        def decode():
            try:
                text = decode_signed_profile(path, openssl_path=decoder, timeout=timeout, max_bytes=limit)
            except Exception as error:
                message = "Signed profile could not be decoded: " + (str(error) or type(error).__name__)[:2000]
                sublime.set_timeout(lambda: finish(message=message), 0)
            else:
                sublime.set_timeout(lambda: finish(text=text), 0)
        try:
            sublime.set_timeout_async(decode, 0)
        except Exception as error:
            finish(message="Signed profile decoding could not start: " + str(error)[:2000])

    def is_enabled(self):
        path = self.view.settings().get(SIGNED_SOURCE) or self.view.file_name() or ""
        return path.lower().endswith(".mobileconfig")


class PlistToolsUnsignedProfileCopyCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if not self.view.settings().get(INSPECTION):
            return
        original = self.view.settings().get(SIGNED_SOURCE)
        text = editor._text(self.view)
        try:
            _new_view(self.view.window(), text, os.path.splitext(os.path.basename(original))[0] + "-unsigned.mobileconfig", original)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            editor._error(error)

    def is_enabled(self):
        return bool(self.view.settings().get(INSPECTION))


class PlistToolsRecoverProfileDraftsCommand(sublime_plugin.WindowCommand):
    def run(self):
        directory = _drafts_dir(create=False)
        entries = []
        try:
            names = os.listdir(directory) if os.path.isdir(directory) else []
            for name in names:
                if not name.lower().endswith(".mobileconfig"):
                    continue
                path = os.path.join(directory, name)
                try:
                    meta = _draft_metadata(path)
                    if os.path.isfile(path):
                        entries.append((meta["display_name"], meta["signed_source"], path))
                except (OSError, ValueError, TypeError, RecursionError):
                    continue
        except OSError as error:
            editor._error(error)
            return
        if not entries:
            sublime.status_message("Plist Tools: no recoverable unsigned profile drafts")
            return
        entries.sort(key=lambda entry: (entry[0].lower(), entry[2]))

        def open_draft(index):
            if 0 <= index < len(entries) and self.window.is_valid():
                self.window.open_file(entries[index][2])
        self.window.show_quick_panel([[name, original] for name, original, path in entries], open_draft)


def _profile_completion_view(view):
    if view.settings().get(SIGNED_SOURCE):
        return True
    if any(name and name.lower().endswith(".mobileconfig") for name in (view.file_name(), view.name())):
        return True
    cached = editor._analysis.get(view.id())
    return bool(cached and cached[0] == view.change_count() and cached[1] is not None and is_profile(cached[1]))


class PlistToolsProfileListener(sublime_plugin.EventListener):
    def on_load(self, view):
        _bind_draft(view)

    def on_activated(self, view):
        _bind_draft(view)

    def on_clone(self, view):
        _bind_draft(view)

    def on_pre_save(self, view):
        _bind_draft(view)
        _redirect_signed_native_save(view)

    def on_query_completions(self, view, prefix, locations):
        if not locations or not _profile_completion_view(view) or not view.match_selector(locations[0], "meta.key.plist"):
            return None
        return [sublime.CompletionItem(trigger=key, annotation=info["type"], completion=key,
                                       details=info["description"], completion_format=sublime.COMPLETION_FORMAT_TEXT)
                for key, info in COMMON_KEYS.items()]

    def on_modified(self, view):
        view.erase_status("plist_tools_profile")

    def on_text_command(self, view, command_name, args):
        if self._export_required(view, command_name, args):
            return ("plist_tools_export_profile", {})

    def on_window_command(self, window, command_name, args):
        view = window.active_view()
        if view and self._export_required(view, command_name, args):
            return ("plist_tools_export_profile_window", {})

    @staticmethod
    def _export_required(view, command_name, args):
        if view.settings().get(editor.INTERNAL) or editor._source(view):
            return False
        original = view.settings().get(SIGNED_SOURCE)
        if not original or command_name not in ("save", "save_as", "prompt_save_as"):
            return False
        if command_name != "save" or (args or {}).get("prompt"):
            return True
        return bool(view.settings().get(INSPECTION) or not view.file_name()
                    or editor._same_file_path(view.file_name(), original))


class PlistToolsExportProfileWindowCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if view:
            view.run_command("plist_tools_export_profile")


def plugin_loaded():
    for window in sublime.windows():
        for view in window.views():
            _bind_draft(view)
