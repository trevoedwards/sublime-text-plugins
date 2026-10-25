"""Sublime UI: snapshot buffers on the UI thread; collect files off-thread."""
import os

import sublime
import sublime_plugin

from .core import Collector, Options, Piece, Request, canonical, git_paths, git_root, safe_label


SETTINGS_FILE = "ContextPack.sublime-settings"
PREVIEW_KEY = "context_pack_preview"


def source_path(view):
    # PlistTools binary-plist XML working copies retain their original identity.
    return view.settings().get("plist_tools_source_path") or view.file_name()


def eligible(view):
    return (view is not None and view.is_valid() and not view.is_loading()
            and not view.settings().get(PREVIEW_KEY)
            and not view.settings().get("is_widget"))


def snapshot(view, options, selections=False):
    path = source_path(view)
    label = view.name() or "Untitled {}".format(view.buffer_id())
    request = Request(path=path, label=label, modified=view.is_dirty())
    regions = [r for r in view.sel() if not r.empty()] if selections else []
    if regions:
        if sum(r.size() for r in regions) > options.max_file_bytes:
            request.error = "selected text exceeds max_file_bytes (all selections skipped)"
            return request
        request.text = ""
        request.pieces = []
        for region in regions:
            start, end = region.begin(), region.end()
            first = view.rowcol(start)[0] + 1
            last = view.rowcol(end - 1)[0] + 1
            request.pieces.append(Piece(view.substr(sublime.Region(start, end)), first, last))
    elif view.size() > options.max_file_bytes:
        request.error = "buffer exceeds max_file_bytes (entire buffer skipped)"
    else:
        request.text = view.substr(sublime.Region(0, view.size()))
    return request


def capture(window, options):
    """Only called on Sublime's main thread; workers never touch views/settings."""
    roots = list(window.folders())
    active = window.active_view()
    source_views = [view for view in window.views() if eligible(view) and not view.is_scratch()]
    if not roots:
        seen_roots = set()
        for view in source_views:
            path = source_path(view)
            if path:
                parent = os.path.dirname(os.path.abspath(path))
                if canonical(parent) not in seen_roots:
                    roots.append(parent)
                    seen_roots.add(canonical(parent))
    preferences = sublime.load_settings("Preferences.sublime-settings")
    global_patterns = []
    view_patterns = []
    for key in ("file_exclude_patterns", "folder_exclude_patterns", "binary_file_patterns"):
        global_patterns.extend(preferences.get(key, []) or [])
        if eligible(active):
            view_patterns.extend(active.settings().get(key, []) or [])
    native = {root: list(view_patterns) for root in roots}
    project_file = window.project_file_name()
    base = os.path.dirname(project_file) if project_file else os.getcwd()
    for folder in (window.project_data() or {}).get("folders", []):
        path = folder.get("path")
        if not path:
            continue
        path = os.path.abspath(os.path.join(base, path))
        patterns = native.setdefault(path, list(view_patterns))
        for key in ("file_exclude_patterns", "folder_exclude_patterns"):
            patterns.extend(folder.get(key, []) or [])
    buffers, requests, seen = {}, [], set()
    remaining = options.max_pack_bytes
    for view in source_views:
        if view.buffer_id() in seen:
            continue
        seen.add(view.buffer_id())
        if view.size() > remaining:
            request = Request(path=source_path(view), label=view.name() or "Untitled {}".format(view.buffer_id()),
                              error="open-buffer capture byte budget exceeded; narrow the open buffers")
        else:
            request = snapshot(view, options)
            if request.text is not None:
                byte_size = len(request.text.encode("utf-8"))
                if byte_size > remaining:
                    request.text = None
                    request.error = "open-buffer capture byte budget exceeded; narrow the open buffers"
                else:
                    remaining -= byte_size
        requests.append(request)
        if request.path:
            buffers[canonical(request.path)] = request
    return roots, native, global_patterns, buffers, requests


def show_preview(window, text):
    if not window.is_valid():
        return
    view = window.new_file()
    view.set_name("Context Pack — Preview.md")
    view.set_scratch(True)
    view.settings().set(PREVIEW_KEY, True)
    view.settings().set("word_wrap", True)
    view.settings().set("spell_check", False)
    view.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")
    view.run_command("append", {"characters": text})
    view.set_read_only(True)
    view.sel().clear()
    view.sel().add(sublime.Region(0))
    view.show(0)
    sublime.status_message("Context Pack preview ready. Review, then run Context Pack: Copy Preview.")


