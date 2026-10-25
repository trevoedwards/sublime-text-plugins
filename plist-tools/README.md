# Plist Tools

PLIST editing for Sublime Text 4, using Python 3.8 and the standard library.
XML files stay XML. Binary files open as typed XML and save back to binary.
There are no dependencies, background services, default key bindings, or JSON
conversions of property-list values.

## Installation

Link or copy this directory into Sublime's `Packages` folder as **PlistTools**.
Find that folder with **Preferences → Browse Packages…**.

```sh
ln -s "$PWD/plist-tools" ~/.config/sublime-text/Packages/PlistTools
```

On Windows, from an elevated Command Prompt:

```bat
mklink /D "%APPDATA%\Sublime Text\Packages\PlistTools" C:\path\to\sublime-text-plugins\plist-tools
```

macOS uses `~/Library/Application Support/Sublime Text/Packages`.
The package directory must be named `PlistTools`.

## Editing

Open a `.plist`, `.entitlements`, or unsigned, unencrypted XML `.mobileconfig`
file. Binary `bplist00` files are detected by their header, regardless of their
extension. The PLIST syntax highlights keys, value types, comments and unknown
elements. Snippets provide `plist`, `dict`, `array`, `string`, `integer`, `real`,
`bool`, `date`, and `data`; type a trigger and press Tab.

Validation checks the actual XML source, including duplicate keys, unknown
elements, unexpected attributes, dictionary key/value pairing, scalar types,
dates, integer ranges and base64. The first error appears inline and in the
gutter/status bar. **Validate and Jump to Error** moves the caret to it. XML
files use normal Sublime saves, including the ability to save unfinished XML;
binary commits and format conversions require a valid document.

Dictionary order, booleans, integer/real types, byte data and UTC dates survive
round trips. There is no automatic formatting on save. **Format XML** changes
structural whitespace while retaining dictionary order, comments, scalar
spelling, entity spelling and significant string whitespace. Binary files
cannot store comments; comments added to their XML working copy remain in that
working copy and are not included in binary output.

| Command Palette command | Action |
| --- | --- |
| Plist Tools: Validate and Jump to Error | Validate and jump to the first diagnostic. |
| Plist Tools: Format XML (Preserve Comments and Order) | Format the current XML buffer in one undoable edit. |
| Plist Tools: Search Outline / Go to Key Path | Search paths and types, then jump to a value. |
| Plist Tools: Copy Current Key Path | Copy an unambiguous path such as `$["Devices"][0]["Name"]`. |
| Plist Tools: Toggle Boolean | Toggle the boolean under the caret. |
| Plist Tools: Convert Source to Binary | Validate and immediately save the source as binary. |
| Plist Tools: Convert Source to XML | Validate and immediately save the source as UTF-8 XML. |
| Plist Tools: Export PLIST Copy As… | Choose XML/binary and a destination for a separate copy. |
| Plist Tools: Recover Pending Edits | Open a saved working copy whose source commit is pending. |
| Preferences: Plist Tools Settings | Open package settings. |

## Binary saves and recovery

Sublime does not expose a cancellable pre-save event or a supported API to mark
an arbitrary custom save as clean. Plist Tools therefore edits binary sources
through a private XML working file in
`Packages/User/PlistTools/Recovery/<source-hash>.plist`. Its adjacent `.json`
stores the original path, output format and source fingerprint. A separate
`.pending` snapshot holds durable text before a source commit, so native Save
alone writes the attached `.plist` and does not trigger external-change dialogs.
The tab is
named `filename.plist [binary]`; the status bar shows the original full path.
Sublime's underlying file path, native file/path commands and external tools
will see the **working file**. Tools can read the original path from the view
setting `plist_tools_source_path`.

The original binary file is never attached to an XML buffer. Normal **Save**
and **Save All** validate, write a durable recovery copy, check for external
changes, and atomically replace the original in its existing format. Undo and
redo work on XML text. Binary opening loads a **fresh XML buffer** and closes
the untouched raw view, preserving its tab group and position. This keeps raw
hexadecimal content and file-retarget operations out of the XML undo history.
Initial XML-to-binary conversion similarly opens a fresh managed view before
closing the original XML view and committing binary output. Close extra views
of the original file before that initial conversion; managed XML views may be
cloned normally. **File → Revert File** reloads the original source and
asks before discarding edits. Opening the original again focuses its existing
working view. Native close/quit saves also commit through a pre-save fallback.
Session restores reattach source metadata from the recovery directory, even
when per-view settings were not restored.

