# Build the standalone backend and stage it where the Tauri bundle expects it.
#
# Release step, NOT part of the test gate: it needs PyInstaller (the optional
# `packaging` dependency group) and produces a large build artifact. Run it on
# Windows before `tauri build`:
#
#     .\packaging\build_backend.ps1
#     cd desktop; npm run tauri build
#
# It freezes `workbench_server.main:run` into a onedir folder and copies that
# folder to `desktop/src-tauri/backend/`, which `tauri.conf.json`
# (`bundle.resources`) ships and `backend.rs` resolves as
# `<resources>/backend/workbench-server.exe`.

$ErrorActionPreference = "Stop"

# Repo root is this script's parent's parent (packaging/ -> repo).
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$distRoot = Join-Path $repoRoot "packaging\dist"
$buildRoot = Join-Path $repoRoot "packaging\build"
$staged = Join-Path $repoRoot "desktop\src-tauri\backend"

Write-Host "Freezing the backend with PyInstaller..."
# `--group packaging` pulls PyInstaller into the env without adding it to the
# default (or --dev) install, so the normal gate never sees it.
uv run --group packaging pyinstaller `
    "packaging\workbench-server.spec" `
    --noconfirm `
    --distpath $distRoot `
    --workpath $buildRoot
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed with exit code $LASTEXITCODE" }

$onedir = Join-Path $distRoot "workbench-server"
$exe = Join-Path $onedir "workbench-server.exe"
if (-not (Test-Path $exe)) { throw "expected a frozen exe at $exe" }

Write-Host "Staging the onedir into $staged ..."
if (Test-Path $staged) { Remove-Item -Recurse -Force $staged }
New-Item -ItemType Directory -Force -Path $staged | Out-Null
Copy-Item -Recurse -Force (Join-Path $onedir "*") $staged

Write-Host "Done. Bundled backend staged at $staged"
Write-Host "Smoke-test it standalone with: & '$(Join-Path $staged 'workbench-server.exe')'"
