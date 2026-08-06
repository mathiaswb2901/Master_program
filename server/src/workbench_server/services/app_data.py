"""Where machine-local Workbench state lives — state that is *not* a project.

One function, in its own module, because two features now need the same answer
and neither should have to import the other to get it. ``services/worktrees.py``
established it (the pool root must not be inside the workspace, or the file tree
would list N copies of the repo); ``services/workspaces.py`` needs it for the
recent-workspaces list, which is data about the *user* rather than about any one
project and therefore cannot live in a ``.workbench/`` folder inside one.
"""

import os
import sys
from pathlib import Path

#: Through a constant rather than a bare ``sys.platform`` comparison, so
#: ``warn_unreachable`` does not call the POSIX half dead code on the
#: Windows-only type-check this project runs.
WINDOWS = sys.platform == "win32"


def app_data_dir() -> Path:
    """Machine-local Workbench state that is *not* the user's project.

    ``%LOCALAPPDATA%`` on Windows, which is where a Windows-first app puts
    working data it does not want roamed or backed up. Elsewhere it follows the
    ``~/.workbench`` convention ``services/shortcuts.py`` already established.
    """
    local = os.environ.get("LOCALAPPDATA")
    if WINDOWS and local:
        return Path(local) / "Workbench"
    return Path.home() / ".workbench"
