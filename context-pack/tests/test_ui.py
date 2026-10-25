"""Minimal editor doubles verify snapshot/clipboard and thread boundaries."""
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


class Region:
    def __init__(self, a, b=None):
        self.a, self.b = a, a if b is None else b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)

    def size(self):
        return self.end() - self.begin()

    def empty(self):
        return self.a == self.b


class Settings(dict):
    def set(self, key, value):
        self[key] = value

    def has(self, key):
        return key in self


class Selections(list):
    def add(self, region):
        self.append(region)


class View:
    next_id = 0

    def __init__(self, text="", path=None, dirty=False):
        View.next_id += 1
        self.id = View.next_id
        self.text, self.path, self.dirty = text, path, dirty
        self.config = Settings()
        self.regions = Selections([Region(0)])
        self.title = ""
        self.read_only = False
        self.scratch = False

    def check(self):
        if getattr(fake_sublime, "in_worker", False):
            raise AssertionError("view touched on worker thread")

    def settings(self):
        self.check()
        return self.config

    def is_valid(self):
        self.check()
        return True

    def is_loading(self):
        self.check()
        return False

    def is_dirty(self):
        self.check()
        return self.dirty

    def is_scratch(self):
        self.check()
        return self.scratch

    def file_name(self):
        self.check()
        return self.path

    def buffer_id(self):
        self.check()
        return self.id

    def name(self):
        self.check()
        return self.title

    def size(self):
        self.check()
        return len(self.text)

    def sel(self):
        self.check()
        return self.regions

    def substr(self, region):
        self.check()
        return self.text[region.begin():region.end()]

    def rowcol(self, point):
        self.check()
        before = self.text[:point]
        return before.count("\n"), len(before.rsplit("\n", 1)[-1])

    def set_name(self, value):
        self.title = value

    def set_scratch(self, value):
        self.scratch = value

    def assign_syntax(self, value):
        self.syntax = value

    def run_command(self, name, args):
        self.text += args["characters"]

    def set_read_only(self, value):
        self.read_only = value

    def show(self, point):
        pass


class Window:
    def __init__(self, views, root=None):
        self.open_views = views
        self.active = views[0] if views else None
        self.root = root

    def active_view(self):
        return self.active

    def folders(self):
        return [self.root] if self.root else []

    def project_file_name(self):
        return None

    def project_data(self):
        return None

    def views(self):
        return self.open_views

    def is_valid(self):
        return True

    def new_file(self):
        result = View()
        self.open_views.append(result)
        self.active = result
        return result


fake_sublime = types.ModuleType("sublime")
fake_sublime.Region = Region
fake_sublime.in_worker = False
fake_sublime.pending = []
fake_sublime.clipboard = "untouched"
fake_sublime.load_settings = lambda name: Settings()
fake_sublime.status_message = lambda text: None
fake_sublime.set_clipboard = lambda text: setattr(fake_sublime, "clipboard", text)
fake_sublime.set_timeout = lambda fn, delay=0: fake_sublime.pending.append(fn)


def worker(fn, delay=0):
    fake_sublime.in_worker = True
    try:
        fn()
    finally:
        fake_sublime.in_worker = False


fake_sublime.set_timeout_async = worker
fake_plugin = types.ModuleType("sublime_plugin")
fake_plugin.WindowCommand = type("WindowCommand", (), {"__init__": lambda self, window: setattr(self, "window", window)})
fake_plugin.TextCommand = type("TextCommand", (), {"__init__": lambda self, view: setattr(self, "view", view)})
sys.modules["sublime"] = fake_sublime
sys.modules["sublime_plugin"] = fake_plugin
package = types.ModuleType("_context_pack_test")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
ui = importlib.import_module("_context_pack_test.context_pack")


class UITests(unittest.TestCase):
    def setUp(self):
        fake_sublime.pending.clear()
        fake_sublime.clipboard = "untouched"

    def drain(self):
        while fake_sublime.pending:
            fake_sublime.pending.pop(0)()

    def test_reversed_selection_and_end_at_next_line(self):
        view = View("first\nsecond\nthird\n")
        view.regions = Selections([Region(6, 0), Region(13, 18)])
        request = ui.snapshot(view, ui.Options(), selections=True)
        self.assertEqual([(p.first, p.last) for p in request.pieces], [(1, 1), (3, 3)])
        self.assertEqual([p.text for p in request.pieces], ["first\n", "third"])

    def test_plist_identity_and_xml_content(self):
        view = View("<plist/>", path="recovery.xml", dirty=True)
        view.config["plist_tools_source_path"] = os.path.abspath("original.plist")
        request = ui.snapshot(view, ui.Options())
        self.assertTrue(request.path.endswith("original.plist"))
        self.assertEqual(request.text, "<plist/>")

    def test_preview_is_read_only_scratch_and_only_explicit_copy_changes_clipboard(self):
        window = Window([])
        ui.show_preview(window, "# Test\n")
        view = window.active
        self.assertTrue(view.read_only)
        self.assertTrue(view.scratch)
        self.assertFalse(ui.eligible(view))
        self.assertEqual(fake_sublime.clipboard, "untouched")
        ui.ContextPackCopyPreviewCommand(view).run(None)
        self.assertEqual(fake_sublime.clipboard, "# Test\n")
        self.assertFalse(ui.ContextPackCopyPreviewCommand(View()).is_enabled())

    def test_complete_tabs_command_never_reads_views_from_worker_or_includes_preview(self):
        source = View("unsaved λ", dirty=True)
        window = Window([source])
        ui.show_preview(window, "old generated output")
        window.active = source
        ui.ContextPackCommand(window).run(mode="tabs")
        self.drain()
        self.assertIn("unsaved λ", window.active.text)
        self.assertNotIn("old generated output", window.active.text)
        self.assertIn("1 item(s)", window.active.text)
        self.assertEqual(fake_sublime.clipboard, "untouched")

    def test_tabs_skip_scratch_find_results_but_explicit_selection_remains_available(self):
        source = View("source text", dirty=True)
        results = View(".env: SECRET_SHOULD_NOT_LEAK")
        results.scratch = True
        window = Window([source, results])
        ui.ContextPackCommand(window).run(mode="tabs")
        self.drain()
        self.assertIn("source text", window.active.text)
        self.assertNotIn("SECRET_SHOULD_NOT_LEAK", window.active.text)
        window.active = results
        ui.ContextPackCommand(window).run(mode="selection")
        self.drain()
        self.assertIn("SECRET_SHOULD_NOT_LEAK", window.active.text)

    def test_sidebar_files_dirs_arguments_and_unsaved_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "a.txt")
            Path(path).write_text("saved")
            view = View("unsaved", path=path, dirty=True)
            window = Window([view], root)
            ui.ContextPackCommand(window).run(mode="sidebar", files=[path], dirs=[root])
            self.drain()
            self.assertIn("unsaved", window.active.text)
            self.assertEqual(window.active.text.count("## a\\.txt"), 1)
            self.assertEqual(Path(path).read_text(), "saved")

    def test_selection_of_small_region_in_large_buffer(self):
        view = View("small" + "x" * 10000)
        view.regions = Selections([Region(0, 5)])
        request = ui.snapshot(view, ui.Options(max_file_bytes=20), selections=True)
        self.assertIsNone(request.error)
        self.assertEqual(request.pieces[0].text, "small")


if __name__ == "__main__":
    unittest.main()
