# Sublime Text Plugins

A collection of Sublime Text 4 plugins. Each plugin lives in its own directory
and is installed as a separate package.

## Plugins

| Plugin                                 | Description                                              |
| -------------------------------------- | -------------------------------------------------------- |
| [Markdown Viewer](markdown-viewer/)    | Live GitHub-flavored Markdown preview beside the file (tables, alerts, task lists, table of contents, syntax-highlighted code), plus Open in Browser. |

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

## Requirements

Sublime Text 4. Plugins are written for Python 3.8, so they run on both the
Python 3.8 plugin host (older Sublime Text 4 builds) and the Python 3.14 host
that replaced it in build 4213.
