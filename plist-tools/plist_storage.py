"""Conflict-checked atomic writes and durable working-copy metadata."""
import hashlib
import json
import os
import secrets
import stat
import tempfile


class ConflictError(OSError):
    pass


def fingerprint(path):
    if not os.path.lexists(path):
        return None
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError("PLIST writes require a regular file, not a symbolic link or special file.")
    if info.st_nlink > 1:
        raise OSError("PLIST writes to files with multiple hard links are not supported.")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        current = os.fstat(handle.fileno())
    if (info.st_mtime_ns, info.st_size, info.st_ino) != (current.st_mtime_ns, current.st_size, current.st_ino):
        raise ConflictError("File changed while it was being read.")
    return {"sha256": digest.hexdigest(), "size": current.st_size,
            "mtime_ns": current.st_mtime_ns, "mode": stat.S_IMODE(current.st_mode),
            "inode": current.st_ino}


def _replace_windows(path, temporary):
    """Preserve the original even if ReplaceFileW fails partway through."""
    import ctypes
    from ctypes import wintypes
    replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    replace.restype = wintypes.BOOL
    backup = temporary + ".original"
    # A backup prevents error 1176 from deleting the original. For error 1177,
    # Windows may have moved the original to this name already.
    if not replace(path, temporary, backup, 0, None, None):
        error = ctypes.WinError(ctypes.get_last_error())
        if os.path.exists(backup):
            if os.path.lexists(path):
                raise OSError("Replacement failed; original backup retained at {}. The existing destination was not overwritten.".format(backup)) from error
            try:
                os.replace(backup, path)
            except OSError as restore_error:
                raise OSError("Replacement and restoration failed; the original is retained at {}: {}".format(backup, restore_error)) from error
        raise error
    try:
        os.unlink(backup)
    except OSError:
        # The save succeeded; an inability to remove an old backup must not be
        # reported as a failed source commit.
        pass


def _shared_temporary(directory):
    """Like mkstemp, but the umask (not a fixed 0600) decides the new file's mode."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(100):
        temporary = os.path.join(directory, ".plist-tools-" + secrets.token_hex(8))
        try:
            return os.open(temporary, flags, 0o666), temporary
        except FileExistsError:
            continue
    raise FileExistsError("Could not create a temporary file in {}.".format(directory))


def atomic_write(path, data, expected, private=True):
    """Compare source twice, then replace from a flushed same-directory temp file.

    Existing files keep their mode. New files are owner-only when private
    (recovery copies, drafts, metadata), otherwise they follow the umask.
    """
    path = os.path.abspath(path)
    before = fingerprint(path)
    if before != expected:
        raise ConflictError("The file changed in another application. Revert or Save As to a new path; it was not overwritten.")
    if before and not before["mode"] & stat.S_IWUSR:
        raise PermissionError("The destination is read-only.")
    if private or before:
        fd, temporary = tempfile.mkstemp(prefix=".plist-tools-", dir=os.path.dirname(path))
    else:
        fd, temporary = _shared_temporary(os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if before:
            os.chmod(temporary, before["mode"])
        if fingerprint(path) != expected:
            raise ConflictError("The file changed during save; it was not overwritten.")
        if os.name == "nt" and before:
            # ReplaceFileW preserves the replaced file's ACL and alternate streams.
            _replace_windows(path, temporary)
        else:
            os.replace(temporary, path)
        return fingerprint(path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_metadata(path, data):
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=True).encode("utf-8"), fingerprint(path))


def text_hash(text):
    # Sublime buffers expose logical LF lines even when the file uses CRLF.
    logical_text = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(logical_text.encode("utf-8")).hexdigest()