For a managed working view, native **Save As** deliberately opens **Export
PLIST Copy As**. This writes a separate copy and **keeps the current source
association**: the next ordinary Save still updates the original source. The
export destination is not automatically opened. Use the explicit conversion
commands to change the current source's format; those commands save immediately.
After conversion to XML, the managed working view remains attached to recovery
until closed; reopening the original XML uses a normal text buffer.

Recovered work is visibly labeled **pending** until committed or reverted.
Invalid XML, missing directories, read-only destinations and source changes
stop a commit. Normal failed Save leaves the buffer editable and dirty, and
retains its XML recovery copy. A native close/quit save can close the view after
its failure callback; the pending copy remains available through **Recover
Pending Edits**. A crash before an ordinary save relies on Sublime's normal hot
exit/session recovery, as other text buffers do. Do not delete the recovery
directory while pending edits are needed. Working copies may contain sensitive
configuration values, so treat this directory like the source files. It is
kept outside Sublime's disposable cache and is never automatically deleted.

Source changes are detected on activation and before every commit. There is no
silent merge or overwrite; use Revert to accept disk changes, or export edits
to another path. Atomic replacement uses a temporary file in the source's own
directory, flushes it, and retains file mode. Windows replacement preserves
destination ACLs and alternate streams. Fingerprints are checked immediately
before replacement; as with other portable file writers, an uncooperative
writer can still race in the final check-to-replace interval. Symbolic links,
special files and files with multiple hard links are rejected for managed
writes. These restrictions avoid silently replacing a link or changing which
file it points to.

## Supported formats and limits

- Supported: PLIST 1.0 XML and ordinary `bplist00` binary property lists with
  string keys, strings, dictionaries, arrays, booleans, integers in
  `-2^63 … 2^64-1`, real numbers (including explicit `inf`, `-inf`, `nan`), byte
  data and whole-second UTC dates. Binary output preserves values and order,
  not the original object-table layout or exact byte encoding.
- **Legacy OpenStep/ASCII property lists are unsupported.** They are reported
  as invalid PLIST XML and are not silently converted.
- **Signed or encrypted `.mobileconfig` profiles are unsupported.** Decode or
  verify them with appropriate platform tooling first. This plugin does not
  remove signatures, decrypt profiles, or re-sign output. Unsigned XML profiles
  are ordinary property lists; profile-specific schema validation is not provided.
- **Binary archive UID values, null values, cycles, shared container references,
  fractional-second binary dates, strings containing carriage returns, and strings containing XML-forbidden characters
  are unsupported for editing.** Opening them retains the raw file read-only
  with an explanation. Their information is never flattened into strings or
  stripped. XML has no safe equivalent for these structures or precision.
  Carriage returns are rejected because `plistlib` normalizes them when creating
  XML. Every conversion also checks ordered typed values and exact floating-point
  bits. If serialization would change a value, such as merging positive and
  negative zero within one binary file, the conversion fails before writing it.
- Custom DTDs, internal entity declarations and processing instructions are
  rejected. Apple's ordinary external PLIST doctype is accepted without fetching
  it. Parsing is limited to 128 levels and 100,000 XML elements.
- Live validation stops above `max_analysis_bytes` (2 MiB by default); binary
  opening/reverting stops above `max_binary_bytes` (8 MiB by default). Increase
  these settings for trusted large files. Delayed live parsing runs off the UI
  thread and stale results are ignored. Explicit format, validate and save
  commands run synchronously.
- Disable other binary-PLIST conversion packages for the same files. Packages
  that deliberately retarget or rewrite saves can bypass each other's behavior.
  If Plist Tools is disabled, native saves update only the XML working copy;
  re-enable it and use Recover Pending Edits to commit the source.

## Settings and tests

`PlistTools.sublime-settings` provides `live_validation`, `validation_delay`
(400 ms), `inline_diagnostics`, `max_analysis_bytes` and `max_binary_bytes`.
The PLIST syntax uses tabs by default; normal Sublime indentation settings are
honored by Format XML.

```sh
python -m unittest discover -s plist-tools/tests -v
```

The tests cover strict XML parsing, types, source offsets/key paths, formatting,
unsupported binary values, atomic writes, conflicts and save lifecycle routing.
Runtime behavior is based on Sublime's documented
[save events and view APIs](https://www.sublimetext.com/docs/api_reference.html)
and Python's [plistlib types and binary support](https://docs.python.org/3.8/library/plistlib.html).
The existing [BinaryPlist package](https://packagecontrol.io/packages/BinaryPlist)
was reviewed for comparison; this package uses a separate implementation and
keeps originals detached to avoid native XML saves over binary files.

## License

[MIT](../LICENSE) © Trevor Edwards
