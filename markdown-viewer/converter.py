"""A small, dependency-free Markdown to HTML converter.

Supports the common subset of CommonMark/GFM: ATX and setext headings,
paragraphs, hard line breaks, emphasis, strikethrough, inline code, fenced and
indented code blocks, blockquotes, ordered/unordered/task lists, tables,
horizontal rules, inline and reference links, images and autolinks. Also
supports a few popular extensions: GitHub alerts (``> [!NOTE]``), heading
anchors with a ``[TOC]`` marker, YAML/TOML front matter, emoji shortcodes
(``:rocket:``) and ``==highlighted==`` text. Raw HTML is escaped, not passed
through. Code blocks can be syntax highlighted through a ``highlight``
callback, so the highlighter itself can live elsewhere.

This module does not import ``sublime`` so it can be tested outside the editor.
"""

import html
import os
import re
from pathlib import Path
from urllib.parse import unquote

__all__ = ["markdown_to_html"]

TAB_SIZE = 4

# minihtml has no tables, so table cells are fixed-width inline blocks sized
# from their text length. minihtml supports no percentage widths, max-width or
# vertical-align, and text can't be measured, so widths are estimates with
# headroom: in Sublime's UI font, text averages about 0.51rem per character and
# bold text about 0.60rem. Text that doesn't fit wraps within its cell.
TABLE_CHAR_WIDTH_REM = 0.6
TABLE_HEADER_CHAR_WIDTH_REM = 0.68
TABLE_CELL_PADDING_REM = 0.6
TABLE_MIN_CHARS = 3
TABLE_MAX_CHARS = 32

