"""Context Pack's editor-independent collection and Markdown generation."""
import fnmatch
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field


DEFAULT_EXCLUDES = [
    ".git", ".hg", ".svn", ".ssh", ".aws", "node_modules", "vendor",
    "build", "dist", "target", ".venv", "venv", "__pycache__", ".next",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.crt", "*.cer", "*.der",
    "id_rsa*", "id_ed25519*", "credentials*", "secrets.*",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".envrc", "*.env",
    ".kube", ".gnupg", "id_ecdsa*", "id_dsa*", "*.ppk", "*.kdbx",
    "*.jks", "*.keystore", "*.tfstate*", "*.tfvars", ".htpasswd",
]


@dataclass
class Options:
    max_file_bytes: int = 256 * 1024
    max_pack_bytes: int = 1024 * 1024
    max_files: int = 50
    max_scan_files: int = 3000
    git_timeout: int = 5
    respect_gitignore: bool = True
    exclude_globs: list = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    allow_env_examples: bool = True

    @classmethod
    def from_dict(cls, data):
        result = cls()
        for key in ("max_file_bytes", "max_pack_bytes", "max_files", "max_scan_files", "git_timeout"):
            value = data.get(key, getattr(result, key))
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(result, key, max(1, min(value, {
                    "max_file_bytes": 16 * 1024 * 1024,
                    "max_pack_bytes": 32 * 1024 * 1024,
                    "max_files": 1000, "max_scan_files": 50000, "git_timeout": 60,
                }[key])))
        # Reserve enough room for a useful report, even with tiny custom limits.
        result.max_pack_bytes = max(4096, result.max_pack_bytes)
        for key in ("respect_gitignore", "allow_env_examples"):
            if isinstance(data.get(key), bool):
                setattr(result, key, data[key])
        # User additions cannot silently remove the default sensitive protections.
        for key in ("exclude_globs", "extra_exclude_globs"):
            if isinstance(data.get(key), list):
                result.exclude_globs.extend(g for g in data[key]
                                            if isinstance(g, str) and g and g not in result.exclude_globs)
        return result


@dataclass
class Piece:
    text: str
    first: int
    last: int


@dataclass
class Request:
    path: str = None
    text: str = None
    label: str = None
    pieces: list = None
    modified: bool = False
    error: str = None


def canonical(path):
    return os.path.normcase(os.path.abspath(path))


def inside(path, root):
    try:
        return os.path.commonpath([canonical(path), canonical(root)]) == canonical(root)
    except ValueError:
        return False


def has_symlink(path):
    """Reject symlinks/junctions in any ancestor, including the selected root."""
    current = os.path.abspath(path)
    while True:
        if os.path.islink(current):
            return True
        # realpath also catches Windows directory junctions on Python 3.8.
        if os.path.normcase(os.path.realpath(current)) != os.path.normcase(current):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def matches(path, patterns):
    path = path.replace("\\", "/").strip("/")
    parts = path.split("/")
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").strip("/")
        if "/" not in pattern:
            if any(fnmatch.fnmatchcase(p.lower(), pattern.lower()) for p in parts):
                return True
        elif fnmatch.fnmatchcase(path.lower(), pattern.lower()):
            return True
        elif fnmatch.fnmatchcase((path + "/").lower(), pattern.lower()):
            return True
    return False


def excluded(path, root, options, native=()):
    relative = os.path.relpath(path, root).replace("\\", "/")
    # Include the root's own name: explicitly opening ~/.ssh as a project must
    # not bypass the same directory exclusion that applies to nested .ssh.
    sensitive_path = os.path.abspath(path).replace("\\", "/")
    # .git internals are never context, even when custom globs replace defaults.
    if ".git" in sensitive_path.lower().split("/"):
        return "Git internal path"
    for part in sensitive_path.split("/"):
        lower = part.lower()
        if lower == ".env" or lower.startswith(".env."):
            if not (options.allow_env_examples and lower in (".env.example", ".env.sample", ".env.template")):
                return "sensitive .env path"
    if matches(relative, options.exclude_globs) or matches(os.path.basename(root), options.exclude_globs):
        return "excluded by Context Pack glob"
    if matches(sensitive_path, [g for g in options.exclude_globs if g in (".ssh", ".aws", ".kube", ".gnupg")]):
        return "excluded by Context Pack glob"
    if matches(relative, native):
        return "excluded by Sublime project/preferences pattern"
    return None


