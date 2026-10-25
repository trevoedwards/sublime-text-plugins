"""Decode a snapshot of a signed CMS profile without changing its source.

OpenSSL verifies the content signature, but -noverify skips certificate-chain
trust. The macOS security fallback uses the platform decoder's policy. Neither
path establishes signer trust here. Encrypted containers are never decoded.
"""
import base64
import binascii
import ntpath
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

try:
    from .mobileconfig import is_profile
    from .plist_core import PlistError, parse_xml
except ImportError:
    from mobileconfig import is_profile
    from plist_core import PlistError, parse_xml


DEFAULT_MAX_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
_CMS_PREFIX = bytes.fromhex("2a864886f70d0107")


class SignedProfileError(ValueError):
    pass


def _header(data, offset, end, indefinite=False):
    """Read one bounded ASN.1 header, never a general recursive ASN.1 tree."""
    if offset + 2 > end:
        return None
    tag, size = data[offset:offset + 2]
    offset += 2
    if size == 0x80:
        return (tag, offset, None) if indefinite else None
    if size & 0x80:
        count = size & 0x7f
        if count > 4 or offset + count > end:
            return None
        size = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    if offset + size > end:
        return None
    return tag, offset, size


def _unwrap(data, max_bytes=DEFAULT_MAX_BYTES):
    if not isinstance(data, bytes):
        return None, None
    if data.lstrip().startswith(b"-----BEGIN"):
        # Bound even whitespace before allocating the base64-decoded envelope.
        if len(data) > max_bytes:
            return None, None
        match = re.fullmatch(
            rb"\s*-----BEGIN (CMS|PKCS7)-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END \1-----\s*",
            data,
        )
        if not match:
            return None, None
        try:
            decoded = base64.b64decode(re.sub(rb"\s", b"", match.group(2)), validate=True)
        except (ValueError, binascii.Error):
            return None, None
        if len(decoded) > max_bytes:
            return None, None
        return decoded, "PEM"
    return data, "DER"


def _kind(data):
    outer = _header(data, 0, len(data), indefinite=True)
    if not outer or outer[0] != 0x30:
        return None
    _, start, size = outer
    end = len(data) if size is None else start + size
    oid = _header(data, start, end)
    if not oid or oid[0] != 0x06 or oid[2] != len(_CMS_PREFIX) + 1:
        return None
    value = data[oid[1]:oid[1] + oid[2]]
    if value[:-1] != _CMS_PREFIX:
        return None
    if value[-1] == 2:
        return "signed"
    if value[-1] in (3, 6):
        return "encrypted"
    return None


def detect_container(data):
    """Recognize the outer CMS OID only; this does not validate the container.

    DER and outer BER-indefinite ContentInfo are supported. PEM CMS/PKCS7 input
    is bounded to 16 MiB. Malformed and unrelated input returns None.
    """
    decoded, _ = _unwrap(data)
    return _kind(decoded) if decoded is not None else None