_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*([^`\s]*)[^`]*$")
_BLOCKQUOTE = re.compile(r"^ {0,3}> ?(.*)$")
_LIST_ITEM = re.compile(r"^( {0,3})([-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$")
_TASK = re.compile(r"^\[([ xX])\][ \t]+")
_TABLE_DELIMITER = re.compile(
    r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$"
)
_TABLE_PIPE = re.compile(r"(?<!\\)\|")
_TAG = re.compile(r"<[^>]*>")
_ALERT = re.compile(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*$", re.I)
_TOC = re.compile(r"^(?:\[TOC\]|\[\[_TOC_\]\])$", re.I)
_TOC_PLACEHOLDER = "\x00TOC\x00"
_FRONT_MATTER_KEY = re.compile(r"^[\w\"'.-]+[ \t]*[:=]")
_SLUG_STRIP = re.compile(r"[^\w\- ]")
_REF_DEF = re.compile(
    r"""^ {0,3}\[([^\]]+)\]:[ \t]*<?([^\s>]+)>?(?:[ \t]+["'(](.*)["')])?[ \t]*$"""
)

_CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)", re.S)
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>|~])")
_AUTOLINK = re.compile(r"<((?:https?|ftp|mailto):[^\s<>]+)>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^\s)>]*)>?(?:\s+\"([^\"]*)\")?\s*\)")
_LINK = re.compile(r"\[([^\]]+)\]\(\s*<?([^\s)>]*)>?(?:\s+\"([^\"]*)\")?\s*\)")
_REF_LINK = re.compile(r"(!?)\[([^\]]+)\](?:\[([^\]]*)\])?")
# GFM extended autolinks: a "www." link must start a word or follow "*_~(".
_BARE_URL = re.compile(r"\bhttps?://[^\s<>\x00]+|(?<![^\s*_~(])www\.[^\s<>\x00]+")
_EMOJI_CODE = re.compile(r":([a-z0-9_+-]+):")
_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", re.S)
_EM = re.compile(r"\*(?=\S)(.+?)(?<=\S)\*|(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", re.S)
# GFM strikethrough: one or two tildes, with the same number on both sides.
_STRIKE = re.compile(r"(?<!~)(~~?)(?=[^\s~])(.+?)(?<=[^\s~])\1(?!~)", re.S)
_MARK = re.compile(r"(?<!=)==(?=\S)(.+?)(?<=\S)==(?!=)", re.S)
_HARD_BREAK = re.compile(r"(?: {2,}|\\)\n")
_TOKEN = re.compile(r"\x00(\d+)\x00")
_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def markdown_to_html(text, base_dir=None, minihtml=False, show_front_matter=True,
                     highlight=None):
    """Convert Markdown ``text`` to an HTML fragment.

    ``base_dir`` turns relative link and image paths into absolute ``file://``
    URLs, so they still resolve when the HTML is rendered elsewhere.

    ``minihtml`` targets Sublime Text's minihtml renderer, which lacks many
    HTML features, so some elements are emitted as styled ``<div>`` and
    ``<span>`` stand-ins instead.

    ``show_front_matter`` renders a leading YAML/TOML front matter block as a
    muted code block; otherwise it is left out.

    ``highlight`` is an optional ``highlight(code, language)`` callback for
    fenced code blocks that name a language. It returns the block's contents
    as highlighted HTML, in the same whitespace form as plain blocks for the
    target (``&nbsp;``/``<br>`` for minihtml, raw for ``<pre>``), or None to
    fall back to plain text.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    renderer = _Renderer(base_dir, minihtml, highlight)
    front_matter, lines = _split_front_matter(text.split("\n"))
    lines = renderer.collect_references(lines)
    body = renderer.render_blocks(lines)
    # The table of contents needs every heading, so it's filled in last.
    if _TOC_PLACEHOLDER in body:
        body = body.replace(_TOC_PLACEHOLDER, renderer.render_toc())
    if front_matter is not None and show_front_matter:
        code = renderer.render_code("\n".join(front_matter))
        body = '<div class="front-matter">{}</div>\n{}'.format(code, body)
    return body


class _Renderer:
    def __init__(self, base_dir, minihtml, highlight=None):
        self.base_dir = base_dir
        self.minihtml = minihtml
        self.highlight = highlight
        self.references = {}
        self.tokens = []
        self.headings = []
        self.slugs = {}

    # -- Block level ---------------------------------------------------------

    def collect_references(self, lines):
        """Strip ``[label]: url "title"`` definitions, remembering them."""
        kept = []
        in_fence = None
        for line in lines:
            fence = _FENCE_OPEN.match(line)
            if in_fence:
                if line.strip().startswith(in_fence):
                    in_fence = None
            elif fence:
                in_fence = fence.group(2)
            else:
                match = _REF_DEF.match(line)
                if match:
                    label = _normalize_label(match.group(1))
                    self.references.setdefault(label, (match.group(2), match.group(3)))
                    continue
            kept.append(line)
        return kept

    def render_blocks(self, lines, tight=False):
        out = []
        paragraph = []
        i = 0

        def flush_paragraph():
            if paragraph:
                out.append(self.render_paragraph(paragraph, tight))
                del paragraph[:]

        while i < len(lines):
            line = lines[i].expandtabs(TAB_SIZE)
            stripped = line.strip()

            if not stripped:
                flush_paragraph()
                i += 1
                continue

            # Table: a delimiter row under a header row with as many cells.
            # The header is the last line of the paragraph collected so far.
            if paragraph and "|" in line and _TABLE_DELIMITER.match(line):
                header = _split_table_row(paragraph[-1])
                aligns = [_cell_alignment(cell) for cell in _split_table_row(line)]
                if len(header) == len(aligns):
                    del paragraph[-1]
                    flush_paragraph()
                    i = self.render_table(lines, i + 1, header, aligns, out)
                    continue

            # Setext heading: an underline directly beneath paragraph text.
            setext = _SETEXT_UNDERLINE.match(line)
            if setext and paragraph:
                level = 1 if setext.group(1)[0] == "=" else 2
                out.append(self.render_heading(level, "\n".join(paragraph).strip()))
                del paragraph[:]
                i += 1
                continue

            fence = _FENCE_OPEN.match(line)
            if fence:
                flush_paragraph()
                i = self.render_fenced_code(lines, i, fence, out)
                continue

            heading = _ATX_HEADING.match(line)
            if heading:
                flush_paragraph()
                level = len(heading.group(1))
                out.append(self.render_heading(level, (heading.group(2) or "").strip()))
                i += 1
                continue

            if _HR.match(line):
                flush_paragraph()
                out.append('<div class="hr"></div>')
                i += 1
                continue

            if _BLOCKQUOTE.match(line):
                flush_paragraph()
                quoted = []
                while i < len(lines):
                    match = _BLOCKQUOTE.match(lines[i].expandtabs(TAB_SIZE))
                    if not match:
                        break
                    quoted.append(match.group(1))
                    i += 1
                alert = _ALERT.match(quoted[0].strip())
                if alert:
                    out.append(self.render_alert(alert.group(1).lower(), quoted[1:]))
                else:
                    out.append('<div class="blockquote">{}</div>'.format(self.render_blocks(quoted)))
                continue

            if _LIST_ITEM.match(line):
                flush_paragraph()
                i = self.render_list(lines, i, out)
                continue

            # Indented code block; a paragraph cannot be interrupted by one.
            if not paragraph and line.startswith(" " * TAB_SIZE):
                code = []
                while i < len(lines):
                    current = lines[i].expandtabs(TAB_SIZE)
                    if current.startswith(" " * TAB_SIZE):
                        code.append(current[TAB_SIZE:])
                    elif not current.strip():
                        code.append("")
                    else:
                        break
                    i += 1
                while code and not code[-1]:
                    code.pop()
                out.append(self.render_code("\n".join(code)))
                continue

            paragraph.append(line)
            i += 1

        flush_paragraph()
        return "\n".join(out)

    def render_paragraph(self, lines, tight):
        if len(lines) == 1 and _TOC.match(lines[0].strip()):
            return _TOC_PLACEHOLDER
        # Keep trailing spaces on all but the last line: they mark hard breaks.
        text = "\n".join(line.lstrip() for line in lines).rstrip()
        text = _HARD_BREAK.sub(lambda m: self.stash("<br>"), text)
        content = self.render_inline(text)
        return content if tight else "<p>{}</p>".format(content)

    def render_heading(self, level, text):
        content = self.render_inline(text)
        slug = self.unique_slug(_visible_text(content))
        self.headings.append((level, slug, content))
        return '<h{0} id="{1}">{2}</h{0}>'.format(level, slug, content)

    def unique_slug(self, text):
        """GitHub-style anchor: lowercase, punctuation dropped, spaces to hyphens.

        Like GitHub, this doesn't trim, so "🎉 Release" becomes "-release".
        """
        slug = _SLUG_STRIP.sub("", text.lower()).replace(" ", "-") or "section"
        count = self.slugs.get(slug, 0)
        self.slugs[slug] = count + 1
        return slug if count == 0 else "{}-{}".format(slug, count)

    def render_alert(self, kind, lines):
        icon, title = _ALERTS[kind]
        return '<div class="alert-{0}"><div class="alert-{0}-title">{1} {2}</div>\n{3}</div>'.format(
            kind, icon, title, self.render_blocks(lines))

    def render_toc(self):
        """Nested list of links to every heading, for the ``[TOC]`` marker."""
        if not self.headings:
            return ""
        base = min(level for level, _, _ in self.headings)
        parts = ['<div class="toc">']
        open_depth = -1
        for level, slug, content in self.headings:
            # Never nest more than one level deeper than the previous entry,
            # so skipped heading levels (h1 then h3) don't leave empty items.
            depth = min(level - base, open_depth + 1)
            if depth > open_depth:
                parts.append("<ul>")
            else:
                parts.append("</li>")
                while open_depth > depth:
                    parts.append("</ul></li>")
                    open_depth -= 1
            open_depth = depth
            link_text = html.escape(_visible_text(content), quote=False)
            parts.append('<li><a href="#{}">{}</a>'.format(slug, link_text))
        parts.append("</li>")
        while open_depth > 0:
            parts.append("</ul></li>")
            open_depth -= 1
        parts.append("</ul></div>")
        return "".join(parts)

    def render_fenced_code(self, lines, start, fence, out):
        indent = len(fence.group(1))
        marker = fence.group(2)
        language = fence.group(3)
        code = []
        i = start + 1
        while i < len(lines):
            line = lines[i].expandtabs(TAB_SIZE)
            closing = line.strip()
            if closing.startswith(marker[0] * len(marker)) and not closing.strip(marker[0]):
                i += 1
                break
            # Remove up to the opening fence's indentation from content lines.
            code.append(re.sub(r"^ {0,%d}" % indent, "", line))
            i += 1
        out.append(self.render_code("\n".join(code), language))
        return i

    def render_code(self, code, language=""):
        css_class = ' class="language-{}"'.format(html.escape(language)) if language else ""
        body = self.highlight(code, language) if self.highlight and language else None
        highlighted = body is not None
        if not highlighted:
            body = html.escape(code, quote=False)
            if self.minihtml:
                body = body.replace(" ", "&nbsp;").replace("\n", "<br>")
        if self.minihtml:
            return '<div class="code-block"><code{}>{}</code></div>'.format(css_class, body)
        pre_class = ' class="highlighted"' if highlighted else ""
        return "<pre{}><code{}>{}</code></pre>".format(pre_class, css_class, body)

    def render_list(self, lines, start, out):
        first = _LIST_ITEM.match(lines[start].expandtabs(TAB_SIZE))
        ordered = first.group(2)[-1] in ".)"
        delimiter = first.group(2)[-1]
        items = []
        loose = False
        i = start

        while i < len(lines):
            line = lines[i].expandtabs(TAB_SIZE)
            match = _LIST_ITEM.match(line)
            if not match or _starts_new_list(match, ordered, delimiter) or _HR.match(line):
                break

            # Continuation lines must be indented to the item's text column.
            marker_width = len(match.group(1)) + len(match.group(2))
            first_text = match.group(3) or ""
            rest = line[marker_width:]
            content_indent = marker_width + (len(rest) - len(rest.lstrip()) if first_text else 1)
            item_lines = [first_text]
            i += 1

            # Gather continuation lines: indented content and blank lines that
            # are followed by more indented content.
            while i < len(lines):
                current = lines[i].expandtabs(TAB_SIZE)
                if not current.strip():
                    next_index = i + 1
                    while next_index < len(lines) and not lines[next_index].strip():
                        next_index += 1
                    if next_index >= len(lines):
                        break
                    following = lines[next_index].expandtabs(TAB_SIZE)
                    indented = len(following) - len(following.lstrip()) >= content_indent
                    sibling = _LIST_ITEM.match(following)
                    if indented or (sibling and not _starts_new_list(sibling, ordered, delimiter)):
                        loose = True
                    if not indented:
                        break
                    item_lines.extend([""] * (next_index - i))
                    i = next_index
                    continue
                current_indent = len(current) - len(current.lstrip())
                if current_indent >= content_indent:
                    item_lines.append(current[content_indent:])
                elif _LIST_ITEM.match(current) or _block_start(current):
                    break
                else:
                    # Lazy paragraph continuation.
                    item_lines.append(current.strip())
                i += 1

            items.append(item_lines)

            # A blank line between items makes the whole list loose.
            if i < len(lines) and not lines[i].strip():
                next_index = i
                while next_index < len(lines) and not lines[next_index].strip():
                    next_index += 1
                sibling = next_index < len(lines) and _LIST_ITEM.match(
                    lines[next_index].expandtabs(TAB_SIZE)
                )
                if sibling and not _starts_new_list(sibling, ordered, delimiter):
                    loose = True
                    i = next_index

        bodies = []
        for item_lines in items:
            prefix = ""
            task = _TASK.match(item_lines[0])
            if task:
                prefix = "☑ " if task.group(1) in "xX" else "☐ "
                item_lines = [item_lines[0][task.end():]] + item_lines[1:]
            bodies.append((prefix, self.render_blocks(item_lines, tight=not loose)))

        start_number = int(first.group(2)[:-1]) if ordered else 1
        if ordered and self.minihtml:
            # minihtml draws a bullet for every <li>, so number items by hand.
            rendered = []
            for number, (prefix, body) in enumerate(bodies, start_number):
                prefix = "{}. {}".format(number, prefix)
                if body.startswith("<p>"):
                    body = "<p>" + prefix + body[len("<p>"):]
                else:
                    body = prefix + body
                rendered.append('<div class="ol-item">{}</div>'.format(body))
            out.append('<div class="ol">\n{}\n</div>'.format("\n".join(rendered)))
            return i

        tag = "ol" if ordered else "ul"
        start_attr = ' start="{}"'.format(start_number) if start_number != 1 else ""
        rendered = ["<li>{}{}</li>".format(prefix, body) for prefix, body in bodies]
        out.append("<{0}{1}>\n{2}\n</{0}>".format(tag, start_attr, "\n".join(rendered)))
        return i

    def render_table(self, lines, start, header, aligns, out):
        # Body rows run until a blank line or the start of another block.
        rows = []
        i = start
        while i < len(lines):
            line = lines[i].expandtabs(TAB_SIZE)
            if not line.strip() or _block_start(line) or _LIST_ITEM.match(line):
                break
            # Pad short rows and drop extra cells, as GFM does.
            cells = _split_table_row(line) + [""] * len(aligns)
            rows.append(cells[:len(aligns)])
            i += 1

        header = [self.render_inline(cell) for cell in header]
        rows = [[self.render_inline(cell) for cell in row] for row in rows]
        if self.minihtml:
            out.append(self._minihtml_table(header, aligns, rows))
        else:
            out.append(self._html_table(header, aligns, rows))
        return i

    def _html_table(self, header, aligns, rows):
        def row_html(cells, tag):
            return "<tr>{}</tr>".format("".join(
                "<{0}{1}>{2}</{0}>".format(
                    tag, ' style="text-align: {}"'.format(align) if align else "", cell)
                for align, cell in zip(aligns, cells)
            ))

        parts = ["<table>", "<thead>", row_html(header, "th"), "</thead>"]
        if rows:
            parts += ["<tbody>"] + [row_html(row, "td") for row in rows] + ["</tbody>"]
        parts.append("</table>")
        return "\n".join(parts)

    def _minihtml_table(self, header, aligns, rows):
        def clamp(chars):
            return min(max(chars, TABLE_MIN_CHARS), TABLE_MAX_CHARS)

        widths = []
        for column in range(len(aligns)):
            body = max([_text_length(row[column]) for row in rows] or [0])
            widths.append(max(
                clamp(_text_length(header[column])) * TABLE_HEADER_CHAR_WIDTH_REM,
                clamp(body) * TABLE_CHAR_WIDTH_REM,
            ))

        def row_html(cells, css_class):
            # No whitespace between cells: it would render as a gap.
            spans = "".join(
                '<span class="td" style="display: inline-block; width: {:.2f}rem;'
                ' padding: 0.2rem {}rem;{}">{}</span>'.format(
                    width, TABLE_CELL_PADDING_REM,
                    " text-align: {};".format(align) if align else "", cell)
                for width, align, cell in zip(widths, aligns, cells)
            )
            return '<div class="{}">{}</div>'.format(css_class, spans)

        rendered = [row_html(header, "tr-head")]
        for number, row in enumerate(rows):
            rendered.append(row_html(row, "tr-alt" if number % 2 else "tr"))
        # Fix the table width so row borders stop at the last column. The
        # extra 0.5rem absorbs pixel rounding, which would otherwise wrap the
        # last cell of a row onto a new line.
        total = sum(widths) + 2 * TABLE_CELL_PADDING_REM * len(widths) + 0.5
        return '<div class="table" style="width: {:.2f}rem">\n{}\n</div>'.format(
            total, "\n".join(rendered))

    # -- Inline level --------------------------------------------------------

    def stash(self, markup):
        """Store finished markup and return a placeholder for it."""
        self.tokens.append(markup)
        return "\x00{}\x00".format(len(self.tokens) - 1)

    def render_inline(self, text):
        # Order matters: anything stashed is protected from later passes.
        text = _CODE_SPAN.sub(self._code_span, text)
        text = _ESCAPE.sub(lambda m: self.stash(html.escape(m.group(1))), text)
        text = _AUTOLINK.sub(self._autolink, text)
        text = _IMAGE.sub(self._image, text)
        text = _LINK.sub(self._link, text)
        text = _REF_LINK.sub(self._reference, text)
        text = _BARE_URL.sub(self._bare_url, text)
        text = html.escape(text, quote=False)
        text = _EMOJI_CODE.sub(lambda m: _EMOJI.get(m.group(1), m.group(0)), text)
        text = _STRONG.sub(lambda m: "<strong>{}</strong>".format(m.group(1) or m.group(2)), text)
        text = _EM.sub(lambda m: "<em>{}</em>".format(m.group(1) or m.group(2)), text)
        # minihtml has no <del>, <mark> or line-through, so it gets styled spans.
        if self.minihtml:
            text = _STRIKE.sub(r'<span class="strike">\2</span>', text)
            text = _MARK.sub(r'<span class="mark">\1</span>', text)
        else:
            text = _STRIKE.sub(r"<del>\2</del>", text)
            text = _MARK.sub(r"<mark>\1</mark>", text)
        text = text.replace("\n", " ")
        return self.restore(text)

    def restore(self, text):
        while _TOKEN.search(text):
            text = _TOKEN.sub(lambda m: self.tokens[int(m.group(1))], text)
        return text

    def _code_span(self, match):
        code = match.group(2).replace("\n", " ")
        if code.startswith(" ") and code.endswith(" ") and code.strip():
            code = code[1:-1]
        return self.stash("<code>{}</code>".format(html.escape(code, quote=False)))

    def _autolink(self, match):
        url = match.group(1)
        return self.stash(self._anchor(url, html.escape(url, quote=False)))

    def _bare_url(self, match):
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,:;!?)'\"":
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            trailing = url[-1] + trailing
            url = url[:-1]
        href = url if url.startswith(("http://", "https://")) else "http://" + url
        return self.stash(self._anchor(href, html.escape(url, quote=False))) + trailing

    def _image(self, match):
        alt, url, title = match.groups()
        return self.stash(self._img(url, alt, title))

    def _link(self, match):
        label, url, title = match.groups()
        return self.stash(self._anchor(url, self.render_inline(label), title))

    def _reference(self, match):
        is_image, label, ref = match.groups()
        key = _normalize_label(ref or label)
        if key not in self.references:
            return match.group(0)
        url, title = self.references[key]
        if is_image:
            return self.stash(self._img(url, label, title))
        return self.stash(self._anchor(url, self.render_inline(label), title))

    def _anchor(self, url, content, title=None):
        title_attr = ' title="{}"'.format(html.escape(title)) if title else ""
        href = html.escape(self.resolve_url(url))
        return '<a href="{}"{}>{}</a>'.format(href, title_attr, content)

    def _img(self, url, alt, title=None):
        title_attr = ' title="{}"'.format(html.escape(title)) if title else ""
        src = html.escape(self.resolve_url(url))
        return '<img src="{}" alt="{}"{}>'.format(src, html.escape(alt or ""), title_attr)

    def resolve_url(self, url):
        """Make relative paths absolute ``file://`` URLs under ``base_dir``."""
        if not self.base_dir or not url or url.startswith("#") or _URL_SCHEME.match(url):
            return url
        path, _, fragment = url.partition("#")
        path = os.path.normpath(os.path.join(self.base_dir, unquote(path)))
        resolved = Path(path).as_uri()
        return resolved + "#" + fragment if fragment else resolved