class GitError(Exception):
    pass


def git_run(root, args, options, stdin=None, allowed=(0,), output_limit=None):
    output_limit = output_limit or max(8 * 1024 * 1024, options.max_file_bytes)
    kwargs = dict(cwd=root, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                  shell=False)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        with tempfile.TemporaryFile() as output_file, tempfile.TemporaryFile() as error_file:
            process = subprocess.Popen(["git", "--no-optional-locks", "-c", "core.quotePath=false",
                                        "-c", "core.fsmonitor=false"] + args,
                                       stdout=output_file, stderr=error_file, **kwargs)
            try:
                process.communicate(stdin, timeout=options.git_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise GitError("Git timed out")
            output_file.seek(0)
            output = output_file.read(output_limit + 1)
            error_file.seek(0)
            error = error_file.read(1024)
    except OSError as error:
        raise GitError("Git unavailable: " + str(error))
    if process.returncode not in allowed:
        raise GitError("Git command failed: " + error.decode("utf-8", "replace").strip()[:240])
    if len(output) > output_limit:
        raise GitError("Git output exceeds byte limit; narrow the selection")
    return output


def git_marker(path):
    """Return the nearest .git directory/worktree-file parent without running Git."""
    start = path if os.path.isdir(path) else os.path.dirname(path)
    directory = os.path.abspath(start)
    while True:
        if os.path.lexists(os.path.join(directory, ".git")):
            return directory
        ancestor = os.path.dirname(directory)
        if ancestor == directory:
            return None
        directory = ancestor


def git_root(path, options):
    marker = git_marker(path)
    if marker is None:
        return None
    # A marker plus failing Git is not the same as a non-repository. Callers
    # must fail closed when they cannot verify this repository's ignore rules.
    raw = git_run(marker, ["rev-parse", "--show-toplevel"], options)
    if not raw.rstrip(b"\r\n"):
        raise GitError("Git returned no repository root")
    return os.path.abspath(os.fsdecode(raw.rstrip(b"\r\n")))


def git_paths(root, options, changed=False):
    """Tracked picker; changed mode includes staged/unstaged tracked and untracked files."""
    if changed:
        head = git_run(root, ["rev-parse", "--verify", "HEAD"], options, allowed=(0, 128))
        raw = git_run(root, ["diff", "--name-only", "-z", "HEAD", "--"], options) if head else b""
        # An unborn repository has no HEAD; include its staged paths too.
        raw += git_run(root, ["diff", "--cached", "--name-only", "-z", "--"], options)
        raw += git_run(root, ["ls-files", "-z", "--others", "--exclude-standard", "--"], options)
    else:
        raw = git_run(root, ["ls-files", "-z", "--cached", "--"], options)
    paths = sorted(set(os.fsdecode(p) for p in raw.split(b"\0") if p))
    return [os.path.abspath(os.path.join(root, p)) for p in paths]


def ignored_paths(root, paths, options):
    if not paths or not options.respect_gitignore:
        return set()
    relative = [os.path.relpath(p, root) for p in paths if inside(p, root)]
    raw = git_run(root, ["check-ignore", "-z", "--stdin"], options,
                  b"\0".join(os.fsencode(p) for p in relative) + b"\0", allowed=(0, 1))
    return {canonical(os.path.join(root, os.fsdecode(p))) for p in raw.split(b"\0") if p}


class Collector:
    def __init__(self, roots, options=None, native_patterns=None):
        self.options = options or Options()
        self.roots = [os.path.abspath(r) for r in roots]
        self.native = native_patterns or {}
        self.skipped = []
        self.notes = []
        self._repo_cache = {}
        self._marker_cache = {}
        self._marker_errors = set()

    def root_for(self, path):
        parent = os.path.abspath(path) if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        if parent not in self._repo_cache:
            # Find repository markers cheaply; do not spawn Git for every folder
            # in a non-repository project tree. Worktrees have a .git file.
            directory = git_marker(path)
            repository = None
            if directory is not None:
                if directory not in self._marker_cache:
                    try:
                        self._marker_cache[directory] = git_root(directory, self.options)
                        if self._marker_cache[directory] is None:
                            raise GitError("Repository marker exists but Git could not resolve it")
                    except GitError:
                        self._marker_cache[directory] = directory
                        self._marker_errors.add(canonical(directory))
                repository = self._marker_cache[directory]
            self._repo_cache[parent] = repository
        repository = self._repo_cache[parent]
        if repository:
            return repository
        containing = [r for r in self.roots if inside(path, r)]
        return max(containing, key=len) if containing else parent

    def patterns_for(self, path):
        patterns = []
        for root, values in self.native.items():
            if inside(path, root):
                patterns.extend(values)
        return patterns

    def reason(self, path, root):
        if not inside(path, root):
            return "outside collection root"
        if has_symlink(path):
            return "symlink or junction path"
        reason = excluded(path, root, self.options)
        if reason:
            return reason
        if self.options.respect_gitignore and canonical(root) in self._marker_errors:
            return "Git repository detected but Git failed; ignore rules could not be verified"
        for native_root, patterns in self.native.items():
            if inside(path, native_root) and matches(os.path.relpath(path, native_root), patterns):
                return "excluded by Sublime project/preferences pattern"
        return None

    def label_for(self, path, root=None):
        """Never expose absolute source paths in file headings/skipped labels."""
        root = root or self.root_for(path)
        return os.path.relpath(path, root).replace("\\", "/")

    def expand(self, selected):
        """Bounded traversal; no symlink following. Ignored files are filtered in build."""
        requests = []
        examined = 0
        for selected_path in selected:
            selected_path = os.path.abspath(selected_path)
            root = self.root_for(selected_path)
            reason = self.reason(selected_path, root)
            if reason:
                self.skipped.append((self.label_for(selected_path, root), reason))
                continue
            if not os.path.isdir(selected_path):
                requests.append(Request(path=selected_path))
                examined += 1
            else:
                pending = [selected_path]
                while pending and examined < self.options.max_scan_files:
                    directory = pending.pop()
                    # scandir is lazy, unlike os.walk/listdir, so even one enormous
                    # directory cannot allocate an unbounded discovery list.
                    children = []
                    try:
                        with os.scandir(directory) as entries:
                            for entry in entries:
                                if examined >= self.options.max_scan_files:
                                    break
                                examined += 1
                                reason = self.reason(entry.path, root)
                                if reason:
                                    self.skipped.append((self.label_for(entry.path, root), reason))
                                elif entry.is_dir(follow_symlinks=False):
                                    children.append(entry.path)
                                else:
                                    requests.append(Request(path=entry.path))
                    except OSError:
                        self.skipped.append((self.label_for(directory, root), "cannot read directory"))
                    pending.extend(sorted(children, reverse=True))
            if examined >= self.options.max_scan_files:
                self.notes.append("Discovery stopped at max_scan_files ({} file/directory entries); narrow the selection for more files.".format(examined))
                break
        return requests

    def build(self, requests, buffers=None, include_diff=False):
        buffers = buffers or {}
        accepted = []
        seen = set()
        grouped = {}
        prepared = []
        for request in requests:
            # Display names are not identities: separate untitled buffers may
            # both be named "Notes". Capture already deduplicates cloned views.
            key = canonical(request.path) if request.path else ("untitled", id(request))
            if key in seen:
                continue
            seen.add(key)
            if request.path and key in buffers:
                # Explicit selection pieces take precedence over whole-buffer snapshots.
                if request.pieces is None and request.text is None and request.error is None:
                    request = buffers[key]
            if request.error:
                self.skipped.append((self.label_for(request.path) if request.path else (request.label or "Untitled"), request.error))
                continue
            root = self.root_for(request.path) if request.path else None
            label = os.path.relpath(request.path, root).replace("\\", "/") if root else (request.label or "Untitled")
            reason = self.reason(request.path, root) if request.path else None
            if reason:
                self.skipped.append((label, reason))
                continue
            prepared.append((request, root, label))
            if root and request.path:
                grouped.setdefault(root, []).append(request.path)

        ignored = set()
        for root, paths in grouped.items():
            if self.options.respect_gitignore:
                try:
                    repository = git_root(root, self.options)
                    if repository:
                        ignored.update(ignored_paths(root, paths, self.options))
                    elif root in self._marker_cache:
                        raise GitError("Previously detected Git repository is unavailable")
                except GitError:
                    # Fail closed: do not accidentally include ignored secrets on Git failure.
                    for path in paths:
                        ignored.add(canonical(path))
                    self.notes.append("Git ignore verification failed; files in the affected repository were skipped.")

        sections = []
        bytes_used = 0
        # Keep space for metadata and a bounded skipped report.
        budget = max(0, self.options.max_pack_bytes - 3072)
        for request, root, label in prepared:
            if request.path and canonical(request.path) in ignored:
                self.skipped.append((label, "Git ignored or ignore check failed"))
                continue
            if len(accepted) >= self.options.max_files:
                self.skipped.append((label, "max_files limit"))
                continue
            try:
                missing = False
                if request.text is not None:
                    content = request.text
                    byte_size = sum(len(p.text.encode("utf-8")) for p in request.pieces) if request.pieces is not None else len(content.encode("utf-8"))
                    if byte_size > self.options.max_file_bytes:
                        raise ValueError("max_file_bytes limit (entire file skipped)")
                else:
                    if not os.path.isfile(request.path):
                        if include_diff and not os.path.lexists(request.path) and git_root(root, self.options):
                            missing = True
                            raw = b""
                        else:
                            raise ValueError("not a regular file (possibly deleted)")
                    else:
                        with open(request.path, "rb") as stream:
                            raw = stream.read(self.options.max_file_bytes + 1)
                    if len(raw) > self.options.max_file_bytes:
                        raise ValueError("max_file_bytes limit (entire file skipped)")
                    try:
                        content = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        raise ValueError("not UTF-8 text; open in Sublime to use decoded buffer")
                checked = [p.text for p in request.pieces] if request.pieces is not None else [content]
                if any(any(ord(c) < 32 and c not in "\t\r\n" for c in text) for text in checked):
                    raise ValueError("binary/control-character content")
                pieces = request.pieces if request.pieces is not None else [Piece(content, 1, line_count(content))]
                section = ("\n## " + safe_label(label) + "\n\nMissing on disk; no file contents captured.\n") if missing else render_file(label, root, pieces, request.modified, request.path is None)
                if bytes_used + len(section.encode("utf-8")) > budget:
                    raise ValueError("max_pack_bytes limit (entire item skipped)")
                sections.append(section)
                bytes_used += len(section.encode("utf-8"))
                accepted.append((request, root, label))
            except (OSError, ValueError, GitError) as error:
                self.skipped.append((label, error.strerror if isinstance(error, OSError) else str(error)))

        if include_diff:
            for request, root, label in accepted:
                if not root or not request.path:
                    continue
                try:
                    if not git_root(root, self.options):
                        continue
                    diff = git_diff(root, request.path, self.options)
                    if not diff:
                        continue
                    section = "\n## Git diff: " + safe_label(label) + "\n\n" + code_block(diff, "diff")
                    if bytes_used + len(section.encode("utf-8")) > budget:
                        self.skipped.append((label + " (diff)", "max_pack_bytes limit (entire diff skipped)"))
                        continue
                    sections.append(section)
                    bytes_used += len(section.encode("utf-8"))
                except GitError as error:
                    self.skipped.append((label + " (diff)", str(error)))

        report = "# Context Pack\n\nGenerated snapshot; review before copying. Copy with **Context Pack: Copy Preview**.\n\n"
        report += "{} item(s). UTF-8 bytes: content is bounded; no token estimate. Line endings normalized to LF.\n".format(len(accepted))
        if include_diff:
            report += "Git diff uses saved working-tree and index contents relative to HEAD (or staged content before the first commit); unsaved edits appear only in file blocks. Untracked files have no diff.\n"
        for note in self.notes[:5]:
            report += "\nNote: " + safe_label(note[:180]) + "\n"
        if len(self.notes) > 5:
            report += "\n{} further discovery notes omitted.\n".format(len(self.notes) - 5)
        report += "".join(sections)
        if self.skipped:
            skipped_header = "\n## Skipped\n\n"
            report_size = len((report + skipped_header).encode("utf-8"))
            skipped_lines = []
            # Bounded report: disclose omitted entries rather than truncating source code.
            displayed = 0
            for label, reason in self.skipped:
                line = "- {}: {}\n".format(safe_label(str(label)[:180]), safe_label(str(reason)[:200]))
                line_size = len(line.encode("utf-8"))
                if report_size + line_size + 160 > self.options.max_pack_bytes:
                    break
                skipped_lines.append(line)
                report_size += line_size
                displayed += 1
            report += skipped_header + "".join(skipped_lines)
            if displayed < len(self.skipped):
                report += "\n{} more skipped entries omitted from this bounded report.\n".format(len(self.skipped) - displayed)
        return report


def line_count(text):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.count("\n") + (1 if normalized and not normalized.endswith("\n") else 0)


def safe_label(value):
    # Paths and errors are plain escaped Markdown, never executable HTML/headings.
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return re.sub(r"([\\`*_{}\[\]()#+.!<>|~-])", r"\\\1", value)


def code_block(text, language=""):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    longest = max([len(m.group(0)) for m in re.finditer(r"`+", text)] or [0])
    fence = "`" * max(3, longest + 1)
    block = fence + language + "\n" + text
    if text and not text.endswith("\n"):
        block += "\n"
    block += fence + "\n"
    if text and not text.endswith("\n"):
        block += "\n_Source has no final newline._\n"
    return block


def render_file(label, root, pieces, modified=False, untitled=False):
    flags = []
    if modified:
        flags.append("unsaved buffer")
    if untitled:
        flags.append("untitled buffer")
    section = "\n## " + safe_label(label) + "\n\n"
    if root:
        section += "Root: " + safe_label(os.path.basename(root) or root) + "\n\n"
    if flags:
        section += ", ".join(flags) + "\n\n"
    extension = os.path.splitext(label)[1].lstrip(".").lower()
    language = {"py": "python", "js": "javascript", "ts": "typescript", "sh": "bash", "md": "markdown", "plist": "xml"}.get(extension, extension)
    if not re.fullmatch(r"[a-z0-9_+-]{1,20}", language):
        language = ""
    for piece in pieces:
        section += "Lines {}–{}{}\n\n".format(piece.first, piece.last, " (empty)" if not piece.text else "")
        section += code_block(piece.text, language)
        section += "\n"
    return section


def git_diff(root, path, options):
    relative = os.path.relpath(path, root).replace("\\", "/")
    # Disable repository-configured external diff/textconv and literalize pathspecs.
    args = ["--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "HEAD", "--", relative]
    head = git_run(root, ["rev-parse", "--verify", "HEAD"], options, allowed=(0, 128))
    if head:
        raw = git_run(root, args, options, output_limit=options.max_file_bytes)
    else:
        raw = git_run(root, ["--literal-pathspecs", "diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color", "--", relative], options, output_limit=options.max_file_bytes)
    if len(raw) > options.max_file_bytes:
        raise GitError("diff exceeds max_file_bytes (entire diff skipped)")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise GitError("diff is not UTF-8 text")
