# Context Pack

Build a Markdown snapshot of selected code for review and sharing. Context Pack
uses open buffers, so unsaved edits are included. It never saves your source
files. It has no network, model, or third-party Python dependencies.

## Installation

Link or copy this directory into Sublime Text 4's `Packages` folder as
**`ContextPack`**. Find that folder with **Preferences → Browse Packages…**.
The package name is required for the settings command.

| OS | Packages folder |
| --- | --- |
| Windows | `%APPDATA%\Sublime Text\Packages` |
| macOS | `~/Library/Application Support/Sublime Text/Packages` |
| Linux | `~/.config/sublime-text/Packages` |

Linux example, from the repository root:

```sh
ln -s "$PWD/context-pack" ~/.config/sublime-text/Packages/ContextPack
```

Windows example, from an elevated Command Prompt:

```bat
mklink /D "%APPDATA%\Sublime Text\Packages\ContextPack" C:\path\to\sublime-text-plugins\context-pack
```

Uses the Python 3.8 plugin host and also supports newer Sublime Text 4 hosts.
Git is optional; install it on Sublime's `PATH` for tracked-file discovery,
ignore rules, and diffs. The plugin uses literal arguments, no shell, a timeout
for each Git process, and hidden process windows on Windows.

## Usage

Run a command from the Command Palette:

| Command | Behavior |
| --- | --- |
| **Context Pack: Preview Selection (or Current File)** | Captures every nonempty selection separately, with its own original line range. With only empty carets, captures the entire active buffer. |
| **Context Pack: Preview Current File** | Captures the entire active text buffer, including unsaved changes or an untitled buffer. |
| **Context Pack: Preview Open Text Buffers** | Captures eligible buffers once, excluding scratch views (including Find Results/plugin output), Context Pack previews, and editor widgets. |
| **Context Pack: Choose Project Files** | Lists Git-tracked files in repositories containing project folders. Without Git/repositories, uses a bounded project-folder scan. Choose files repeatedly, then **Preview selected files**, or use the bounded **Add first… and preview** action. Escape cancels. |
| **Context Pack: Preview Changed Files with Git Diff** | Captures staged and unstaged tracked changes, plus nonignored untracked files, from project repositories. Adds eligible saved-file diffs. |
| **Context Pack: Copy Preview** | Copies the entire active Context Pack preview. Enabled only in generated previews. |
| **Preferences: Context Pack Settings** | Opens defaults and user overrides side by side. |

Right-click files/folders in the sidebar for **Context Pack: Preview Selected
Files / Folders**, with or without a Git diff. Folder traversal is bounded and
does not follow symlinks or junctions. Open buffers override disk content for
matching paths, including files chosen in the sidebar or picker.

With no project folders open, file discovery uses the parent folders of eligible
source tabs, including the original paths of managed Plist Tools buffers. An
active preview or untitled tab does not hide those roots. Open project folders
keep discovery scoped to those folders. Global Sublime exclusions also apply
to buffers outside project roots; folder-specific project rules stay scoped.

Every operation creates a new **read-only scratch Markdown preview**. Review
its contents and skipped-file report, then run **Copy Preview** explicitly.
Generation does not change the clipboard. Previews are snapshots; regenerate
after editing. There is no default key binding.

Explicit current-file/selection commands can capture a scratch text view when
you deliberately choose it. Scratch views are never included automatically in
open-buffer collections or used as filesystem-buffer overrides.

## Bundle format

- File headings use repository-relative paths, or project-relative paths outside
  Git. Standalone files use their parent folder as the root. A root folder name
  is shown to help distinguish files from different projects.
- Lines are one-based. A selection ending at the next line's first column refers
  to the preceding line. Partial-line selections preserve the exact selected
  text; the line range identifies the source lines rather than adding text.
- Code fences are longer than any backtick run in the captured code. UTF-8
  characters are retained. CRLF/CR become LF in the bundle, and missing final
  newlines are annotated. Empty files use the range `1–0 (empty)`.
- On-disk files must be UTF-8 (an optional UTF-8 BOM is accepted). For other
  encodings, open the file in Sublime first so its decoded buffer can be used.
  NUL and unexpected ASCII control characters are rejected.
- PlistTools XML working buffers use the original plist path and XML fence.
- No token count is claimed. Limits measure UTF-8 bytes, with space reserved for
  preview metadata and skipped reasons. Code is never silently truncated: an
  oversized item or diff is skipped whole.

Paths and code may contain private information. The defaults reduce accidental
inclusion of common secrets; they do not inspect or redact secrets in otherwise
eligible files. Review before sharing. Avoid projects with indistinguishable
root folder names if you need unambiguous cross-project labels.

## Exclusions and limits

