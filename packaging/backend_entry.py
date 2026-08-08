"""PyInstaller entry point for the standalone backend.

PyInstaller freezes a *script*, not a `module:function` console-script, so this
one-line shim calls the same `run()` that `uv run workbench-server` does
(`workbench_server.main:run`). The frozen exe is what a packaged desktop app
ships and spawns (`desktop/src-tauri/src/backend.rs`), so a user needs no Python
and no `uv` on their machine.
"""

from workbench_server.main import run

if __name__ == "__main__":
    run()
