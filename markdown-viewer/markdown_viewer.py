import html
import os
import tempfile
import threading
import webbrowser
from collections import OrderedDict
from pathlib import Path

import sublime
import sublime_plugin

from .converter import markdown_to_html

SETTINGS_FILE = "MarkdownViewer.sublime-settings"

# Hidden output panel used to tokenize code blocks for syntax highlighting.
HIGHLIGHT_PANEL = "markdown_viewer_highlight"
HIGHLIGHT_CACHE_SIZE = 256

# Fence languages that match neither a syntax's file extension nor its name.
LANGUAGE_ALIASES = {
    "shell": "sh",
    "golang": "go",
    "csharp": "cs",
    "objc": "m",
    "python3": "py",
    "py3": "py",
    "node": "js",
    "scss": "css",
}

# GitHub alert colors: (preview color scheme variable, browser light, browser dark).
ALERT_COLORS = {
    "note": ("var(--bluish)", "#0969da", "#4493f8"),
    "tip": ("var(--greenish)", "#1a7f37", "#3fb950"),
    "important": ("var(--purplish)", "#8250df", "#ab7df8"),
    "warning": ("var(--yellowish)", "#9a6700", "#d29922"),
    "caution": ("var(--redish)", "#d1242f", "#f85149"),
}


def alert_css(rule, color_index):
    """Expand a CSS ``rule`` template once per alert kind."""
    return "\n".join(
        rule.format(kind=kind, color=colors[color_index])
        for kind, colors in ALERT_COLORS.items()
    )


# Styles for Sublime's minihtml renderer; colors follow the active color scheme.
SHEET_CSS = """
body {
    padding: 0.5rem 2rem 2rem 2rem;
    font-family: system;
    line-height: 1.5;
}
h1, h2 {
    padding-bottom: 0.3rem;
    border-bottom: 1px solid color(var(--foreground) alpha(0.15));
}
a {
    color: var(--bluish);
}
code {
    background-color: color(var(--foreground) alpha(0.08));
    border-radius: 3px;
    padding: 0 0.2rem;
}
.code-block {
    background-color: color(var(--foreground) alpha(0.06));
    border-radius: 4px;
    padding: 0.6rem 0.8rem;
    margin: 0.5rem 0;
}
.code-block code {
    background-color: transparent;
    padding: 0;
}
.blockquote {
    border-left: 4px solid color(var(--foreground) alpha(0.25));
    color: color(var(--foreground) alpha(0.75));
    padding-left: 1rem;
    margin: 0.5rem 0;
}
.hr {
    border-top: 1px solid color(var(--foreground) alpha(0.25));
    margin: 1rem 0;
}
.ol {
    margin: 0.5rem 0;
    padding-left: 1.5rem;
}
.table {
    margin: 0.5rem 0;
}
.tr, .tr-alt {
    border-bottom: 1px solid color(var(--foreground) alpha(0.12));
}
.tr-alt {
    background-color: color(var(--foreground) alpha(0.04));
}
.tr-head {
    font-weight: bold;
    background-color: color(var(--foreground) alpha(0.07));
    border-bottom: 1px solid color(var(--foreground) alpha(0.3));
}
.strike {
    color: color(var(--foreground) alpha(0.45));
}
.mark {
    background-color: color(var(--yellowish) alpha(0.3));
}
.front-matter {
    color: color(var(--foreground) alpha(0.6));
    font-size: 0.9rem;
}
.toc {
    margin: 0.5rem 0;
}
.toc ul {
    margin: 0;
}
""" + alert_css(
    ".alert-{kind} {{ border-left: 4px solid {color}; padding-left: 1rem; margin: 0.5rem 0; }}\n"
    ".alert-{kind}-title {{ color: {color}; font-weight: bold; }}",
    0,
)