Default limits are **50 items**, **256 KiB per file/diff**, **1 MiB per bundle**,
and **3,000 discovered file/directory entries**. Open-buffer capture also has
a total byte budget equal to the bundle limit. Entries beyond a limit are
reported, and the skipped report itself is bounded with an explicit omitted
entry count. Narrow your selection or adjust settings for larger bundles.
Directory discovery is bounded even for empty directory trees and very large
individual directories. A picker uses at most the discovery limit.

Default exclusions include `.env` and `.env.*` (except `.env.example`,
`.env.sample`, `.env.template`), `*.env`, `.envrc`, `.npmrc`, `.pypirc`,
`.netrc`, `.git-credentials`, `.htpasswd`, common private-key/certificate
extensions, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`, `*.ppk`,
`*.kdbx`, `*.jks`, `*.keystore`, `*.tfstate*`, `*.tfvars`, `credentials*`,
`secrets.*`, `.ssh`, `.aws`, `.kube`, `.gnupg`, `.git`, dependency folders,
virtual environments, caches and common build folders. Sensitive hidden
directories are checked in ancestors too, so opening a nested project beneath
them does not bypass these defaults.
Context Pack also honors Sublime's file/folder/binary exclusion patterns and
folder-specific project exclusions. Patterns match path components when they
have no slash, or paths relative to the relevant root when they contain a slash.
Matching is case-insensitive for consistent behavior across operating systems.

Git ignore rules apply even to explicitly selected files when Git is available;
tracked files keep Git's normal behavior and are not ignored merely because a
pattern now matches them. If a `.git` directory/worktree marker exists but Git
is missing, repository resolution fails, or its ignore check fails, that
repository's files are skipped and the preview explains why. A genuine
non-repository folder works without Git. Set `respect_gitignore` to `false`
only when you deliberately want to disable this protection. Git internals are
always excluded. Skipped-file labels are relative to their collection roots.
Symlinks/junctions and paths traversing them are always skipped, including
links pointing inside the project. Files outside a project can be captured when
explicitly opened or selected, using their own repository/parent as the root.
This check is deliberately conservative: a path whose real path differs is
also skipped. That can reject Windows 8.3 aliases and macOS `/tmp` or `/var`
aliases. Open the project through its resolved long/canonical path to capture
those files; the plugin does not silently traverse aliases.

| Setting | Default | Purpose |
| --- | --- | --- |
| `max_file_bytes` | `262144` | Entire file, selected text total, or diff limit. |
| `max_pack_bytes` | `1048576` | Entire preview byte limit; minimum 4096. |
| `max_files` | `50` | Maximum captured file/buffer items. |
| `max_scan_files` | `3000` | Maximum filesystem entries examined, or Git paths offered. |
| `git_timeout` | `5` | Seconds per Git subprocess. |
| `respect_gitignore` | `true` | Apply Git's ignore rules when available. |
| `include_git_diff` | `false` | Add diffs to ordinary preview commands. Explicit “with Git Diff” commands always request them. |
| `allow_env_examples` | `true` | Permit the three example/template `.env` names above. |
| `extra_exclude_globs` | `[]` | Adds project-specific exclusions to the built-in defaults. |
| `exclude_globs` | See defaults | Legacy/custom additions are also additive. User settings cannot remove built-in default protections. |

Numeric settings are clamped to practical upper bounds: 16 MiB per file,
32 MiB per bundle, 1,000 items, 50,000 discovery entries, and 60 seconds per
Git process. Git output is read with a byte limit; exceptionally large Git
listings fail with an explanation instead of creating an unbounded picker.

Example user settings:

```json
{
    "max_files": 20,
    "max_file_bytes": 131072,
    "include_git_diff": true,
    "extra_exclude_globs": ["private-notes/*", "internal-config.json"]
}
```

### Git diff behavior

Diffs compare saved working-tree contents to `HEAD`, so staged and unstaged
changes are represented together. Before the first commit, they show staged
changes. They are generated only for accepted eligible paths, with external
diff and text-conversion commands disabled. Untracked files have file blocks
but no Git diff. Deleted tracked files have a missing-file annotation and
their deletion diff. Unsaved edits appear in buffer blocks; Git cannot see
those edits. Files rejected as sensitive, ignored, binary, oversized, or excluded
do not receive a diff. Git's filesystem monitor is disabled for every command,
and diff explicitly disables external diff/text-conversion commands. Git may
still run repository-configured clean filters while comparing working-tree
files. Use Git features only with repositories whose configuration you trust;
the plugin does not sandbox Git filter programs.

Repository discovery follows `.git` directory/worktree markers;
unusual repositories configured only through Git environment variables are
not discovered automatically.

## Development

Run from the repository root with Python 3.8 or later:

```sh
python -m unittest discover -s context-pack/tests -v
```

Tests use temporary directories and Git repositories plus small Sublime UI
doubles to check buffer snapshots, selection lines, read-only previews,
explicit copy, exclusion rules, size limits, literal Git pathspecs, and thread
boundaries. Tests live below `tests/` so Sublime does not load them as plugins.
