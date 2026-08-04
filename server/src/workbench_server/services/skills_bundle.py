"""Locate the bundled skills plugin that ships inside the package.

Workbench's own skills are one *local Claude Code plugin* (`skills_bundle/`,
shipped as package data), handed to each session through
``ClaudeAgentOptions.plugins`` — which the SDK turns into a ``--plugin-dir``
argument on the CLI it spawns. Two consequences worth stating:

* **Session-scoped.** The plugin lives for the lifetime of that CLI subprocess.
  Nothing is copied into ``~/.claude``, no global config is edited, and
  uninstalling Workbench takes the skills with it.
* **Namespaced.** Plugin skills are addressed as ``workbench:<name>``, so a user
  skill called ``remember`` and ours cannot shadow one another.

A missing bundle is degraded, never fatal: sessions still work, they just have
no ``workbench:*`` skills.
"""

from importlib.resources import files
from pathlib import Path

import structlog

log = structlog.get_logger()

#: Plugin name in ``.claude-plugin/plugin.json``; also the ``<name>:`` prefix
#: every bundled skill is addressed by.
PLUGIN_NAME = "workbench"

_BUNDLE_PACKAGE = "workbench_server"
_BUNDLE_DIR = "skills_bundle"
_MANIFEST = (".claude-plugin", "plugin.json")


def bundled_plugin_path() -> Path | None:
    """Absolute path of the bundled plugin directory, or ``None`` if unusable.

    ``None`` is returned (with a warning) when the directory is absent, when its
    ``.claude-plugin/plugin.json`` manifest is missing — the CLI rejects a plugin
    directory without one — or when the package is installed in a form that has
    no real filesystem path, such as a zipimport. Callers skip the plugin and
    carry on.
    """
    resource = files(_BUNDLE_PACKAGE) / _BUNDLE_DIR
    if not isinstance(resource, Path):  # pragma: no cover - only under zipimport
        log.warning("skills_bundle.not_on_disk", resource=str(resource))
        return None
    if not resource.joinpath(*_MANIFEST).is_file():
        log.warning("skills_bundle.missing", path=str(resource))
        return None
    return resource
