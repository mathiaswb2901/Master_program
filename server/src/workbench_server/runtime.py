"""Facts about *this* running server that an attaching process needs.

One function, in a module of its own, and the reason is a measured one.
``runtime_token_path`` used to live in :mod:`workbench_server.main` — the module
that builds the whole FastAPI application. ``workbench-cmd`` needed exactly this
one function, so importing it dragged in every router, every service, ``openpyxl``,
``nbformat`` and the agent SDK behind it: measured on Windows 11 with a uv venv,
five runs each, ``import workbench_server.cli.commands_cli`` cost **1.74 s**
median, of which ``workbench_server.main`` alone was **1.66 s**, against a bare
interpreter at 0.06 s. Moving the helper here takes that off *every* CLI
invocation.

So the rule this module exists to keep: **nothing here may import anything the
application layer imports.** ``config`` and ``services/app_data`` are stdlib +
pydantic-settings and nothing else, which is what makes the CLI's import cheap.
``server/tests/test_cli_script.py`` asserts the CLI never re-acquires an edge to
``workbench_server.main``, because a 1.6 s regression is invisible to every other
gate this repo has.
"""

from pathlib import Path

from workbench_server.config import Settings
from workbench_server.services.app_data import app_data_dir


def runtime_token_path(settings: Settings) -> Path:
    """Where this instance drops its per-launch token for an attaching shell.

    Keyed by port so two servers on the same machine (different ports) do not
    clobber or delete each other's file: each writes ``auth-token-<port>`` and
    an attaching shell reads the one matching the port it is dialling. Without
    the discriminator, whichever process exited first would unlink the shared
    file out from under a still-running sibling.
    """
    root = settings.app_data_root or app_data_dir()
    return root / "runtime" / f"auth-token-{settings.port}"
