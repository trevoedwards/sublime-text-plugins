# Markdown Viewer

A small Sublime Text 4 plugin for previewing Markdown files. It has no
dependencies: the Markdown converter is built in.

- **Live preview.** Opens a rendered preview beside the file in an HTML tab
  and updates it as you type.
- **Open in browser.** Renders the file to a standalone HTML page and opens it
  in your web browser.

## Installation

Link or copy this directory into your Sublime Text `Packages` folder and name
it `MarkdownViewer`. Sublime loads it right away, with no restart needed.

To find the `Packages` folder, use **Preferences → Browse Packages…**, or:

| OS      | Packages folder                                |
| ------- | ---------------------------------------------- |
| Linux   | `~/.config/sublime-text/Packages`              |
| macOS   | `~/Library/Application Support/Sublime Text/Packages` |
| Windows | `%APPDATA%\Sublime Text\Packages`              |

For example, on Linux:

```sh
ln -s "$PWD/markdown-viewer" ~/.config/sublime-text/Packages/MarkdownViewer
```

On Windows (from an elevated Command Prompt):

```bat
mklink /D "%APPDATA%\Sublime Text\Packages\MarkdownViewer" C:\path\to\sublime-text-plugins\markdown-viewer
```

The package folder must be named `MarkdownViewer`. The settings command
looks for the default settings file at that path.

## Usage

Open a Markdown file and run one of these from the Command Palette
(<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd>):

| Command                                  | What it does                                   |
| ---------------------------------------- | ---------------------------------------------- |
| `Markdown Viewer: Toggle Preview`        | Opens the live preview beside the file, or closes it. |
| `Markdown Viewer: Open in Browser`       | Opens the rendered file in your default browser. |
| `Preferences: Markdown Viewer Settings`  | Opens the settings side by side with the defaults. |

The commands are enabled for files with Markdown syntax or a `.md`,
`.markdown`, `.mdown` or `.mkd` extension. Closing a file also closes its
preview.

### Key binding

The plugin has no default key bindings, so it won't conflict with yours. To
add one, open **Preferences → Key Bindings** and add:

```json
{
    "keys": ["ctrl+alt+m"],
    "command": "markdown_viewer_toggle_preview",
    "context": [{ "key": "selector", "operator": "equal", "operand": "text.html.markdown" }]
}
```

## Settings

| Setting        | Default | Description                                            |
| -------------- | ------- | ------------------------------------------------------ |
| `live_update`  | `true`  | Re-render the preview while you type.                  |
| `update_delay` | `300`   | Milliseconds to wait after the last edit before re-rendering. |
| `show_front_matter` | `true` | Show YAML/TOML front matter as a muted block. Set to `false` to hide it. |
| `syntax_highlighting` | `true` | Syntax highlight fenced code blocks that name a language. |

## Supported Markdown

- ATX (`# Heading`) and setext (underlined) headings
- Paragraphs and hard line breaks (two trailing spaces or a trailing `\`)
- `**bold**`, `__bold__`, `*italic*`, `_italic_` and `` `inline code` ``
- `~~strikethrough~~` (or `~single~`) and `==highlighted==` text
- Fenced (```` ``` ```` / `~~~`) and indented code blocks, with syntax
  highlighting (see below)
- Blockquotes, which can contain other blocks
- Ordered, unordered, nested and task (`- [x]`) lists
- GitHub-style tables, with column alignment (`:---`, `:---:`, `---:`) and
  escaped pipes (`\|`)
- Horizontal rules
- Inline and reference-style links, images, `<autolinks>`, bare URLs and
  `www.` links
- Backslash escapes

Relative link and image paths resolve against the Markdown file's folder.

### Extensions

- **GitHub alerts.** A blockquote whose first line is `[!NOTE]`, `[!TIP]`,
  `[!IMPORTANT]`, `[!WARNING]` or `[!CAUTION]` becomes a colored callout:

  ```markdown
  > [!WARNING]
  > Back up your settings first.
  ```

- **Table of contents.** A line containing only `[TOC]` (or GitLab's
  `[[_TOC_]]`) is replaced with a nested list of links to every heading.
  Headings get GitHub-style anchor ids, so `[link](#section-name)` links work
  in the browser too.
- **Front matter.** A YAML (`---`) or TOML (`+++`) block at the very top of
  the file is shown as a muted block, not as a rule and stray heading. Use
  the `show_front_matter` setting to hide it.
- **Syntax highlighting.** Fenced code blocks that name a language, such as
  ```` ```python ````, are highlighted with Sublime's own syntax definitions
  and your color scheme, so they look the way they do in the editor. The
  language can be a file extension (`py`, `rs`, `sh`) or a syntax name
  (`python`, `rust`, `bash`). Any syntax you've installed works too, such as
  one from Package Control. Blocks with an unknown or no language stay plain.
  In the browser, highlighted blocks keep the color scheme's background.
- **Emoji shortcodes.** About 250 of GitHub's common codes, such as
  `:rocket:`, `:tada:`, `:+1:` and `:white_check_mark:`, plus the gitmoji
  set. Unknown codes are left as typed.

## Limitations

- The in-editor preview uses Sublime's [minihtml](https://www.sublimetext.com/docs/minihtml.html),
  which supports only a subset of HTML and CSS. Remote (`http://`) images
  don't load in the preview, but they do in **Open in Browser**.
- minihtml has no tables, so the preview lays tables out as fixed-width
  columns sized from their text length. Text in a cell that's wider than
  estimated wraps within the cell, and the row's other cells line up with its
  last line. A table wider than the preview pane is cut off at the edge. For
  wide tables, use **Open in Browser**, which renders a real HTML table.
- Links aren't clickable in the preview: Sublime's HTML tabs don't follow
  them. This includes table-of-contents links. They all work in
  **Open in Browser**.
- minihtml has no strikethrough, so struck-out text is dimmed in the preview
  instead. The browser shows a real strikethrough.
- Footnotes aren't supported.
- Raw HTML in the Markdown is escaped and shown as text, not rendered.

## Files

| File                               | Purpose                                         |
| ---------------------------------- | ----------------------------------------------- |
| `markdown_viewer.py`               | Commands and the live-update event listener.    |
| `converter.py`                     | Markdown to HTML converter. It doesn't import `sublime`. |
| `Default.sublime-commands`         | Command Palette entries.                        |
| `MarkdownViewer.sublime-settings`  | Default settings.                               |
| `.python-version`                  | Selects Sublime's Python 3 plugin host.         |

Because `converter.py` doesn't depend on Sublime, you can try it with plain
Python:

```sh
cd markdown-viewer
python3 -c 'from converter import markdown_to_html; print(markdown_to_html("# Hi\n\n*there*"))'
```
