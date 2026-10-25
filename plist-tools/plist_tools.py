"""Sublime integration. Binary sources are NEVER attached to an XML buffer."""
import hashlib
import html
import json
import os
import re

import sublime
import sublime_plugin

from .plist_core import PlistError, parse_xml, format_xml, load_binary, binary_from_xml, xml_bytes
from .plist_storage import ConflictError, atomic_write, fingerprint, write_metadata, text_hash


SETTINGS = "PlistTools.sublime-settings"
SYNTAX = "Packages/PlistTools/PLIST.sublime-syntax"
SOURCE = "plist_tools_source_path"
META = "plist_tools_metadata"
INTERNAL = "plist_tools_internal_command"
_analysis = {}
_tickets = {}
_saving = set()
_failed_save = {}
_opening = {}


def settings():
    return sublime.load_settings(SETTINGS)


def _source(view):
    source = view.settings().get(SOURCE)
    if source or view.settings().get("plist_tools_detached"):
        return source
    # Cloned views may share the buffer without inheriting per-view settings.
    path = view.file_name()
    if path and os.path.normcase(os.path.dirname(path)) == os.path.normcase(_recovery_dir(create=False)):
        meta_path = os.path.splitext(path)[0] + ".json"
        if os.path.isfile(meta_path):
            try:
                meta = _read_meta(meta_path)
                _bind(view, meta_path, meta)
                return meta["source"]
            except (OSError, ValueError, KeyError):
                pass
    return None


def _text(view):
    return view.substr(sublime.Region(0, view.size()))


def _is_plist(view):
    name = view.file_name() or ""
    return bool(_source(view) or name.lower().endswith((".plist", ".mobileconfig", ".entitlements"))
                or view.match_selector(0, "text.xml.plist"))


def _error(error):
    sublime.error_message("Plist Tools: {}".format(error))


def _native(view, command, args=None):
    view.settings().set(INTERNAL, True)
    try:
        view.run_command(command, args or {})
    finally:
        view.settings().erase(INTERNAL)
        if command == "save":
            _saving.discard(view.buffer_id())


def _recovery_dir(create=True):
    path = os.path.join(sublime.packages_path(), "User", "PlistTools", "Recovery")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _paths(source):
    identity = os.path.normcase(os.path.abspath(source)).encode("utf-8")
    base = os.path.join(_recovery_dir(), hashlib.sha256(identity).hexdigest())
    return base + ".plist", base + ".json"