class ContextPackCommand(sublime_plugin.WindowCommand):
    def run(self, mode="selection", paths=None, files=None, dirs=None, include_diff=None):
        paths = list(paths or []) + list(files or []) + list(dirs or [])
        settings = sublime.load_settings(SETTINGS_FILE)
        data = {key: settings.get(key) for key in (
            "max_file_bytes", "max_pack_bytes", "max_files", "max_scan_files", "git_timeout",
            "respect_gitignore", "exclude_globs", "extra_exclude_globs", "allow_env_examples") if settings.has(key)}
        options = Options.from_dict(data)
        if include_diff is None:
            include_diff = bool(settings.get("include_git_diff", False))
        roots, native, global_patterns, buffers, tab_requests = capture(self.window, options)
        active = self.window.active_view()
        if mode in ("selection", "file"):
            if not eligible(active):
                sublime.status_message("Context Pack: select a source text buffer first.")
                return
            requests = [snapshot(active, options, mode == "selection")]
        elif mode == "tabs":
            requests = tab_requests
        else:
            requests = []
        if mode == "sidebar" and not paths:
            sublime.status_message("Context Pack: choose files or folders in the sidebar first.")
            return
        sublime.status_message("Context Pack: collecting a bounded snapshot…")
        window = self.window

        def worker():
            collector = Collector(roots, options, native, global_patterns)
            try:
                if mode == "sidebar":
                    chosen = collector.expand(paths)
                elif mode in ("files", "changed"):
                    repositories = []
                    for path in roots:
                        repo = git_root(path, options)
                        if repo and canonical(repo) not in {canonical(r) for r in repositories}:
                            repositories.append(repo)
                    if mode == "changed" and not repositories:
                        collector.notes.append("No Git repository found in the open project folders or folders containing open source buffers.")
                    chosen = []
                    for repo in repositories:
                        remaining = options.max_scan_files - len(chosen)
                        if remaining <= 0:
                            collector.notes.append("Git discovery stopped at max_scan_files; narrow the project or increase the limit.")
                            break
                        discovered = git_paths(repo, options, changed=mode == "changed")
                        if len(discovered) > remaining:
                            collector.notes.append("Git discovery stopped at max_scan_files; narrow the project or increase the limit.")
                        chosen.extend(Request(path=p) for p in discovered[:remaining])
                    if mode == "files" and not repositories:
                        collector.notes.append("Git unavailable or no repository: picker uses a bounded project-folder scan.")
                        chosen = collector.expand(roots)
                    if mode == "files":
                        # Filter sensitive/excluded entries before exposing the picker.
                        visible = []
                        for request in chosen:
                            root = collector.root_for(request.path)
                            reason = collector.reason(request.path, root)
                            if reason:
                                collector.skipped.append((collector.label_for(request.path, root), reason))
                            else:
                                request.label = os.path.relpath(request.path, root)
                                visible.append(request)
                        sublime.set_timeout(lambda: self.pick(window, collector, visible, buffers, include_diff), 0)
                        return
                else:
                    chosen = requests
                text = collector.build(chosen, buffers, include_diff)
            except Exception as error:
                text = "# Context Pack\n\nCollection failed: {}\n\nNo clipboard changes were made.\n".format(safe_label(type(error).__name__ + ": " + str(error)[:400]))
                print("Context Pack:", error)
            sublime.set_timeout(lambda: show_preview(window, text), 0)

        sublime.set_timeout_async(worker, 0)

    def pick(self, window, collector, requests, buffers, include_diff):
        if not window.is_valid():
            return
        selected = []
        available = list(requests)

        def show():
            if not window.is_valid():
                return
            items = [["Preview {} selected file(s)".format(len(selected)), "Generate the Markdown preview"],
                     ["Add first {} eligible file(s) and preview".format(min(len(available), collector.options.max_files - len(selected))),
                      "Limited by max_files; review the preview before copying"]]
            for request in available:
                items.append([request.label, "Select to add; then choose Preview selected files"])
            window.show_quick_panel(items, choose)

        def finish():
            def worker():
                try:
                    text = collector.build(selected, buffers, include_diff)
                except Exception as error:
                    text = "# Context Pack\n\nCollection failed: " + safe_label(str(error)[:400])
                sublime.set_timeout(lambda: show_preview(window, text), 0)
            sublime.set_timeout_async(worker, 0)

        def choose(index):
            if index < 0:
                return
            if index == 0:
                finish()
            elif index == 1:
                selected.extend(available[:max(0, collector.options.max_files - len(selected))])
                finish()
            elif len(selected) < collector.options.max_files:
                selected.append(available.pop(index - 2))
                sublime.set_timeout(show, 0)
            else:
                sublime.status_message("Context Pack: max_files reached; choose Preview selected files.")
                sublime.set_timeout(show, 0)

        show()


class ContextPackCopyPreviewCommand(sublime_plugin.TextCommand):
    def is_enabled(self):
        return bool(self.view.settings().get(PREVIEW_KEY))

    def run(self, edit):
        if self.is_enabled():
            sublime.set_clipboard(self.view.substr(sublime.Region(0, self.view.size())))
            sublime.status_message("Context Pack: preview copied to clipboard.")
