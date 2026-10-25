# Plist Tools

PLIST editing for Sublime Text 4, using the Python 3.8 plugin API (run on
Python 3.14 from build 4213) and the standard library.
XML files stay XML. Binary files open as typed XML and save back to binary.
There are no third-party Python dependencies, background services, default key
bindings, or JSON conversions of property-list values. Signed profile inspection
uses an optional local CMS decoder.

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
elements, undefined entities, unexpected attributes, dictionary key/value pairing, scalar types,
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

## Apple profiles, Jamf and Intune

Unsigned XML `.mobileconfig` files use the same source-preserving editor as
other plists. Existing profile/payload identifiers, UUIDs, custom application
domains, unknown vendor keys and literal MDM substitution tokens are retained.
Normal editing, formatting and exporting never regenerate profile identities.

| Command Palette command | Action |
| --- | --- |
| Plist Tools: New Apple Configuration Profile | Create an unsaved profile with fresh identity metadata and an empty payload array. |
| Plist Tools: Add Payload Common-Key Scaffold | Enter any Apple or application payload domain; append fresh metadata without changing existing payloads. Configure that domain's settings yourself. |
| Plist Tools: Check Profile Common Keys | Check the profile envelope and immediate payload dictionaries, with jumpable diagnostics. |
| Plist Tools: Check Profile for Jamf | Run common checks and explain Jamf import considerations. |
| Plist Tools: Check Profile for Intune | Run common checks and enforce the 1,000,000-byte UTF-8 XML upload limit. |
| Plist Tools: Search Profile Payloads | Search payload names, types, identifiers and UUIDs, then jump to their source. Private settings are not included in the picker. |
| Plist Tools: Insert New UUID | Explicitly replace each selection or insert at each caret with a new UUID. |
| Plist Tools: Export Unsigned XML Profile Copy… | Validate common keys and write a separate UTF-8 XML `.mobileconfig`; retain existing identities. |
| Plist Tools: Export Unsigned Profile Copy for Intune… | Export a separate XML copy after checking its exact upload size. |
| Plist Tools: Inspect Signed Profile | Decode a signed profile from disk into a read-only scratch view. |
| Plist Tools: Create Editable Unsigned Profile Copy | Copy a decoded inspection into an editable local XML draft. The original signature is not included. |
| Plist Tools: Recover Unsigned Profile Drafts | Reopen local unsigned drafts and their signed-origin destination protection. |

Common-key checks cover required strings, strict integer version `1`, duplicate
identifiers/UUIDs, dictionary/array shape, optional metadata types and macOS
`System`/`User` scope. Empty payload arrays are flagged as scaffolds. UUIDs are
checked for uniqueness without requiring RFC formatting, so existing opaque
identities and tokens are accepted. Common key names complete inside `<key>`
elements, with their expected types and descriptions.

