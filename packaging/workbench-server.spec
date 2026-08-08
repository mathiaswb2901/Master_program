# PyInstaller spec — the standalone backend the packaged desktop app ships.
#
# A **onedir** build (a folder with the exe plus its dependencies), not onefile:
# onefile unpacks to a temp dir on every launch, which is slower and trips
# antivirus; onedir is what `tauri.conf.json` bundles under `bundle.resources`
# and what `backend.rs` resolves as `<resources>/backend/workbench-server.exe`.
#
# Run from the repo root through the project venv so `workbench_server` and its
# deps are importable:
#
#     uv run --group packaging pyinstaller packaging/workbench-server.spec --noconfirm
#
# `packaging/build_backend.ps1` wraps that and stages the output into
# `desktop/src-tauri/backend/`.

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# Non-Python package data the server reads at runtime: document templates
# (`blank.docx`/`.xlsx`/…), the bundled skills plugin (`skills_bundle/`) and
# `py.typed`. Frozen apps have no source tree to fall back on, so these must be
# carried explicitly.
datas = collect_data_files("workbench_server")
hiddenimports = collect_submodules("workbench_server")
binaries: list[tuple[str, str]] = []

# uvicorn imports its event-loop and protocol implementations by string at
# runtime (`uvicorn[standard]`: websockets, httptools), so PyInstaller cannot
# see them by following imports — collect the whole package. The agent SDK
# likewise loads pieces dynamically.
for package in ("uvicorn", "claude_agent_sdk"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

analysis = Analysis(
    ["backend_entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="workbench-server",
    debug=False,
    strip=False,
    upx=False,
    # A console app: its stdout/stderr are the pipes `backend.rs` pumps into the
    # shell log. The shell itself spawns it with CREATE_NO_WINDOW, so no console
    # window ever appears to the user.
    console=True,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="workbench-server",
)