# Styles for the standalone page opened in a web browser.
BROWSER_CSS = """
body {
    max-width: 50rem;
    margin: 0 auto;
    padding: 2rem;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.6;
    color: #1f2328;
    background: #ffffff;
}
h1, h2 { border-bottom: 1px solid #d1d9e0; padding-bottom: 0.3em; }
a { color: #0969da; }
code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: rgba(129, 139, 152, 0.15);
    border-radius: 4px;
    padding: 0.1em 0.3em;
}
pre { background: #f6f8fa; border-radius: 6px; padding: 1em; overflow: auto; }
pre code { background: none; padding: 0; }
img { max-width: 100%; }
table { border-collapse: collapse; margin: 1em 0; display: block; overflow: auto; }
th, td { border: 1px solid #d1d9e0; padding: 0.4em 0.8em; }
th { background: #f6f8fa; }
tbody tr:nth-child(even) { background: #f6f8fa; }
.blockquote { border-left: 4px solid #d1d9e0; color: #59636e; padding-left: 1em; margin: 1em 0; }
.hr { border-top: 1px solid #d1d9e0; margin: 1.5em 0; }
del { color: #59636e; }
mark { background: #fff8c5; color: inherit; }
.front-matter pre { color: #59636e; font-size: 0.85em; }
@media (prefers-color-scheme: dark) {
    body { color: #f0f6fc; background: #0d1117; }
    h1, h2, .hr, th, td { border-color: #3d444d; }
    a { color: #4493f8; }
    pre, th, tbody tr:nth-child(even) { background: #151b23; }
    .blockquote { border-color: #3d444d; color: #9198a1; }
    del, .front-matter pre { color: #9198a1; }
    mark { background: rgba(187, 128, 9, 0.4); }
}
""" + alert_css(
    ".alert-{kind} {{ border-left: 0.25em solid {color}; padding: 0.5em 1em; margin: 1em 0; }}\n"
    ".alert-{kind}-title {{ color: {color}; font-weight: 600; }}\n"
    ".alert-{kind} p {{ margin: 0.5em 0 0; }}",
    1,
) + "\n@media (prefers-color-scheme: dark) {\n" + alert_css(
    "    .alert-{kind} {{ border-color: {color}; }} .alert-{kind}-title {{ color: {color}; }}",
    2,
) + "\n}\n"

# Source view id -> HtmlSheet showing its preview.
_previews = {}
# Source view id -> number of edits seen, used to debounce live updates.
_edit_counts = {}
# Fence language -> Syntax (or None), and recently highlighted code blocks.
# Rendering runs on both the main and async threads, and they share the
# highlighting panel, so highlighting holds a lock.
_syntaxes = {}
_highlighted = OrderedDict()
_highlight_lock = threading.Lock()


def settings():
    return sublime.load_settings(SETTINGS_FILE)


def find_syntax(language):
    """The installed syntax for a fence language like "py" or "Python", or None."""
    token = language.lower()
    token = LANGUAGE_ALIASES.get(token, token)
    if token not in _syntaxes:
        # Try it as a file extension first, then as a syntax name. Unknown
        # extensions give Plain Text, which isn't worth highlighting.
        syntax = sublime.find_syntax_for_file("file." + token)
        if syntax is None or syntax.scope == "text.plain":
            syntax = next(
                (s for s in sublime.list_syntaxes()
                 if not s.hidden and s.name.lower() == token and s.scope != "text.plain"),
                None,
            )
        _syntaxes[token] = syntax
    return _syntaxes[token]


def highlight_panel(window):
    """The hidden, unlisted output panel used for highlighting. It's never shown."""
    return (window.find_output_panel(HIGHLIGHT_PANEL)
            or window.create_output_panel(HIGHLIGHT_PANEL, unlisted=True))


def make_highlighter(window, minihtml):
    """A converter ``highlight`` callback using Sublime's syntaxes and color scheme."""
    if window is None or not settings().get("syntax_highlighting", True):
        return None

    def highlight(code, language):
        syntax = find_syntax(language)
        if syntax is None:
            return None
        with _highlight_lock:
            panel = highlight_panel(window)
            # Include the color scheme so switching schemes re-highlights.
            scheme = (panel.settings().get("color_scheme"), panel.style().get("background"))
            key = (syntax.path, scheme, minihtml, code)
            if key in _highlighted:
                _highlighted.move_to_end(key)
                return _highlighted[key]

            panel.assign_syntax(syntax)
            panel.run_command("markdown_viewer_set_text", {"text": code})
            panel.clear_undo_stack()
            result = panel.export_to_html(
                minihtml=minihtml, enclosing_tags=False, font_size=False, font_family=False)
            if not minihtml:
                # Inside <pre>, real newlines copy and paste better than <br>.
                result = result.replace("<br>", "\n")

            _highlighted[key] = result
            if len(_highlighted) > HIGHLIGHT_CACHE_SIZE:
                _highlighted.popitem(last=False)
            return result

    return highlight


