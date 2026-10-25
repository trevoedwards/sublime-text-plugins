"""Conflict-checked atomic writes and durable working-copy metadata."""
import hashlib
import json
import os
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


def atomic_write(path, data, expected):
    """Compare source twice, then replace from a flushed same-directory temp file."""
    path = os.path.abspath(path)
    before = fingerprint(path)
    if before != expected:
        raise ConflictError("The file changed in another application. Revert or Save As to a new path; it was not overwritten.")
    if before and not before["mode"] & stat.S_IWUSR:
        raise PermissionError("The destination is read-only.")
    fd, temporary = tempfile.mkstemp(prefix=".plist-tools-", dir=os.path.dirname(path))
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
            import ctypes
            from ctypes import wintypes
            replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
            replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
            replace.restype = wintypes.BOOL
            if not replace(path, temporary, None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(temporary, path)
        return fingerprint(path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_metadata(path, data):
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=True).encode("utf-8"), fingerprint(path))


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
