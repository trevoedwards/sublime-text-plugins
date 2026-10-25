"""Build self-contained Sublime Text packages using only the standard library."""

import argparse
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent.parent
PACKAGES = {
    "markdown-viewer": "MarkdownViewer",
    "context-pack": "ContextPack",
    "plist-tools": "PlistTools",
}
EXCLUDED_DIRECTORIES = {"tests", "__pycache__", ".git", ".pytest_cache"}


def build_package(directory, name, output_directory):
    source = ROOT / directory
    for required in (".python-version", "Default.sublime-commands", "README.md"):
        if not (source / required).is_file():
            raise ValueError("{} is missing {}".format(directory, required))

    files = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        if path.is_file() and path.suffix not in (".pyc", ".pyo"):
            files[relative.as_posix()] = path
    files.setdefault("LICENSE", ROOT / "LICENSE")

    output_directory.mkdir(parents=True, exist_ok=True)
    destination = output_directory / (name + ".sublime-package")
    with zipfile.ZipFile(str(destination), "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, path in sorted(files.items()):
            entry = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    with zipfile.ZipFile(str(destination)) as archive:
        bad_file = archive.testzip()
        if bad_file:
            raise ValueError("Archive verification failed: " + bad_file)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="*", help="Package directories; defaults to all three")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    packages = args.packages or list(PACKAGES)
    unknown = [name for name in packages if name not in PACKAGES]
    if unknown:
        parser.error("Unknown package(s): {}. Choose from {}".format(
            ", ".join(unknown), ", ".join(PACKAGES)))
    for directory in packages:
        try:
            destination = build_package(directory, PACKAGES[directory], args.output_dir)
        except (OSError, ValueError) as error:
            parser.exit(1, "Build failed: {}\n".format(error))
        print("{} ({} bytes)".format(destination, destination.stat().st_size))


if __name__ == "__main__":
    main()
