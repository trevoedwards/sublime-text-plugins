# Sublime Text Plugins

A collection of Sublime Text 4 plugins. Each plugin lives in its own directory
and is installed as a separate package.

## Plugins

| Plugin | Description |
| --- | --- |
| [Markdown Viewer](markdown-viewer/) | Live GitHub-flavored Markdown preview beside the file, plus Open in Browser. |
| [Context Pack](context-pack/) | Preview and copy selected code, open buffers, project files, and Git changes as a Markdown context bundle. |
| [Plist Tools](plist-tools/) | Edit XML/binary plists and Apple profiles, check Jamf/Intune common keys, and inspect signed profiles. |

## Installing a plugin

Sublime Text loads packages from its `Packages` folder. To find it, use
**Preferences → Browse Packages…**. Install a plugin by linking (or copying)
its directory into that folder under the package name given in the plugin's
README.

For example, on Linux:

```sh
git clone https://github.com/trevoedwards/sublime-text-plugins.git
cd sublime-text-plugins
ln -s "$PWD/markdown-viewer" ~/.config/sublime-text/Packages/MarkdownViewer
```

With a link, `git pull` updates the installed plugin and Sublime reloads it
automatically. See each plugin's README for its commands, settings and other
platforms.

The package directory names are `MarkdownViewer`, `ContextPack`, and
`PlistTools`, respectively. Each package is independent and has no third-party
Python dependencies. See its README for supported commands and limitations.

## Building installable packages

From the repository root, run:

```sh
python tools/build_packages.py
```

This creates the three `.sublime-package` archives in `dist/`, including the
license and excluding tests and Python caches. To build just the new plugins:

```sh
python tools/build_packages.py context-pack plist-tools
```

Copy the desired archives into Sublime's `Installed Packages` directory,
beside its `Packages` directory. Use either an archive or a linked source
directory for each plugin; keeping both installed can hide which copy is loaded.

## Development checks

The new plugins include standard-library test suites that run outside Sublime:

```sh
python -m unittest discover -s context-pack/tests -v
python -m unittest discover -s plist-tools/tests -v
```

## Requirements

Sublime Text 4. Plugins are written for Python 3.8, so they run on both the
Python 3.8 plugin host (older Sublime Text 4 builds) and the Python 3.14 host
that replaced it in build 4213.

## License

[MIT](LICENSE) © Trevor Edwards