def _normalize_label(label):
    return " ".join(label.lower().split())


def _starts_new_list(match, ordered, delimiter):
    """True if a list item belongs to a different list than the current one."""
    marker = match.group(2)
    return (marker[-1] in ".)") != ordered or marker[-1] != delimiter


def _split_table_row(line):
    """Split ``| a | b |`` into cells; outer pipes are optional, ``\\|`` is literal."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _TABLE_PIPE.split(line)]


def _cell_alignment(delimiter):
    delimiter = delimiter.strip()
    if delimiter.startswith(":") and delimiter.endswith(":"):
        return "center"
    if delimiter.endswith(":"):
        return "right"
    if delimiter.startswith(":"):
        return "left"
    return None


def _visible_text(markup):
    """The text of rendered inline ``markup``, without tags or entities."""
    return html.unescape(_TAG.sub("", markup))


def _text_length(markup):
    return len(_visible_text(markup))


def _split_front_matter(lines):
    """Split a leading ``---`` YAML or ``+++`` TOML block from the document.

    Returns ``(front_matter_lines, remaining_lines)``; the first item is None
    when there is no front matter.
    """
    if not lines or lines[0].rstrip() not in ("---", "+++"):
        return None, lines
    closers = ("---", "...") if lines[0].rstrip() == "---" else ("+++",)
    for end in range(1, len(lines)):
        if lines[end].rstrip() in closers:
            body = lines[1:end]
            # A rule followed by prose isn't front matter: the first line with
            # content must look like "key: value" (YAML) or "key = value" (TOML).
            first = next(
                (line for line in body if line.strip() and not line.lstrip().startswith("#")),
                None,
            )
            if first is None or not _FRONT_MATTER_KEY.match(first):
                return None, lines
            return body, lines[end + 1:]
    return None, lines


def _block_start(line):
    return bool(
        _ATX_HEADING.match(line)
        or _FENCE_OPEN.match(line)
        or _BLOCKQUOTE.match(line)
        or _HR.match(line)
    )


# GitHub alert kinds: (icon, title).
_ALERTS = {
    "note": ("ℹ️", "Note"),
    "tip": ("💡", "Tip"),
    "important": ("💬", "Important"),
    "warning": ("⚠️", "Warning"),
    "caution": ("🛑", "Caution"),
}

# A curated subset of GitHub's emoji shortcodes (gemoji names), covering
# common faces, hands, symbols and the gitmoji set used in changelogs.
_EMOJI = {
    # Faces
    "grinning": "😀", "smiley": "😃", "smile": "😄", "grin": "😁", "laughing": "😆",
    "satisfied": "😆", "sweat_smile": "😅", "joy": "😂", "rofl": "🤣",
    "slightly_smiling_face": "🙂", "upside_down_face": "🙃", "wink": "😉", "blush": "😊",
    "innocent": "😇", "heart_eyes": "😍", "star_struck": "🤩", "kissing_heart": "😘",
    "yum": "😋", "stuck_out_tongue": "😛", "stuck_out_tongue_winking_eye": "😜",
    "hugs": "🤗", "thinking": "🤔", "neutral_face": "😐", "expressionless": "😑",
    "no_mouth": "😶", "smirk": "😏", "unamused": "😒", "roll_eyes": "🙄",
    "grimacing": "😬", "relieved": "😌", "pensive": "😔", "sleepy": "😪", "sleeping": "😴",
    "mask": "😷", "nerd_face": "🤓", "monocle_face": "🧐", "sunglasses": "😎",
    "confused": "😕", "worried": "😟", "slightly_frowning_face": "🙁", "open_mouth": "😮",
    "astonished": "😲", "flushed": "😳", "cry": "😢", "sob": "😭", "scream": "😱",
    "angry": "😠", "rage": "😡", "exploding_head": "🤯", "partying_face": "🥳",
    "clown_face": "🤡", "skull": "💀", "hankey": "💩", "poop": "💩", "ghost": "👻",
    "alien": "👽", "robot": "🤖", "see_no_evil": "🙈",
    # Hands and people
    "+1": "👍", "thumbsup": "👍", "-1": "👎", "thumbsdown": "👎", "ok_hand": "👌",
    "clap": "👏", "wave": "👋", "raised_hands": "🙌", "pray": "🙏", "muscle": "💪",
    "point_up": "☝️", "point_up_2": "👆", "point_down": "👇", "point_left": "👈",
    "point_right": "👉", "raised_hand": "✋", "v": "✌️", "crossed_fingers": "🤞",
    "handshake": "🤝", "eyes": "👀", "brain": "🧠", "bow": "🙇", "facepalm": "🤦",
    "shrug": "🤷", "construction_worker": "👷", "technologist": "🧑‍💻",
    "bust_in_silhouette": "👤", "busts_in_silhouette": "👥", "children_crossing": "🚸",
    "wheelchair": "♿",
    # Hearts and celebration
    "heart": "❤️", "orange_heart": "🧡", "yellow_heart": "💛", "green_heart": "💚",
    "blue_heart": "💙", "purple_heart": "💜", "black_heart": "🖤", "broken_heart": "💔",
    "sparkling_heart": "💖", "star": "⭐", "star2": "🌟", "sparkles": "✨", "dizzy": "💫",
    "fire": "🔥", "zap": "⚡", "boom": "💥", "collision": "💥", "100": "💯", "tada": "🎉",
    "confetti_ball": "🎊", "balloon": "🎈", "gift": "🎁", "trophy": "🏆",
    "1st_place_medal": "🥇", "dart": "🎯", "checkered_flag": "🏁",
    "triangular_flag_on_post": "🚩", "rocket": "🚀", "bulb": "💡",
    # Status and symbols
    "warning": "⚠️", "x": "❌", "heavy_check_mark": "✔️", "white_check_mark": "✅",
    "ballot_box_with_check": "☑️", "question": "❓", "grey_question": "❔",
    "exclamation": "❗", "heavy_exclamation_mark": "❗", "grey_exclamation": "❕",
    "bangbang": "‼️", "no_entry": "⛔", "no_entry_sign": "🚫", "stop_sign": "🛑",
    "construction": "🚧", "rotating_light": "🚨", "ambulance": "🚑",
    "information_source": "ℹ️", "heavy_plus_sign": "➕", "heavy_minus_sign": "➖",
    "heavy_multiplication_x": "✖️", "red_circle": "🔴", "large_blue_circle": "🔵",
    "white_circle": "⚪", "black_circle": "⚫", "new": "🆕", "up": "🆙", "cool": "🆒",
    "free": "🆓", "ok": "🆗", "soon": "🔜", "arrow_up": "⬆️", "arrow_down": "⬇️",
    "arrow_left": "⬅️", "arrow_right": "➡️", "arrows_counterclockwise": "🔄",
    "rewind": "⏪", "twisted_rightwards_arrows": "🔀", "recycle": "♻️", "lock": "🔒",
    "unlock": "🔓", "key": "🔑", "bell": "🔔", "no_bell": "🔕", "mag": "🔍", "link": "🔗",
    "loud_sound": "🔊", "mute": "🔇", "zzz": "💤",
    # Objects and tools
    "bug": "🐛", "memo": "📝", "pencil": "📝", "pencil2": "✏️", "book": "📖",
    "books": "📚", "bookmark": "🔖", "bookmark_tabs": "📑", "label": "🏷️",
    "paperclip": "📎", "pushpin": "📌", "triangular_ruler": "📐", "straight_ruler": "📏",
    "calendar": "📆", "date": "📅", "hourglass": "⌛", "alarm_clock": "⏰",
    "stopwatch": "⏱️", "gear": "⚙️", "wrench": "🔧", "hammer": "🔨",
    "hammer_and_wrench": "🛠️", "nut_and_bolt": "🔩", "bricks": "🧱",
    "building_construction": "🏗️", "package": "📦", "truck": "🚚",
    "chart_with_upwards_trend": "📈", "chart_with_downwards_trend": "📉",
    "bar_chart": "📊", "mega": "📣", "loudspeaker": "📢", "speech_balloon": "💬",
    "email": "📧", "envelope": "✉️", "inbox_tray": "📥", "outbox_tray": "📤",
    "file_folder": "📁", "open_file_folder": "📂", "card_file_box": "🗃️",
    "page_facing_up": "📄", "clipboard": "📋", "computer": "💻", "iphone": "📱",
    "keyboard": "⌨️", "camera_flash": "📸", "art": "🎨", "lipstick": "💄",
    "wastebasket": "🗑️", "shield": "🛡️", "test_tube": "🧪", "alembic": "⚗️",
    "microscope": "🔬", "telescope": "🔭", "adhesive_bandage": "🩹", "pill": "💊",
    "moneybag": "💰", "dollar": "💵", "money_with_wings": "💸", "globe_with_meridians": "🌐",
    "house": "🏠", "bento": "🍱",
    # Food, nature and weather
    "coffee": "☕", "beer": "🍺", "beers": "🍻", "pizza": "🍕", "cake": "🍰",
    "birthday": "🎂", "apple": "🍎", "green_apple": "🍏", "egg": "🥚", "penguin": "🐧",
    "snake": "🐍", "whale": "🐳", "cat": "🐱", "dog": "🐶", "unicorn": "🦄",
    "seedling": "🌱", "evergreen_tree": "🌲", "deciduous_tree": "🌳",
    "four_leaf_clover": "🍀", "sunny": "☀️", "cloud": "☁️", "umbrella": "☔",
    "snowflake": "❄️", "rainbow": "🌈", "ocean": "🌊", "crescent_moon": "🌙",
    "earth_africa": "🌍", "earth_americas": "🌎", "earth_asia": "🌏",
}