def highlight_css(window):
    """Browser CSS giving highlighted code blocks the color scheme's colors."""
    if window is None or not settings().get("syntax_highlighting", True):
        return ""
    style = highlight_panel(window).style()
    return "\npre.highlighted {{ background: {}; color: {}; }}\n".format(
        style.get("background"), style.get("foreground"))


def is_markdown(view):
    if view is None:
        return False
    if view.match_selector(0, "text.html.markdown"):
        return True
    name = view.file_name() or ""
    return name.lower().endswith((".md", ".markdown", ".mdown", ".mkd"))


def render(view, minihtml):
    text = view.substr(sublime.Region(0, view.size()))
    file_name = view.file_name()
    base_dir = os.path.dirname(file_name) if file_name else None
    return markdown_to_html(
        text,
        base_dir=base_dir,
        minihtml=minihtml,
        show_front_matter=settings().get("show_front_matter", True),
        highlight=make_highlighter(view.window(), minihtml),
    )


def preview_title(view):
    name = os.path.basename(view.file_name() or "") or view.name() or "untitled"
    return "Preview: " + name


def sheet_html(view):
    return '<html><body id="markdown-viewer"><style>{}</style>{}</body></html>'.format(
        SHEET_CSS, render(view, minihtml=True)
    )


def get_preview(view):
    """Return the open preview sheet for ``view``, forgetting closed ones."""
    sheet = _previews.get(view.id())
    if sheet is not None and sheet.window() is None:
        del _previews[view.id()]
        sheet = None
    return sheet


def update_preview(view):
    sheet = get_preview(view)
    if sheet is not None and view.is_valid():
        sheet.set_name(preview_title(view))
        sheet.set_contents(sheet_html(view))


class MarkdownViewerTogglePreviewCommand(sublime_plugin.TextCommand):
    """Open a live preview next to the current file, or close it if open."""

    def run(self, edit):
        view = self.view
        window = view.window()
        sheet = get_preview(view)
        if sheet is not None:
            sheet.close()
            del _previews[view.id()]
            return

        # Add the preview to the selected tabs so both show side by side.
        sheet = window.new_html_sheet(
            preview_title(view),
            sheet_html(view),
            flags=sublime.ADD_TO_SELECTION,
            group=window.active_group(),
        )
        _previews[view.id()] = sheet
        window.focus_view(view)

    def is_enabled(self):
        return is_markdown(self.view)


class MarkdownViewerOpenInBrowserCommand(sublime_plugin.TextCommand):
    """Render the file to a temporary HTML page and open it in a browser."""

    def run(self, edit):
        view = self.view
        title = preview_title(view)[len("Preview: "):]
        page = (
            '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n'
            "<title>{}</title>\n<style>{}</style>\n</head>\n<body>\n{}\n</body>\n</html>\n"
        ).format(
            html.escape(title),
            BROWSER_CSS + highlight_css(view.window()),
            render(view, minihtml=False),
        )

        directory = os.path.join(tempfile.gettempdir(), "sublime-markdown-viewer")
        os.makedirs(directory, exist_ok=True)
        stem = os.path.splitext(title)[0] or "untitled"
        path = os.path.join(directory, "{}-{}.html".format(stem, view.id()))
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)

        webbrowser.open_new_tab(Path(path).as_uri())
        sublime.status_message("Markdown Viewer: opened " + path)

    def is_enabled(self):
        return is_markdown(self.view)


class MarkdownViewerSetTextCommand(sublime_plugin.TextCommand):
    """Replace the whole buffer. Internal: fills the highlighting panel."""

    def run(self, edit, text):
        self.view.replace(edit, sublime.Region(0, self.view.size()), text)


class MarkdownViewerListener(sublime_plugin.EventListener):
    def on_modified_async(self, view):
        if not settings().get("live_update", True) or get_preview(view) is None:
            return
        # Debounce: only re-render once typing has paused. Async events can
        # arrive after several edits, so count events rather than reading
        # view.change_count(), which would already reflect the later edits.
        view_id = view.id()
        edit_count = _edit_counts.get(view_id, 0) + 1
        _edit_counts[view_id] = edit_count

        def update():
            if _edit_counts.get(view_id) == edit_count:
                update_preview(view)

        sublime.set_timeout_async(update, settings().get("update_delay", 300))

    def on_post_save_async(self, view):
        # Saving can change the file name, and with it the title and base path.
        update_preview(view)

    def on_close(self, view):
        _edit_counts.pop(view.id(), None)
        sheet = _previews.pop(view.id(), None)
        if sheet is not None and sheet.window() is not None:
            sheet.close()
