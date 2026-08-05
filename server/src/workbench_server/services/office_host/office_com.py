"""Starting a real Word or Excel, and being able to prove it is ours.

Everything in this module is **synchronous Win32/COM**, called from the single
apartment thread :mod:`~workbench_server.services.office_host.shell_backend`
owns. COM objects belong to the apartment that created them, so "one thread for
all of it" is not a simplification — it is the rule.

Four measured facts shape it:

* ``DispatchEx("Word.Application")`` takes about a second and yields a
  **private** instance: a pid that did not exist before. Plain ``Dispatch``
  would hand back whatever Word the user already had open, which is the one
  thing hosting must never touch.
* The window is reached through ``doc.ActiveWindow.Hwnd`` and then
  ``GetAncestor(GA_ROOT)`` — Word has no ``Application.Hwnd``. Excel does, and
  is asked directly.
* ``Application.Quit()`` does **not** reliably end the process. It lingered as
  an orphan and died only when a Job Object handle closed. So every instance is
  assigned to a job with ``KILL_ON_JOB_CLOSE`` the moment it exists, and the
  job is the reaping guarantee — including for a server that crashes.
* **Office can stop, mid-launch, to ask the user a question** — and the COM call
  waits for the answer with no timeout of its own. "The last time you opened
  'report.docx', it caused a serious error. Do you still want to open it?"
  turned up during this PR's own testing and blocked ``Documents.Open`` for
  three minutes. So the process is identified and contained **before** that call
  is made (:func:`_identify`), which is what lets :func:`abandon` end it from
  another thread when the caller's deadline passes.

**Ownership is a pid comparison, not a heuristic.** The set of running process
ids *and* the set of frame windows are snapshotted before the launch: the frame
we host must be one that did not exist, belonging to a process that did not
exist. One new window can still belong to an old process — the author's Word
owns two frames — which is why both are checked.

**Killing is never the first answer.** :func:`close` saves the document, asks
the application to quit, and waits. Only an instance that is gone-or-saved is
killed; one that will not close *and* could not be saved is deliberately let go
— the job's kill flag is cleared before its handle closes — so the user keeps
their unsaved work as an ordinary window on their desktop. That is the
``close_failed`` path the service already models.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from workbench_server.models.office_host import HostAppKind

log = structlog.get_logger()

WINDOWS = sys.platform == "win32"

if WINDOWS:  # pragma: no cover - import-time platform split
    import pythoncom
    import win32api
    import win32com.client
    import win32event
    import win32gui
    import win32job
    import win32process

#: ``GetAncestor`` root flag. Named here rather than imported so the constant is
#: readable next to its one use.
GA_ROOT = 2

#: Win32 process rights needed to put a process in a job and to end it.
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000

#: The COM ProgID per application, and the frame window class its instance owns.
#: The class is not used to *find* a window (that would be a way to adopt one);
#: it is asserted against the window COM handed us, so a future Office that
#: returns something unexpected fails loudly instead of being reparented.
PROG_IDS: dict[HostAppKind, str] = {
    "word": "Word.Application",
    "excel": "Excel.Application",
}
FRAME_CLASSES: dict[HostAppKind, str] = {
    "word": "OpusApp",
    "excel": "XLMAIN",
}

#: How long a COM call that comes back "server busy" is retried. Office rejects
#: calls while it is showing a menu or painting a first frame; the documented
#: fix is an ``IMessageFilter``, which is a COM object we would have to
#: implement and register — retrying a handful of times costs nothing and
#: covers the same window.
_BUSY_RETRIES = 40
_BUSY_SLEEP_S = 0.25
#: ``RPC_E_CALL_REJECTED`` / ``RPC_E_SERVERCALL_RETRYLATER``.
_BUSY_HRESULTS = frozenset({-2147418111, -2147417846})

#: How long :func:`close` waits for a quit to take effect before deciding the
#: application is not going to.
CLOSE_GRACE_S = 6.0

#: How long the launch waits for the frame window that identifies its process.
#: Measured at ~0.8 s warm; this covers a cold start of the whole Office stack.
FRAME_WAIT_S = 30.0


class OfficeComError(Exception):
    """Any COM or Win32 step that refused. The message is the whole story."""


class DocumentBusyError(OfficeComError):
    """The document is already open in an instance we did not launch."""


@dataclass
class OfficeInstance:
    """One launched application, everything needed to drive and reap it."""

    kind: HostAppKind
    pid: int
    #: Top-level frame window, the handle the shell reparents.
    window_id: int
    #: True when the pid existed before we launched: somebody else's. Never
    #: reparented, never closed — the caller refuses it.
    adopted: bool
    #: The COM ``Application``, and the open document/workbook.
    app: Any = None
    document: Any = None
    #: Job object handle and process handle. The job is the reaping guarantee.
    job: Any = None
    process: Any = None
    #: Set once :func:`close` has decided this instance is no longer ours to
    #: kill (it would not close and could not be saved).
    escaped: bool = False


def _com_call(call: Any, *args: Any, **kwargs: Any) -> Any:
    """One COM call, retried while the server says "busy"."""
    last: Exception | None = None
    for _ in range(_BUSY_RETRIES):
        try:
            return call(*args, **kwargs)
        except Exception as error:  # pywintypes.com_error and friends
            if _hresult(error) not in _BUSY_HRESULTS:
                raise
            last = error
            time.sleep(_BUSY_SLEEP_S)
    raise OfficeComError(f"the Office automation server stayed busy: {last}")


def _hresult(error: Exception) -> int | None:
    args = getattr(error, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def running_pids() -> set[int]:
    """Every process id right now — the ownership snapshot.

    Deliberately *all* of them and not "the Word ones": deciding which processes
    are Word means opening each one and reading its image name, which needs
    rights we would rather not ask for, and a miss there would turn into an
    adoption.
    """
    if not WINDOWS:  # pragma: no cover - platform split
        return set()
    return set(win32process.EnumProcesses())


def running_documents() -> set[str]:
    """Every document open in an Office instance, from the Running Object Table.

    The authoritative answer, and a stale-proof one: the ROT is a live registry
    of objects, so a lock file left behind by a crash is not in it. Monikers are
    only *named*, never bound — binding a file moniker is what would open the
    document, which is the opposite of asking whether it is open.
    """
    if not WINDOWS:  # pragma: no cover - platform split
        return set()
    names: set[str] = set()
    try:
        context = pythoncom.CreateBindCtx(0)
        for moniker in pythoncom.GetRunningObjectTable().EnumRunning():
            with contextlib.suppress(Exception):
                names.add(moniker.GetDisplayName(context, None).lower())
    except Exception as error:  # pragma: no cover - a broken ROT is not fatal
        log.warning("office_host.rot_unreadable", detail=str(error))
    return names


def frame_windows(kind: HostAppKind) -> dict[int, int]:
    """Every frame window of this application right now: ``hwnd -> pid``.

    Used twice, and never to *find* a window to adopt: to tell which frame is
    the one our own launch produced, and to prove the pid behind it is new.
    """
    expected = FRAME_CLASSES[kind]
    found: dict[int, int] = {}

    def visit(window: int, _param: object) -> bool:
        with contextlib.suppress(Exception):
            if win32gui.GetClassName(window) == expected:
                found[window] = int(win32process.GetWindowThreadProcessId(window)[1])
        return True

    with contextlib.suppress(Exception):
        win32gui.EnumWindows(visit, None)
    return found


def document_is_open_elsewhere(path: Path) -> bool:
    """Would opening this document join somebody else's instance?

    **This pre-flight is load-bearing, and the measurement says so.** Asking
    Word to open a document another instance already has open does not fail and
    does not raise: it blocks, indefinitely, behind a "File In Use" prompt that
    ``DisplayAlerts = wdAlertsNone`` does not suppress (measured 2026-08-05 —
    four minutes and still waiting). Answering the question *before* the open,
    from outside Office, is what keeps that from happening at all.

    The Running Object Table is the authority, and the measurement chose it over
    the obvious alternative. Office's ``~$name`` owner file looks like the
    answer and is not: it survives a crash (so a killed instance makes a
    document permanently un-hostable), and it cannot be told from a live one by
    opening it — measured 2026-08-05, a *live* owner file opens read-write just
    as a stale one does. The ROT, in the same measurement, saw a document opened
    by an instance this process did not launch and did **not** see the one
    behind the stale file. Monikers are only *named*, never bound: binding a
    file moniker is what would open the document, which is the opposite of
    asking whether it is open.
    """
    return str(path).lower() in running_documents()


def initialize_apartment() -> None:
    """Join the single-threaded apartment. Once per worker thread."""
    if WINDOWS:  # pragma: no cover - platform split
        pythoncom.CoInitialize()


def uninitialize_apartment() -> None:
    if WINDOWS:  # pragma: no cover - platform split
        pythoncom.CoUninitialize()


def launch(
    path: Path,
    kind: HostAppKind,
    observer: Callable[[OfficeInstance], None] | None = None,
) -> OfficeInstance:
    """Start ``kind`` on ``path`` in a private instance and return it.

    ``observer`` is called the moment the process has been identified and put in
    its job object — before the document is opened, which is the call that can
    block. It is how a caller on another thread gets something it can
    :func:`abandon` when its own deadline passes; nothing else in this module
    needs it.

    Raises :class:`DocumentBusyError` when the document is already open
    somewhere else, and :class:`OfficeComError` for everything else.
    """
    if not WINDOWS:  # pragma: no cover - platform split
        raise OfficeComError("native Office hosting needs Windows")
    prog_id = PROG_IDS.get(kind)
    if prog_id is None:
        raise OfficeComError(f"{kind} cannot be hosted")
    if document_is_open_elsewhere(path):
        raise DocumentBusyError(f"{path.name} is already open in another {kind} window")

    before_pids = running_pids()
    before_frames = frame_windows(kind)
    started = time.monotonic()
    try:
        app = win32com.client.DispatchEx(prog_id)
    except Exception as error:
        raise OfficeComError(f"could not start {kind}: {error}") from error

    # The process, identified and contained *before* the document is opened.
    # That order is the point (see the docstring): `Documents.Open` is the call
    # that can sit behind a modal forever, and by the time it runs we already
    # hold a job object that can end this instance from another thread.
    instance = _identify(app, kind, before_pids, before_frames)
    if not instance.adopted:
        _contain(instance)
    if observer is not None:
        observer(instance)
    if instance.adopted:
        # Somebody else's process. Nothing was contained and nothing is closed:
        # it was never ours. The service refuses the handle.
        return instance
    try:
        _open_document(instance, path)
    except Exception:
        _quit_quietly(app, kind)
        _release_job(instance, kill=True)
        raise
    log.info(
        "office_host.launched",
        kind=kind,
        pid=instance.pid,
        window=instance.window_id,
        seconds=round(time.monotonic() - started, 2),
    )
    return instance


def _identify(
    app: Any, kind: HostAppKind, before_pids: set[int], before_frames: dict[int, int]
) -> OfficeInstance:
    """Find the frame window this launch just created, and contain its process.

    Measured 2026-08-05: a new ``OpusApp`` frame appears about 0.8 s after
    ``DispatchEx`` and **before any document is opened**, and it is the same
    handle ``doc.ActiveWindow.Hwnd`` reports afterwards. That is what makes the
    dangerous call safe to make: the pid is known and jobbed first.

    Ownership is checked twice over, because one new *window* can still belong
    to an old *process* — the user's Word owns two frames on this machine — and
    reparenting one of those would take over their session.
    """
    _com_call(setattr, app, "Visible", True)
    # Alerts off before anything is opened: a "file in use" prompt would block
    # the open behind a modal on a window the user cannot see yet.
    _silence_alerts(app, kind)
    deadline = time.monotonic() + FRAME_WAIT_S
    while time.monotonic() < deadline:
        new = {
            window: pid
            for window, pid in frame_windows(kind).items()
            if window not in before_frames
        }
        if new:
            window, pid = next(iter(new.items()))
            return OfficeInstance(
                kind=kind,
                pid=pid,
                window_id=window,
                adopted=pid in before_pids,
                app=app,
                document=None,
                job=None,
                process=None,
            )
        time.sleep(0.05)
    raise OfficeComError(f"{kind} started but never showed a {FRAME_CLASSES[kind]} window")


def _open_document(instance: OfficeInstance, path: Path) -> None:
    """The risky call, made against an instance that is already contained."""
    app = instance.app
    if instance.kind == "word":
        document = _com_call(
            app.Documents.Open,
            str(path),
            False,  # ConfirmConversions
            False,  # ReadOnly
            False,  # AddToRecentFiles
        )
        window = int(_com_call(getattr, document.ActiveWindow, "Hwnd"))
    else:
        document = _com_call(app.Workbooks.Open, str(path), 0, False)
        window = int(_com_call(getattr, app, "Hwnd"))
    root = int(win32gui.GetAncestor(window, GA_ROOT) or window)
    _assert_frame_class(root, instance.kind)
    if root != instance.window_id:
        # The document landed in a frame other than the one this launch made.
        # Refusing is the only safe answer: that frame may be somebody else's.
        raise OfficeComError(f"{instance.kind} opened the document in a window we did not start")
    if bool(_com_call(getattr, document, "ReadOnly")):
        # Office silently downgraded us because somebody else holds the file.
        # Hosting a read-only copy would be a panel that quietly cannot save.
        raise DocumentBusyError(
            f"{path.name} opened read-only: it is already open in another {instance.kind} instance"
        )
    instance.document = document


def abandon(instance: OfficeInstance) -> None:
    """End an instance from *another* thread, without touching COM.

    The escape from a launch that never came back. The apartment thread is
    inside a blocking call and cannot be asked anything — but the job object was
    taken before that call, and terminating it is pure Win32. Killing the server
    frees the blocked call too, so the thread comes back rather than being lost
    with every host that would have come after it.
    """
    log.warning("office_host.abandoning", kind=instance.kind, pid=instance.pid)
    instance.app = None
    instance.document = None
    _release_job(instance, kill=True)


def _silence_alerts(app: Any, kind: HostAppKind) -> None:
    # wdAlertsNone is 0; Excel's DisplayAlerts is a plain boolean.
    with_value: Any = 0 if kind == "word" else False
    try:
        _com_call(setattr, app, "DisplayAlerts", with_value)
    except Exception as error:  # not fatal: it only changes what a failure looks like
        log.debug("office_host.alerts_not_silenced", kind=kind, detail=str(error))


def _assert_frame_class(window: int, kind: HostAppKind) -> None:
    actual = win32gui.GetClassName(window)
    expected = FRAME_CLASSES[kind]
    if actual != expected:
        raise OfficeComError(
            f"the window {kind} handed us is a {actual!r}, not the {expected!r} frame"
        )


def _contain(instance: OfficeInstance) -> None:
    """Put the process in a job that dies with us.

    Best-effort in the sense that a failure is logged rather than fatal — but it
    is the only thing standing between a crashed server and an orphaned Office,
    so it is loud.
    """
    try:
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        process = win32api.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _SYNCHRONIZE, False, instance.pid
        )
        win32job.AssignProcessToJobObject(job, process)
        instance.job = job
        instance.process = process
    except Exception as error:
        log.warning("office_host.job_object_failed", pid=instance.pid, detail=str(error))


def is_running(instance: OfficeInstance) -> bool:
    """Is the process still there?"""
    if not WINDOWS:  # pragma: no cover - platform split
        return False
    if instance.process is not None:
        # WAIT_TIMEOUT means the process handle is not signalled: still running.
        return bool(win32event.WaitForSingleObject(instance.process, 0) == win32event.WAIT_TIMEOUT)
    # No process handle (the job assignment failed): the window is the only
    # evidence left, and it is the same evidence the watchdog uses.
    return bool(win32gui.IsWindow(instance.window_id))


def window_exists(instance: OfficeInstance) -> bool:
    if not WINDOWS:  # pragma: no cover - platform split
        return False
    return bool(win32gui.IsWindow(instance.window_id))


def close(instance: OfficeInstance, *, grace_s: float = CLOSE_GRACE_S) -> None:
    """Save, quit, and make sure the process is gone. Safe to call twice.

    Raises :class:`OfficeComError` when the instance would not close *and* its
    document could not be saved: the caller records that as ``close_failed``,
    and the process is deliberately released rather than killed.
    """
    if not is_running(instance):
        _release_job(instance, kill=False)
        return
    if instance.escaped:
        raise OfficeComError(
            "this instance was released with unsaved changes and is now the user's"
        )

    saved = _save(instance)
    _quit(instance)
    if _wait_for_exit(instance, grace_s):
        _release_job(instance, kill=False)
        return
    if saved:
        log.info("office_host.killing_after_quit_ignored", pid=instance.pid)
        _release_job(instance, kill=True)
        return
    # Would not close, and its changes are not on disk. Killing here is the one
    # thing that loses the user's work, so the instance stops being ours: the
    # job's kill flag is cleared and the window is theirs.
    instance.escaped = True
    _release_job(instance, kill=False)
    raise OfficeComError(
        "the document would not close and could not be saved; "
        f"{instance.kind} is still open with unsaved changes"
    )


def _save(instance: OfficeInstance) -> bool:
    """True when the document is on disk — including when it never changed."""
    document = instance.document
    if document is None:
        return True
    try:
        if bool(_com_call(getattr, document, "Saved")):
            return True
        _com_call(document.Save)
        return True
    except Exception as error:
        log.warning("office_host.save_failed", pid=instance.pid, detail=str(error))
        return False


def _quit(instance: OfficeInstance) -> None:
    document, app = instance.document, instance.app
    # Drop our references either way: an outstanding COM proxy is one of the
    # reasons an Office process outlives its own Quit.
    instance.document = None
    instance.app = None
    try:
        if document is not None:
            # Never "save on close" here — `_save` already decided that, and a
            # second decision could re-open the very dialog we are escaping.
            _close_document(document, instance.kind)
    except Exception as error:
        log.debug("office_host.document_close_failed", pid=instance.pid, detail=str(error))
    _quit_quietly(app, instance.kind)


def _close_document(document: Any, kind: HostAppKind) -> None:
    if kind == "word":
        _com_call(document.Close, 0)  # wdDoNotSaveChanges
    else:
        _com_call(document.Close, False)


def _quit_quietly(app: Any, kind: HostAppKind) -> None:
    if app is None:
        return
    try:
        if kind == "word":
            _com_call(app.Quit, 0)
        else:
            _com_call(app.Quit)
    except Exception as error:
        log.debug("office_host.quit_failed", kind=kind, detail=str(error))


def _wait_for_exit(instance: OfficeInstance, grace_s: float) -> bool:
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not is_running(instance):
            return True
        time.sleep(0.1)
    return not is_running(instance)


def _release_job(instance: OfficeInstance, *, kill: bool) -> None:
    """Let the job go. ``kill=False`` clears the kill-on-close flag first, so
    closing the handle leaves the process running."""
    job, process = instance.job, instance.process
    instance.job = None
    instance.process = None
    if job is None:
        # No job means the containment failed at launch. A kill still has to
        # happen — an instance nobody can see is worse than a rude one.
        if kill:
            _terminate(instance.pid)
        _close_handle(process)
        return
    try:
        if kill:
            win32job.TerminateJobObject(job, 1)
        else:
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] &= ~(
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    except Exception as error:
        log.warning("office_host.job_release_failed", pid=instance.pid, detail=str(error))
    _close_handle(job)
    _close_handle(process)


def _terminate(pid: int) -> None:
    """Last resort for an instance with no job object behind it."""
    try:
        handle = win32api.OpenProcess(_PROCESS_TERMINATE, False, pid)
    except Exception as error:
        log.warning("office_host.terminate_failed", pid=pid, detail=str(error))
        return
    with contextlib.suppress(Exception):
        win32process.TerminateProcess(handle, 1)
    _close_handle(handle)


def _close_handle(handle: Any) -> None:
    if handle is None:
        return
    # A handle that is already closed is the outcome we wanted anyway.
    with contextlib.suppress(Exception):
        handle.Close()