def _read_meta(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _meta(view):
    return _read_meta(view.settings().get(META))


def _working_path(view):
    return os.path.splitext(view.settings().get(META))[0] + ".plist"


def _snapshot_path(view):
    return os.path.splitext(view.settings().get(META))[0] + ".pending"


def _check_working_target(view):
    expected = _working_path(view)
    actual = view.file_name()
    if not actual:
        # Hot exit can restore a retargeted view as an unnamed buffer.
        view.retarget(expected)
        return
    if os.path.normcase(os.path.abspath(actual)) != os.path.normcase(os.path.abspath(expected)):
        if os.path.normcase(os.path.abspath(actual)) == os.path.normcase(os.path.abspath(_source(view))):
            view.retarget(expected)
        view.settings().erase(SOURCE)
        view.settings().erase(META)
        view.settings().set("plist_tools_detached", True)
        view.set_status("plist_tools_source", "PLIST source association detached after unexpected file retarget")
        raise ConflictError("This view was retargeted outside its recovery file. The old PLIST source was not changed; its association has been removed.")


def _bind(view, meta_path, meta):
    view.settings().set(SOURCE, meta["source"])
    view.settings().set(META, meta_path)
    view.settings().set("plist_tools_format", meta["format"])
    view.settings().set("syntax", SYNTAX)
    pending = " · pending" if meta.get("pending") or meta.get("working_pending") else ""
    view.set_name(os.path.basename(meta["source"]) + " [" + meta["format"] + pending + "]")
    view.set_status("plist_tools_source", "PLIST {} · {}".format(meta["format"], meta["source"]))
    view.set_encoding("UTF-8")
    # Native external reload only watches the recovery file. Check the source on
    # activation and before every commit instead of silently reloading edits.
    view.settings().set("reload_file_on_change", False)
    view.settings().set("always_prompt_for_file_reload", False)
    if pending:
        view.set_status("plist_tools_pending", "PLIST recovery pending: Save to commit, Revert to discard")
    else:
        view.erase_status("plist_tools_pending")


def _check_conflict(view):
    try:
        meta = _meta(view)
        if fingerprint(meta["source"]) != meta["fingerprint"]:
            view.set_status("plist_tools_conflict", "PLIST source changed on disk; Revert or Save As")
        else:
            view.erase_status("plist_tools_conflict")
    except (OSError, ValueError) as error:
        view.set_status("plist_tools_conflict", "PLIST source unavailable: {}".format(error))


def _open_working_view(original, work, meta_path, meta, after_open=None):
    """Load XML into a new buffer, never inheriting raw bytes or retarget history."""
    window = original.window()
    if not window:
        raise PlistError("The original view must be in a window before opening its XML working copy.")
    if original.clones():
        raise PlistError("Close extra views of this original file before converting it to a managed PLIST view.")
    group, index = window.get_view_index(original)
    was_read_only = original.is_read_only()
    original.settings().set("plist_tools_opening_work", True)
    original.set_read_only(True)
    try:
        working = window.open_file(work, group=group)
        _opening[working.id()] = (original, meta_path, meta, group, index, was_read_only, after_open)
        # Finish outside the initiating TextCommand's edit lifetime: closing its
        # original view while that Edit is active is unsafe on older builds.
        sublime.set_timeout(lambda: _finish_working_open(working), 0)
    except Exception:
        original.settings().erase("plist_tools_opening_work")
        original.set_read_only(was_read_only)
        raise
    return working


def _finish_working_open(working):
    if working.id() not in _opening or working.is_loading():
        return
    original, meta_path, meta, group, index, was_read_only, after_open = _opening.pop(working.id())
    try:
        expected = os.path.splitext(meta_path)[0] + ".plist"
        if (not working.is_valid() or not working.file_name()
                or os.path.normcase(os.path.abspath(working.file_name())) != os.path.normcase(os.path.abspath(expected))):
            raise PlistError("The XML working copy could not be opened. The original has not been changed.")
        _bind(working, meta_path, meta)
        window = working.window()
        window.set_view_index(working, group, index)
        window.focus_view(working)
        if original.is_valid():
            original.set_scratch(True)
            if not original.close():
                original.set_scratch(False)
                raise PlistError("The original view could not be closed. Its file has not been converted.")
        _check_conflict(working)
        _schedule(working)
        if after_open:
            after_open(working)
    except (OSError, ValueError, RecursionError) as error:
        if original.is_valid():
            original.settings().erase("plist_tools_opening_work")
            original.set_read_only(was_read_only)
        _error(error)


def _prepare(view):
    if not view.is_valid() or view.is_loading():
        return
    if view.settings().get("plist_tools_opening_work"):
        return
    path = view.file_name()
    if not path and view.settings().get(META):
        try:
            meta = _meta(view)
            view.retarget(_working_path(view))
            _bind(view, view.settings().get(META), meta)
            path = view.file_name()
        except (OSError, ValueError, KeyError):
            return
    if not path:
        return
    # A restored recovery file is identifiable even if session settings vanished.
    meta_path = os.path.splitext(path)[0] + ".json"
    recovery = os.path.normcase(os.path.abspath(os.path.dirname(path))) == os.path.normcase(_recovery_dir(create=False))
    if recovery and os.path.isfile(meta_path):
        try:
            meta = _read_meta(meta_path)
            _bind(view, meta_path, meta)
            snapshot = _snapshot_path(view)
            if meta.get("working_pending") and os.path.isfile(snapshot) and not view.is_dirty():
                with open(snapshot, "r", encoding="utf-8", newline="") as handle:
                    recovered = handle.read()
                if recovered != _text(view):
                    view.run_command("plist_tools_replace", {"text": recovered})
            _check_conflict(view)
            _schedule(view)
        except (OSError, ValueError) as error:
            _error(error)
        return
    if _source(view):
        _check_conflict(view)
        return
    # Header sniffing is cheap even for files whose extension is not .plist.
    binary_detected = False
    before = None
    try:
        with open(path, "rb") as handle:
            header = handle.read(8)
        if header != b"bplist00":
            if _is_plist(view):
                if path.lower().endswith(".mobileconfig") and not _text(view).lstrip("\ufeff \t\r\n").startswith("<"):
                    view.set_read_only(True)
                    view.set_status("plist_tools_error", "Signed or encrypted mobileconfig profiles are unsupported (read-only)")
                    return
                view.settings().set("syntax", SYNTAX)
                _schedule(view)
            return
        binary_detected = True
        limit = int(settings().get("max_binary_bytes", 8 * 1024 * 1024))
        size = os.path.getsize(path)
        if size > limit:
            if view.settings().get("plist_tools_rejected_size") == size:
                return
            view.settings().set("plist_tools_rejected_size", size)
            raise PlistError("Binary PLIST exceeds the configured max_binary_bytes limit; it remains read-only.")
        before = fingerprint(path)
        if view.settings().get("plist_tools_rejected") == before:
            return
        # Do not discard hot-exit edits or an already modified binary view.
        if view.is_dirty():
            view.set_read_only(True)
            view.settings().set("plist_tools_rejected", before)
            _error("A modified raw binary buffer was found. Its edits were retained. Save a copy before reopening the original as a PLIST.")
            return
        for window in sublime.windows():
            for other in window.views():
                if other.id() != view.id() and _source(other) and os.path.normcase(_source(other)) == os.path.normcase(path):
                    window.focus_view(other)
                    view.set_scratch(True)
                    view.close()
                    return
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise PlistError("Binary PLIST exceeds the configured max_binary_bytes limit; it remains read-only.")
        xml = load_binary(data)
        if fingerprint(path) != before:
            raise ConflictError("The binary file changed while opening it. Open it again.")
        work, meta_path = _paths(path)
        meta = None
        if os.path.isfile(work) and os.path.isfile(meta_path):
            old = _read_meta(meta_path)
            snapshot = os.path.splitext(meta_path)[0] + ".pending"
            recovery_path = snapshot if old.get("working_pending") and os.path.isfile(snapshot) else work
            with open(recovery_path, "r", encoding="utf-8", newline="") as handle:
                recovery_text = handle.read()
            if (old.get("pending") or text_hash(recovery_text) != old.get("saved_text_hash")
                    or old.get("fingerprint") == before):
                meta = old
                if old.get("pending") or text_hash(recovery_text) != old.get("saved_text_hash"):
                    sublime.status_message("Plist Tools: recovered pending edits for " + path)
                if recovery_path != work:
                    atomic_write(work, recovery_text.encode("utf-8"), fingerprint(work))
        if meta is None:
            meta = {"version": 1, "source": os.path.abspath(path), "format": "binary",
                    "fingerprint": before, "saved_text_hash": text_hash(xml), "pending": False}
            atomic_write(work, xml.encode("utf-8"), fingerprint(work))
            write_metadata(meta_path, meta)
        # Retarget+revert retains raw hexadecimal data in Sublime's undo history.
        # A new XML buffer is essential: the raw buffer is closed untouched.
        _open_working_view(view, work, meta_path, meta)
    except (OSError, ValueError, RecursionError) as error:
        # Losing access to an ordinary text file must not change its editability,
        # including XML plists that have been renamed or deleted externally.
        if not binary_detected:
            return
        if binary_detected and before:
            view.settings().set("plist_tools_rejected", before)
        view.set_read_only(True)
        view.set_status("plist_tools_error", "PLIST read-only: {}".format(error))
        message = str(error)
        if view.settings().get("plist_tools_rejected_error") != message:
            view.settings().set("plist_tools_rejected_error", message)
            _error(error)


def _save_source(view, target=None, output_format=None):
    _check_working_target(view)
    meta = _meta(view)
    source = _text(view)
    output_format = output_format or meta["format"]
    payload = binary_from_xml(source) if output_format == "binary" else xml_bytes(source)
    destination = os.path.abspath(target) if target else meta["source"]
    expected = meta["fingerprint"] if os.path.normcase(destination) == os.path.normcase(meta["source"]) else fingerprint(destination)
    # Persist recovery *before* touching the source, including invalid documents
    # through the ordinary native save fallback.
    meta["pending"] = True
    meta["working_pending"] = True
    snapshot = _snapshot_path(view)
    atomic_write(snapshot, source.encode("utf-8"), fingerprint(snapshot))
    write_metadata(view.settings().get(META), meta)
    result = atomic_write(destination, payload, expected)
    if target and os.path.normcase(destination) != os.path.normcase(meta["source"]):
        # Save As exports a copy, keeping the source association explicit.
        return
    meta.update({"fingerprint": result, "format": output_format,
                 "saved_text_hash": text_hash(source), "pending": False})
    write_metadata(view.settings().get(META), meta)
    _bind(view, view.settings().get(META), meta)
    view.erase_status("plist_tools_conflict")
    view.erase_status("plist_tools_save_error")


def _save(view):
    try:
        _save_source(view)
    except (OSError, ValueError, OverflowError, RecursionError) as error:
        # Leave the edit history and dirty state unchanged. The separate recovery
        # write means an editor shutdown after a failed commit still retains text.
        _persist_pending(view, error)
        _error(error)
        return False
    _saving.add(view.buffer_id())
    _native(view, "save")
    return True


def _persist_pending(view, error):
    view.set_status("plist_tools_save_error", "PLIST NOT SAVED: {}".format(error))
    if not view.settings().get(META):
        return
    try:
        meta = _meta(view)
        meta["pending"] = True
        meta["working_pending"] = True
        meta["last_error"] = str(error)
        snapshot = _snapshot_path(view)
        atomic_write(snapshot, _text(view).encode("utf-8"), fingerprint(snapshot))
        write_metadata(view.settings().get(META), meta)
        _bind(view, view.settings().get(META), meta)
    except (OSError, ValueError) as recovery_error:
        _error("Save failed, and recovery could not be written: {}. Keep this buffer open.".format(recovery_error))


def _analysis_source(view):
    limit = int(settings().get("max_analysis_bytes", 2 * 1024 * 1024))
    if view.size() > limit:
        _analysis.pop(view.id(), None)
        view.set_status("plist_tools_validation", "PLIST: analysis paused (file exceeds limit)")
        return None
    return _text(view)


def _parse_analysis(source):
    try:
        return parse_xml(source), None
    except (PlistError, ValueError, RecursionError) as error:
        return None, error


def _apply_analysis(view, count, document, error):
    if not view.is_valid() or view.change_count() != count:
        return
    _analysis[view.id()] = (count, document, error)
    if document:
        view.erase_regions("plist_tools_errors")
        view.erase_phantoms("plist_tools_error")
        view.set_status("plist_tools_validation", "PLIST: valid")
    else:
        at = min(getattr(error, "offset", 0), view.size())
        region = sublime.Region(at, min(at + 1, view.size()))
        view.add_regions("plist_tools_errors", [region], "invalid", "dot", sublime.DRAW_NO_FILL)
        view.erase_phantoms("plist_tools_error")
        if settings().get("inline_diagnostics", True):
            view.add_phantom("plist_tools_error", region,
                             '<body><div style="padding: 0.2rem; color: var(--redish)">PLIST: {}</div></body>'.format(html.escape(str(error))),
                             sublime.LAYOUT_BELOW)
        row, col = view.rowcol(at)
        view.set_status("plist_tools_validation", "PLIST {}:{} · {}".format(row + 1, col + 1, error))


def _diagnose(view):
    source = _analysis_source(view)
    if source is None:
        return None
    document, error = _parse_analysis(source)
    _apply_analysis(view, view.change_count(), document, error)
    return document


def _document(view):
    cached = _analysis.get(view.id())
    return cached[1] if cached and cached[0] == view.change_count() else _diagnose(view)


def _schedule(view):
    if not _is_plist(view) or not settings().get("live_validation", True):
        return
    key = view.id()
    ticket = _tickets[key] = _tickets.get(key, 0) + 1

    def analyze():
        if view.is_valid() and _tickets.get(key) == ticket:
            source = _analysis_source(view)
            if source is None:
                return
            count = view.change_count()

            def parse():
                if _tickets.get(key) != ticket:
                    return
                document, error = _parse_analysis(source)
                sublime.set_timeout(lambda: _apply_analysis(view, count, document, error), 0)

            sublime.set_timeout_async(parse, 0)

    sublime.set_timeout(analyze, max(100, int(settings().get("validation_delay", 400))))


def _goto(view, point):
    view.sel().clear()
    view.sel().add(sublime.Region(point))
    view.show_at_center(point)


class PlistToolsReplaceCommand(sublime_plugin.TextCommand):
    def run(self, edit, text):
        self.view.replace(edit, sublime.Region(0, self.view.size()), text)


class PlistToolsMarkDirtyCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        # Two edits leave identical text but a new undo revision after a native
        # close-save bypass. The durable pending recovery also survives closure.
        self.view.insert(edit, self.view.size(), " ")
        self.view.erase(edit, sublime.Region(self.view.size() - 1, self.view.size()))


class PlistToolsSaveCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        _save(self.view)


class PlistToolsSaveAllCommand(sublime_plugin.WindowCommand):
    def run(self):
        seen = set()
        for view in self.window.views():
            if view.buffer_id() in seen:
                continue
            seen.add(view.buffer_id())
            if _source(view):
                if view.is_dirty() or _meta(view).get("pending") or _meta(view).get("working_pending"):
                    _save(view)
            elif view.is_dirty():
                view.run_command("save")


class PlistToolsSaveAsCommand(sublime_plugin.TextCommand):
    def run(self, edit, output_format=None):
        view = self.view
        if not output_format:
            view.window().show_quick_panel(["Export binary PLIST copy", "Export XML PLIST copy"],
                lambda index: view.run_command("plist_tools_save_as", {"output_format": ("binary", "xml")[index]}) if index >= 0 else None)
            return
        initial = _source(view) or view.file_name() or "untitled.plist"
        base, ext = os.path.splitext(initial)

        def export(path):
            path = os.path.abspath(os.path.expanduser(path))
            if path == view.file_name() or (_source(view) and os.path.normcase(path) == os.path.normcase(_source(view))):
                _error("Save As exports a separate copy. Use Save or Convert Format for the current source.")
                return
            if os.path.exists(path) and not sublime.ok_cancel_dialog("Replace {}?".format(path), "Replace"):
                return
            try:
                text = _text(view)
                payload = binary_from_xml(text) if output_format == "binary" else xml_bytes(text)
                atomic_write(path, payload, fingerprint(path))
                sublime.status_message("Plist Tools: exported " + path)
            except (OSError, ValueError, OverflowError, RecursionError) as error:
                _error(error)

        view.window().show_input_panel("Export {} PLIST to".format(output_format), base + "-copy" + (ext or ".plist"), export, None, None)

    def is_enabled(self, **kwargs):
        return _is_plist(self.view)


class PlistToolsConvertCommand(sublime_plugin.TextCommand):
    def run(self, edit, output_format):
        view = self.view
        try:
            if _source(view):
                _save_source(view, output_format=output_format)
                _saving.add(view.buffer_id())
                _native(view, "save")
                return
            path = view.file_name()
            if not path:
                view.run_command("plist_tools_save_as", {"output_format": output_format})
                return
            # A fresh view also isolates file-association history during XML to
            # binary conversion. The original closes before its bytes change.
            text = _text(view)
            binary_from_xml(text)  # validate before creating recovery state
            if not view.window():
                raise PlistError("The original view must be in a window before converting it.")
            if view.clones():
                raise PlistError("Close extra views of this original file before converting it to a managed PLIST view.")
            before = fingerprint(path)
            work, meta_path = _paths(path)
            for window in sublime.windows():
                for other in window.views():
                    if other.id() != view.id() and _source(other) and os.path.normcase(_source(other)) == os.path.normcase(path):
                        raise PlistError("This source already has a managed PLIST view. Convert that view instead.")
            if os.path.isfile(meta_path):
                previous = _read_meta(meta_path)
                if previous.get("pending") or previous.get("working_pending"):
                    raise PlistError("This source has pending recovered edits. Open Recover Pending Edits before converting it.")
            meta = {"version": 1, "source": os.path.abspath(path), "format": "xml",
                    "fingerprint": before, "saved_text_hash": "", "pending": True}
            atomic_write(work, text.encode("utf-8"), fingerprint(work))
            write_metadata(meta_path, meta)
            _open_working_view(view, work, meta_path, meta,
                lambda working: working.run_command("plist_tools_convert", {"output_format": output_format}))
        except (OSError, ValueError, OverflowError, RecursionError) as error:
            if _source(view):
                _persist_pending(view, error)
            _error(error)

    def is_enabled(self, output_format):
        return _is_plist(self.view)


class PlistToolsRevertCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        if (view.is_dirty() or _meta(view).get("pending")) and not sublime.ok_cancel_dialog(
                "Discard PLIST edits and reload the source from disk?", "Revert"):
            return
        try:
            meta = _meta(view)
            before = fingerprint(meta["source"])
            with open(meta["source"], "rb") as handle:
                data = handle.read(int(settings().get("max_binary_bytes", 8 * 1024 * 1024)) + 1)
            if len(data) > int(settings().get("max_binary_bytes", 8 * 1024 * 1024)):
                raise PlistError("Source exceeds the configured max_binary_bytes limit.")
            binary = data.startswith(b"bplist00")
            text = load_binary(data) if binary else data.decode("utf-8-sig")
            parse_xml(text)
            if fingerprint(meta["source"]) != before:
                raise ConflictError("Source changed while reloading.")
            atomic_write(view.file_name(), text.encode("utf-8"), fingerprint(view.file_name()))
            meta.update({"format": "binary" if binary else "xml", "fingerprint": before,
                         "saved_text_hash": text_hash(text), "pending": False, "working_pending": False})
            write_metadata(view.settings().get(META), meta)
            _bind(view, view.settings().get(META), meta)
            _native(view, "revert")
            _check_conflict(view)
        except (OSError, ValueError, RecursionError) as error:
            _error(error)


class PlistToolsValidateCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if _diagnose(self.view):
            sublime.status_message("Plist Tools: valid property list")
        else:
            cached = _analysis.get(self.view.id())
            if cached and cached[2]:
                _goto(self.view, getattr(cached[2], "offset", 0))

    def is_enabled(self):
        return _is_plist(self.view)


class PlistToolsFormatCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        try:
            indent = " " * self.view.settings().get("tab_size", 4) if self.view.settings().get("translate_tabs_to_spaces") else "\t"
            text = format_xml(_text(self.view), indent)
            self.view.replace(edit, sublime.Region(0, self.view.size()), text)
        except (ValueError, RecursionError) as error:
            _diagnose(self.view)
            _error(error)

    def is_enabled(self):
        return _is_plist(self.view) and not self.view.is_read_only()


class PlistToolsOutlineCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        document = _document(self.view)
        if not document:
            self.view.run_command("plist_tools_validate")
            return
        entries = [n for n in document.entries if n.tag not in ("plist", "key")]
        items = [[n.path, n.tag + (" · " + str(n.value)[:100].replace("\n", " ") if n.tag not in ("dict", "array", "data") else "")] for n in entries]
        self.view.window().show_quick_panel(items, lambda index: _goto(self.view, entries[index].start) if index >= 0 else None)

    def is_enabled(self):
        return _is_plist(self.view)


class PlistToolsCopyKeyPathCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        document = _document(self.view)
        if document and self.view.sel():
            path = document.node_at(self.view.sel()[0].begin()).path
            sublime.set_clipboard(path)
            sublime.status_message("Plist Tools: copied " + path)

    def is_enabled(self):
        return _is_plist(self.view)


class PlistToolsToggleBooleanCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        document = _document(self.view)
        if document and self.view.sel():
            node = document.node_at(self.view.sel()[0].begin())
            if node.tag in ("true", "false"):
                old = document.source[node.start:node.end]
                replacement = "false" if node.tag == "true" else "true"
                updated = re.sub(r"(</?)" + node.tag + r"\b", lambda match: match.group(1) + replacement, old)
                self.view.replace(edit, sublime.Region(node.start, node.end), updated)
            else:
                sublime.status_message("Plist Tools: put the caret on a boolean value")

    def is_enabled(self):
        return _is_plist(self.view) and not self.view.is_read_only()


class PlistToolsRecoverCommand(sublime_plugin.WindowCommand):
    def run(self):
        entries = []
        for name in os.listdir(_recovery_dir()):
            if not name.endswith(".json"):
                continue
            path = os.path.join(_recovery_dir(), name)
            try:
                meta = _read_meta(path)
                work = os.path.splitext(path)[0] + ".plist"
                snapshot = os.path.splitext(path)[0] + ".pending"
                read_path = snapshot if meta.get("working_pending") and os.path.isfile(snapshot) else work
                with open(read_path, "r", encoding="utf-8", newline="") as handle:
                    pending = text_hash(handle.read()) != meta.get("saved_text_hash")
                if pending or meta.get("pending") or meta.get("working_pending"):
                    entries.append((meta["source"], work))
            except (OSError, ValueError, KeyError):
                continue
        if not entries:
            sublime.status_message("Plist Tools: no pending recovery files")
            return
        self.window.show_quick_panel([item[0] for item in entries],
            lambda index: self.window.open_file(entries[index][1]) if index >= 0 else None)


class PlistToolsListener(sublime_plugin.EventListener):
    def on_load(self, view):
        _prepare(view)
        _finish_working_open(view)

    def on_activated(self, view):
        _prepare(view)

    def on_clone(self, view):
        _prepare(view)

    def on_modified(self, view):
        _schedule(view)

    def on_revert(self, view):
        _schedule(view)

    def on_reload(self, view):
        _schedule(view)

    def on_close(self, view):
        _analysis.pop(view.id(), None)
        _tickets.pop(view.id(), None)

    def on_text_command(self, view, command_name, args):
        if not _source(view) or view.settings().get(INTERNAL):
            return None
        if command_name == "save":
            return ("plist_tools_save_as", {}) if (args or {}).get("prompt") else ("plist_tools_save", {})
        if command_name in ("save_as", "prompt_save_as"):
            return ("plist_tools_save_as", {})
        if command_name == "revert":
            return ("plist_tools_revert", {})

    def on_window_command(self, window, command_name, args):
        if command_name == "save_all" and any(_source(view) for view in window.views()):
            return ("plist_tools_save_all", {})
        view = window.active_view()
        if view and _source(view) and command_name in ("save", "save_as", "prompt_save_as"):
            return ("plist_tools_window_save", {"save_as": command_name != "save" or bool((args or {}).get("prompt"))})

    def on_pre_save(self, view):
        if not _source(view):
            return
        # Native close/quit saves can bypass command interception. Their target is
        # still the private XML working copy; commit before native marks it clean.
        try:
            _check_working_target(view)
            if view.buffer_id() in _saving:
                _saving.discard(view.buffer_id())
                return
            _save_source(view)
        except (OSError, ValueError, OverflowError, RecursionError) as error:
            _failed_save[view.buffer_id()] = str(error)
            _persist_pending(view, error)

    def on_post_save(self, view):
        _saving.discard(view.buffer_id())
        if view.settings().get(META):
            try:
                meta = _meta(view)
                meta["working_pending"] = False
                write_metadata(view.settings().get(META), meta)
                _bind(view, view.settings().get(META), meta)
            except (OSError, ValueError) as meta_error:
                view.set_status("plist_tools_save_error", "PLIST recovery metadata could not be updated: {}".format(meta_error))
        error = _failed_save.pop(view.buffer_id(), None)
        if error:
            view.run_command("plist_tools_mark_dirty")
            _error(error)
        _schedule(view)


class PlistToolsWindowSaveCommand(sublime_plugin.WindowCommand):
    def run(self, save_as=False):
        view = self.window.active_view()
        if view:
            view.run_command("plist_tools_save_as" if save_as else "plist_tools_save")


def plugin_loaded():
    for window in sublime.windows():
        for view in window.views():
            _prepare(view)


def plugin_unloaded():
    # Working copies stay detached even while the package reloads/is disabled.
    # Native save can only write XML recovery, never overwrite a binary source.
    _analysis.clear()
    _tickets.clear()
    for original, meta_path, meta, group, index, was_read_only, after_open in _opening.values():
        if original.is_valid():
            original.settings().erase("plist_tools_opening_work")
            original.set_read_only(was_read_only)
    _opening.clear()