These checks follow Apple's [top-level profile definition](https://github.com/apple/device-management/blob/release/mdm/profiles/TopLevel.yaml)
and [common payload keys](https://github.com/apple/device-management/blob/release/mdm/profiles/CommonPayloadKeys.yaml).
They **do not validate every payload's settings or certify deployment**. PPPC,
system extensions, Wi-Fi, restrictions and custom application preferences can
all be edited; their platform/version support, setting values and MDM assignment
still need verification. A new payload scaffold contains common metadata only.

Intune's [custom Apple profile documentation](https://learn.microsoft.com/en-us/intune/device-configuration/templates/configure-custom-settings-apple)
sets the XML upload limit and describes choosing the deployment channel in
Intune. Profile `PayloadScope` and that portal choice need to agree with the
intended deployment; this package does not configure Intune. Jamf's
[profile upload documentation](https://learn.jamf.com/r/en-US/jamf-pro-documentation-current/Uploading_a_Configuration_Profile_macOS)
describes different handling of signed and unsigned profiles. Verify the
uploaded/deployed result when Jamf imports and edits unfamiliar keys. Exporting
a copy retains identity for updates; creating an independent policy may require
deliberate identity changes. Nothing is uploaded or deployed by this package.

### Signed profile inspection

Open a signed `.mobileconfig`, then run **Inspect Signed Profile**. DER, streamed
BER and CMS/PKCS7 PEM containers are recognized. The original raw view stays
read-only. Decoding runs off the UI thread against a private temporary snapshot;
the decoded XML opens in a separate read-only scratch tab, where profile checks,
payload navigation, key paths and copying work normally.

The decoder uses OpenSSL from `openssl_path`, then PATH, then standard Git for
Windows installation locations. If OpenSSL is unavailable on macOS,
`/usr/bin/security cms` is used. On Windows, for example, a custom setting is:

```json
{ "openssl_path": "C:\\Program Files\\Git\\usr\\bin\\openssl.exe" }
```

The [OpenSSL CMS command](https://docs.openssl.org/3.0/man1/openssl-cms/) verifies
the content signature with `-verify -noverify`; certificate-chain trust is
skipped. A bad signature fails rather than falling back to an unchecked decode.
The macOS `/usr/bin/security cms` fallback applies the same policy: it reads
each signer's status first and rejects anything other than a good signature or
an untrusted signing certificate. **Plist Tools does not
establish signer trust** on either path. Inspecting a profile does not make it
safe or appropriate to deploy.

Use **Create Editable Unsigned Profile Copy** to edit decoded contents. The copy
is attached to a durable private draft in
`Packages/User/PlistTools/ProfileCopies/<uuid>.mobileconfig`. Ordinary Save and
native close/quit saves update that draft; Save As opens the separate guarded
export flow. **Recover Unsigned Profile Drafts** reopens these files. Adjacent
JSON metadata retains the signed origin for destination protection, including
after restarting Sublime. Drafts are retained until you remove them and can
contain profile secrets; treat this directory like your source files.

Save and Save As in a read-only inspection open the export flow. Exports cannot
target the signed original, its aliases, the attached file or a managed binary
source. Use ordinary Save to update an editable draft. The exported copy is not automatically opened.
New export files get your normal file permissions (umask); drafts, recovery
copies and their metadata are created readable only by you.
Existing identifiers are retained. Editing an unsigned copy does not retain or
re-create the original signature. Re-signing and certificate management are not
provided.

Encrypted CMS containers are rejected without attempting decryption. An XML
profile containing `EncryptedPayloadContent` can have its visible envelope
checked, but encrypted payloads remain opaque. Source and decoded-output limits
default to 16 MiB, with a 15-second decoder timeout. Reads and diagnostics are
bounded; temporary decoder output can grow until completion or timeout. These
temporary files can contain profile secrets and are removed after decoding.

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

On macOS, avoid editing files in `~/Library/Preferences` while their app is
running: `cfprefsd` caches preferences and can ignore or overwrite a file that
is replaced on disk. Quit the app first, or use `defaults` for live changes.

Source changes are detected on activation and before every commit. There is no
silent merge or overwrite; use Revert to accept disk changes, or export edits
to another path. Atomic replacement uses a temporary file in the source's own
directory, flushes it, and retains file mode. Windows replacement preserves
destination ACLs and alternate streams. Windows replacement keeps an original-file
backup until replacement succeeds; a partial failure restores the original when
possible and reports the retained backup path if restoration cannot finish.
Fingerprints are checked immediately
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
- Signed `.mobileconfig` profiles support explicit read-only inspection and
  separate unsigned copies using an optional local decoder, as described above.
  Encrypted content cannot be decrypted. Common profile metadata is checked;
  exhaustive payload-specific schema validation and re-signing are not provided.
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
Profile settings include `profile_identifier_prefix`, `profile_organization`,
`profile_scope`, `openssl_path`, `signed_decode_timeout`, and
`max_signed_profile_bytes`. Change `com.example.profile` to your organization's
prefix when creating new profiles; these defaults never change existing files.
The PLIST syntax uses tabs by default; normal Sublime indentation settings are
honored by Format XML.

```sh
python -m unittest discover -s plist-tools/tests -v
```

The tests run without Sublime Text, on Python 3.9 or later.

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
