"""The spec entry point — the *only* thing the reconcile subprocess ever runs.

``uv run python -m workbench_server.spec_entry``, with the workspace root as the
working directory. That argv is **fixed and server-owned**
(``services/reconcile_spec.py``); the only variable input is one JSON document on
**stdin**, so nothing a spec file says can reach an argv, a cwd or a shell —
``models/gates.py``'s rule, held one module further out.

What it does, in order:

1. read one JSON request from stdin — a list of ``module:function`` strings;
2. import each module — **only if it is this workspace's own code** — and call
   the function with no arguments;
3. turn the answer into either a scalar or ``(ISO timestamp, value)`` pairs;
4. walk ``sys.modules`` and report every module whose ``__file__`` resolves under
   the working directory;
5. write one compact JSON line to stdout.

Four deliberate properties, each of which is a line of code rather than a
comment:

**The import is jailed to the working directory.** ``module:function`` is
documented everywhere as being *within the workspace*, and until
:func:`outside_workspace` existed nothing held it: ``import_module`` resolves
through ``sys.path``, so a spec naming an installed distribution ran whatever
``site-packages`` last received — under an approval whose digest was recorded
against an in-tree path that never existed and could therefore never move. The
top-level name is checked *before* the import (with ``PathFinder``, which does
not execute the module it finds), and the imported module's own ``__file__`` is
checked after.

**The callable's own output never touches stdout.** ``print`` inside user code is
redirected to stderr for the duration of the call, and the envelope is written to
the stdout handle captured before any of it runs. Without that, one ``print`` in
a reporting function would corrupt the parent's parse, and the failure would look
like a broken spec rather than a chatty one. The parent bounds stderr while the
pipe drains (``services/gates.py::_BoundedCapture``), so a callable that prints a
500 MB dataframe costs a window, not the memory.

**It reports paths, not digests.** Step 4 hands back the *names* of the files
that participated; the parent reads and hashes them itself. A digest computed by
the process being trusted is not evidence about that process.

**It has no server imports.** Nothing here reaches ``workbench_server``'s
services, models, FastAPI or pydantic — it runs in the *workspace's* interpreter,
which may have none of them, and it must start in tens of milliseconds rather
than the ~1.6 s importing the application costs. The envelope is plain
``json.dumps``; the parent validates it into
``models/reconcile_spec.py::SpecEntryResult``, which is where the typing lives.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import importlib.machinery
import json
import sys
import traceback
from pathlib import Path
from typing import Any, TextIO

#: Characters of a failing callable's message kept in the envelope. One line for
#: an evidence detail, not a traceback: the traceback goes to stderr, where the
#: parent's bounded capture already carries it.
MAX_ERROR_CHARS = 400

#: Ceiling on the reported import closure. A workspace that pulled in a thousand
#: of its own modules is reported up to here, which is what keeps an approval's
#: ``covered[]`` a list a person can actually read.
MAX_COVERED_MODULES = 512

#: Fallback when the request names no cap.
DEFAULT_MAX_PAIRS = 100_000


def _clip(text: str) -> str:
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def _describe(exc: BaseException) -> str:
    return _clip(f"{type(exc).__name__}: {exc}")


def _as_float(value: Any) -> float | None:
    """A number, or ``None`` when the value is not one.

    ``bool`` is excluded deliberately: a function returning ``True`` reconciled
    as ``1.0`` is a lie, and it is the same rule ``services/reconciliation.py``
    applies to a workbook cell. ``float()`` rather than an ``isinstance`` chain
    so a numpy scalar or a ``Decimal`` — both of which an analyst's code really
    does return — converts instead of being refused as "not a number".
    """
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_timestamp(value: Any) -> str | None:
    """One pair's timestamp as an ISO string, or ``None`` when it is not one.

    A tz-**aware** value keeps its offset on purpose. The parent hands these to
    ``TimeExpectation.timestamp``, whose contract is naive local wall clock, and
    an offset-bearing one is refused there with a named reason
    (``OffsetNotAllowed``). Stripping it here would hand a fall-back day's two
    02:00s the same lookup key — which is the exact bug that refusal exists for,
    reintroduced one process earlier where no test would be looking.
    """
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _as_pairs(value: Any, max_pairs: int) -> tuple[list[list[Any]], int] | None:
    """``(pairs, total)`` for an iterable of two-element rows, else ``None``.

    ``total`` is the number of rows *seen*, which may exceed ``len(pairs)`` — the
    cut is stated rather than silent (AXI shape 1). Iteration stops one row past
    the cap: a generator that never ends is a hang the parent's timeout would
    have to clean up, and there is no reason to walk it to find that out.
    """
    if isinstance(value, str | bytes | dict):
        return None
    try:
        iterator = iter(value)
    except TypeError:
        return None
    pairs: list[list[Any]] = []
    total = 0
    for row in iterator:
        total += 1
        if total > max_pairs:
            break
        if isinstance(row, str | bytes):
            return None
        try:
            first, second = row
        except (TypeError, ValueError):
            return None
        stamp = _as_timestamp(first)
        number = _as_float(second)
        if stamp is None or number is None:
            return None
        pairs.append([stamp, number])
    return pairs, total


def _under(root: Path, where: str | None) -> bool:
    """Is ``where`` a real path under ``root``?

    ``exists()`` rather than a string comparison: a ``ModuleSpec`` origin is not
    always a path — ``"built-in"`` and ``"frozen"`` would otherwise resolve
    relative to the cwd (which *is* ``root`` here) and read as in-tree, letting
    through exactly the modules this refuses.
    """
    if where is None:
        return False
    try:
        path = Path(where).resolve()
        if not path.exists():
            return False
    except (OSError, ValueError):
        return False
    return path == root or root in path.parents


def outside_workspace(module_name: str, root: Path) -> str | None:
    """Why importing ``module_name`` would not be running *this workspace's*
    code, or ``None``.

    The contract is ``module:function`` **within the workspace**, and it was
    stated in three docstrings without being held anywhere: ``import_module``
    resolves through ``sys.path``, so a spec naming an installed distribution ran
    whatever ``site-packages`` last received — under an approval whose digest was
    recorded against a workspace path that does not exist and therefore can never
    move. The parent refuses such a spec at load time; this is the same refusal
    at the only place that can be sure, because it is the place that imports.

    Asked of the **top-level** name and asked *before* the import, with
    :class:`importlib.machinery.PathFinder` rather than
    :func:`importlib.util.find_spec` — the latter imports parent packages, and
    the whole point is that an installed package that shadows a spec's name never
    gets to run its module body.
    """
    top = module_name.partition(".")[0]
    if top in sys.builtin_module_names:
        return f"{top!r} is a built-in module"
    try:
        found = importlib.machinery.PathFinder.find_spec(top, sys.path)
    except (ImportError, AttributeError, TypeError, ValueError, OSError):
        return None  # let the import itself produce the better message
    if found is None:
        return None  # not importable at all — the ImportError says it plainly
    where = [found.origin] if found.origin is not None else []
    where.extend(found.submodule_search_locations or [])
    outside = [place for place in where if not _under(root, place)]
    if not outside:
        return None
    return f"{top!r} resolves to {outside[0]}"


def call_one(reference: str, max_pairs: int, root: Path) -> dict[str, Any]:
    """Import and call one ``module:function``, and describe what came back.

    Never raises: every failure is one ``ok: false`` entry with a sentence, so a
    spec with one broken callable still reports the other nine. That is the same
    posture ``ReconciliationCheck`` takes towards one unreadable cell.

    ``root`` is the workspace, and the import is **jailed to it** at both ends:
    the name is refused before it is imported when the import system would
    answer it from outside, and the module is refused after it is imported when
    its ``__file__`` did not land under the root after all.
    """
    entry: dict[str, Any] = {"call": reference, "ok": False, "pairs": []}
    module_name, _, function_name = reference.partition(":")
    if not module_name or not function_name:
        entry["error"] = f"not a module:function reference: {reference!r}"
        return entry
    shadowed = outside_workspace(module_name, root)
    if shadowed is not None:
        entry["error"] = _clip(
            f"refused {module_name!r}: {shadowed}, which is not code in {root}. A spec's "
            "callable must live under the workspace root — the approval covers files there "
            "and nowhere else."
        )
        return entry
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # any import error is one evidence line, not a crash
        traceback.print_exc(file=sys.stderr)
        entry["error"] = _clip(f"could not import {module_name!r} — {_describe(exc)}")
        return entry
    origin = getattr(module, "__file__", None)
    if not _under(root, origin if isinstance(origin, str) else None):
        # Belt to the pre-import brace: a top-level name that *did* resolve
        # in-tree can still hand back a submodule from elsewhere (a path entry a
        # `.pth` added, a package that rewrites its own `__path__`). The digest
        # only ever covers files under the root, so anything else is refused.
        entry["error"] = _clip(
            f"refused {module_name!r}: imported from {origin!r}, which is outside {root}"
        )
        return entry
    function = getattr(module, function_name, None)
    if function is None:
        entry["error"] = f"{module_name!r} has no attribute {function_name!r}"
        return entry
    if not callable(function):
        entry["error"] = f"{reference} is not callable ({type(function).__name__})"
        return entry
    try:
        answer = function()
    except Exception as exc:  # a raising callable is evidence, not a crash
        traceback.print_exc(file=sys.stderr)
        entry["error"] = _clip(f"{reference} raised — {_describe(exc)}")
        return entry
    scalar = _as_float(answer)
    if scalar is not None:
        entry.update(ok=True, scalar=scalar)
        return entry
    shaped = _as_pairs(answer, max_pairs)
    if shaped is None:
        entry["error"] = (
            f"{reference} returned {type(answer).__name__}; a check expects a number "
            "or an iterable of (timestamp, value) pairs"
        )
        return entry
    pairs, total = shaped
    entry.update(ok=True, pairs=pairs)
    if total > len(pairs):
        entry["total_pairs"] = total
    return entry


def workspace_modules(root: Path) -> list[str]:
    """Every module in ``sys.modules`` whose file is under ``root``.

    A **fact about what ran**, not a static guess at an import graph that can be
    conditional or built with ``importlib``. This entry module's own package is
    excluded — the harness is not the user's code, and in the one workspace where
    it *is* under the root (this repository) it would otherwise bury the answer.
    """
    harness = Path(__file__).resolve().parent
    found: set[str] = set()
    for module in list(sys.modules.values()):
        raw = getattr(module, "__file__", None)
        if not isinstance(raw, str):
            continue
        try:
            resolved = Path(raw).resolve()
        except OSError:
            continue
        if resolved.parent == harness or harness in resolved.parents:
            continue
        if root != resolved and root not in resolved.parents:
            continue
        found.add(str(resolved))
    return sorted(found)[:MAX_COVERED_MODULES]


def _emit(stream: TextIO, payload: dict[str, Any]) -> None:
    """One compact JSON line, flushed. Compact because this is a machine's line."""
    stream.write(json.dumps(payload, separators=(",", ":"), default=str))
    stream.write("\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    """Read the request, run it, write the envelope. Never raises."""
    del argv  # the argv is fixed and carries nothing; the request is on stdin
    envelope_out: TextIO = sys.stdout
    raw = sys.stdin.read()
    try:
        request = json.loads(raw or "{}")
    except ValueError as exc:
        _emit(envelope_out, {"ok": False, "values": [], "modules": [], "error": _describe(exc)})
        return 2
    if not isinstance(request, dict):
        _emit(
            envelope_out,
            {"ok": False, "values": [], "modules": [], "error": "request must be a JSON object"},
        )
        return 2
    references = [str(item) for item in request.get("callables") or []]
    raw_cap = request.get("max_pairs") or DEFAULT_MAX_PAIRS
    max_pairs = int(raw_cap) if isinstance(raw_cap, int | float) else DEFAULT_MAX_PAIRS
    root = Path.cwd().resolve()

    # Everything the callables print goes to stderr. The envelope is written to
    # the handle captured above, so one stray ``print`` cannot corrupt the parse.
    with contextlib.redirect_stdout(sys.stderr):
        values = [call_one(reference, max_pairs, root) for reference in references]
    _emit(
        envelope_out,
        {"ok": True, "values": values, "modules": workspace_modules(root), "error": None},
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a real subprocess
    raise SystemExit(main(sys.argv[1:]))