def _executable(path):
    return os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _known_folder(folder):
    """Read a Windows folder without relying on the child process environment."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        lookup = ctypes.WinDLL("shell32", use_last_error=True).SHGetFolderPathW
        lookup.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                           wintypes.DWORD, wintypes.LPWSTR]
        lookup.restype = wintypes.LONG
        destination = ctypes.create_unicode_buffer(32768)
        if lookup(None, folder, None, 0, destination) == 0:
            return destination.value or None
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _resolve_decoder(openssl_path=None):
    if openssl_path is not None:
        try:
            configured = os.fspath(openssl_path)
        except TypeError as error:
            raise SignedProfileError("The configured OpenSSL path must be an absolute executable file.") from error
        if not isinstance(configured, str):
            raise SignedProfileError("The configured OpenSSL path must be an absolute executable file.")
        if configured:
            if not _executable(configured):
                raise SignedProfileError("The configured OpenSSL path must be an absolute executable file.")
            return "openssl", configured
    discovered = shutil.which("openssl")
    if discovered and _executable(discovered):
        return "openssl", discovered
    if sys.platform == "win32":
        candidates = []
        git = shutil.which("git")
        if git:
            git_directory = ntpath.dirname(git)
            if ntpath.basename(git_directory).lower() in ("cmd", "bin"):
                git_root = ntpath.dirname(git_directory)
                if ntpath.basename(git_root).lower() in ("mingw32", "mingw64", "usr"):
                    git_root = ntpath.dirname(git_root)
                candidates.append(ntpath.join(git_root, "usr", "bin", "openssl.exe"))
        for candidate in candidates:
            if _executable(candidate):
                return "openssl", candidate
        candidates = []
        for name, folder in (("ProgramFiles", 0x26), ("ProgramFiles(x86)", 0x2a)):
            root = os.environ.get(name) or _known_folder(folder)
            if root:
                candidates.append(os.path.join(root, "Git", "usr", "bin", "openssl.exe"))
        local = os.environ.get("LOCALAPPDATA") or _known_folder(0x1c)
        if local:
            candidates.append(os.path.join(local, "Programs", "Git", "usr", "bin", "openssl.exe"))
        for candidate in candidates:
            if _executable(candidate):
                return "openssl", candidate
    if sys.platform == "darwin" and _executable("/usr/bin/security"):
        return "security", "/usr/bin/security"
    raise SignedProfileError("No CMS decoder found. Install OpenSSL or configure its absolute executable path.")


def _read_bounded(path, limit, description):
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise SignedProfileError("{} exceeds the {}-byte limit.".format(description, limit))
    return data


def _xml_text(data):
    # Respect common XML byte signatures before inspecting an ASCII declaration.
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\x00<\x00?"):
        return data.decode("utf-16-be")
    if data.startswith(b"<\x00?\x00"):
        return data.decode("utf-16-le")
    declaration = re.match(rb"<\?xml\b[^?]{0,512}\bencoding\s*=\s*['\"]([A-Za-z0-9._-]+)['\"]", data)
    encoding = declaration.group(1).decode("ascii") if declaration else "utf-8-sig"
    return data.decode(encoding)


def decode_signed_profile(path, openssl_path=None, timeout=15, max_bytes=DEFAULT_MAX_BYTES):
    """Return strict XML from a signed profile; never decrypt, trust or rewrite.

    Source, decoded output and stderr are read with explicit bounds. Decoder
    files are private temporary snapshots, cleaned after a timeout or failure.
    Output files can grow until the subprocess exits or reaches the timeout;
    bounded reads prevent unbounded memory capture.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise SignedProfileError("max_bytes must be a positive integer.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise SignedProfileError("timeout must be a finite positive number.")
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise SignedProfileError("Signed profile input must be a regular file.")
        data = _read_bounded(path, max_bytes, "Signed profile input")
        decoded, encoding = _unwrap(data, max_bytes)
        kind = _kind(decoded) if decoded is not None else None
        if kind == "encrypted":
            raise SignedProfileError("Encrypted CMS profiles are opaque and cannot be decoded by this command.")
        if kind != "signed":
            raise SignedProfileError("Input is not a recognized signed CMS profile.")
        decoder, executable = _resolve_decoder(openssl_path)
        with tempfile.TemporaryDirectory(prefix="plist-tools-cms-") as directory:
            snapshot = os.path.join(directory, "input.cms")
            output = os.path.join(directory, "output.plist")
            diagnostic = os.path.join(directory, "stderr.txt")
            with open(snapshot, "wb") as handle:
                handle.write(data)
            if decoder == "openssl":
                args = [executable, "cms", "-verify", "-noverify", "-inform", encoding,
                        "-in", snapshot, "-out", output]
            else:
                args = [executable, "cms", "-D", "-i", snapshot, "-o", output]
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                      "shell": False, "timeout": timeout}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            with open(diagnostic, "wb") as stderr:
                result = subprocess.run(args, stderr=stderr, **kwargs)
            details = _read_bounded(diagnostic, MAX_STDERR_BYTES, "Decoder stderr")
            if result.returncode:
                # Cap displayed diagnostics separately from the stderr read bound.
                message = details[:2048].decode("utf-8", errors="replace").strip()
                raise SignedProfileError("CMS decoder failed (exit {}).{}".format(
                    result.returncode, " " + message if message else ""))
            content = _read_bounded(output, max_bytes, "Decoded profile output")
            # XML normalizes literal newlines to LF. Match that behavior in the
            # editor buffer while leaving character references such as &#13;
            # untouched, so their semantic carriage returns are preserved.
            text = _xml_text(content).replace("\r\n", "\n").replace("\r", "\n")
            document = parse_xml(text)
            if not is_profile(document):
                raise SignedProfileError("Decoded content is not an XML configuration profile.")
            return text
    except SignedProfileError:
        raise
    except subprocess.TimeoutExpired as error:
        raise SignedProfileError("CMS decoder exceeded the {}-second timeout.".format(timeout)) from error
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, LookupError, PlistError) as error:
        raise SignedProfileError("Could not decode signed profile: {}".format(error)) from error
