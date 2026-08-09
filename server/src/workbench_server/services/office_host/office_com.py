"""Starting a real Word, Excel or PowerPoint, and being able to prove it is ours.

Everything in this module is **synchronous Win32/COM**, called from the single
apartment thread :mod:`~workbench_server.services.office_host.shell_backend`
owns. COM objects belong to the apartment that created them, so "one thread for
all of it" is not a simplification — it is the rule.

Four measured facts shape it:

* ``DispatchEx("Word.Application")`` takes about a second and yields a
  **private** instance: a pid that did not exist before. Plain ``Dispatch``
  would hand back whatever Word the user already had open, which is the one
  thing hosting must never touch.
* Excel needs one more wall. ``DispatchEx`` gives us its own Excel process, but
  Excel is a DDE server: the next workbook the user double-clicks in Explorer
  (or ``start book.xlsx``) is routed to *some* running instance, and a fresh
  automation one is a candidate — the same isolation the ``excel.exe /x``
  command-line flag exists for. ``Application.IgnoreRemoteRequests = True`` is
  the in-process equivalent and is set the moment the instance is identified
  (:func:`_isolate_instance`), so the user's workbooks open in *their* Excel and
  never land in the one Workbench is hosting. Word has no such server and needs
  no such wall.
* **PowerPoint cannot be given a private process at all, and the honest answer
  is to refuse rather than to share.** Measured 2026-08-09 on Office 16: its COM
  class factory is *multi-use*, so ``DispatchEx("PowerPoint.Application")``
  returns whatever PowerPoint is already running — 0.17 s, no new pid, bound
  straight to a user-started instance. Shelling ``POWERPNT.EXE`` twice merges
  into one process as well (two frames, one pid), and there is no ``/x``: the
  only switch its registration carries is the ``/AUTOMATION`` that COM itself
  passes. ``IgnoreRemoteRequests`` does not exist on its ``Application``
  (``AttributeError``, measured). So every wall Excel gets is unavailable, and
  what remains is the one that actually matters: :func:`launch` asks
  :func:`instance_already_running` **before** it creates any COM object, and
  refuses with :class:`InstanceBusyError` when one is up. That is what keeps the
  user's PowerPoint out of a ``KILL_ON_JOB_CLOSE`` job. With nothing running,
  ``DispatchEx`` does start a fresh process (measured, ~2.4 s) and it is ours on
  the same terms as Word's.
* **The pre-flight is a check, not an interlock, and the residual window is
  stated rather than claimed away.** Three steps run in order and none of them is
  atomic with the next: ``GetActiveObject`` answers "none", the pid and frame
  snapshots are taken, and only then does ``DispatchEx`` run. A PowerPoint the
  user starts *after* the snapshot is not in ``before_pids``, so it would look
  new — and if it were also a registered COM server by the time ``DispatchEx``
  runs, that call would bind to theirs and :func:`_identify` would call it ours.
  Both halves must be true inside the same gap, which is where this stops being
  reachable in practice: the gap is the microseconds between two local calls, and
  a PowerPoint takes seconds to start and register. An instance that began
  *before* the snapshot is in ``before_pids`` and comes back ``adopted`` (refused
  by the service); one that begins inside the gap cannot be a live COM server by
  the end of it, so ``DispatchEx`` starts our own process and *two* new frames
  appear — which :func:`_identify` refuses to choose between rather than guess.
  So: narrow, covered downstream, and not an absolute guarantee. Closing it for
  real needs an interlock COM does not offer.
* **PowerPoint offers no window handle over automation either** — which is worth
  stating because the type library says otherwise. ``Application.HWND`` and
  ``DocumentWindow.HWND`` are both *declared* and both raise "member not found"
  when read, late-bound or early-bound through the generated typelib wrapper
  (measured 2026-08-09). So PowerPoint identifies its frame the way Word does,
  by the snapshot comparison below — which is safer for PowerPoint than for
  Word, because the pre-flight has already established that no other PowerPoint
  exists to be confused with.
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

**"New" is not the same as "ours", and ambiguity is refused rather than
guessed.** A second, genuinely new instance can appear while a launch is in
flight — the user double-clicks a ``.docx`` in Explorer at the wrong second —
and then *two* frames are new, behind *two* pids that were not running before.
Picking either would be a coin toss whose losing side puts the user's own window
in a job object and, on the next failure, terminates it. So :func:`_identify`
never picks from more than one candidate process: Excel is asked which frame it
owns (``Application.Hwnd``), and Word — which has no such property before a
document is open — must produce exactly one new pid across two looks or the
launch fails with nothing contained. The later self-check has the same rule: a
document that lands in a frame belonging to another process proves the contained
pid was never ours, and such a job is released *without* its kill flag
(:class:`ForeignWindowError`) instead of being terminated.

**Killing is never the first answer, and discarding is never an answer at all.**
:func:`close` saves the document, asks the application to quit, and waits. Only
an instance that is gone-or-saved is killed. A document whose save *failed* is
never closed — ``Close`` here means ``wdDoNotSaveChanges``, and the "keep your
changes?" prompt that would normally stand in the way was turned off at launch,
so closing would destroy the user's edit in silence. That instance is
deliberately let go instead: alerts are turned back on, the job's kill flag is
cleared before its handle closes, and the user keeps their unsaved work as an
ordinary window on their desktop. That is the ``close_failed`` path the service
already models, and it re-asks every couple of seconds until the window is gone.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from workbench_server.models.office_bridge import CalculationState
from workbench_server.models.office_host import SINGLE_INSTANCE_KINDS, HostAppKind
from workbench_server.services.office_host.document_window import SlideContent

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
    "powerpoint": "PowerPoint.Application",
}
FRAME_CLASSES: dict[HostAppKind, str] = {
    "word": "OpusApp",
    "excel": "XLMAIN",
    "powerpoint": "PPTFrameClass",
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

#: How often :func:`_identify` looks for the frame window.
_IDENTIFY_POLL_S = 0.05

#: How long it waits *after* the first new frame turns up, before believing it.
#: One frame is not proof of ownership: a document the user opened a moment
#: earlier is a new frame too, and would be the only candidate. A second look
#: sees both and refuses, where the first look would have picked theirs. The
#: cost is one tick on a ~1.6 s launch; the alternative is hosting — and later
#: force-killing — somebody else's window.
_IDENTIFY_SETTLE_S = 0.15

#: How many times a close tries to write the document before giving up on it.
#: The failures this covers are transient (a share that blinked, an antivirus
#: holding the file), and the thing a second try buys is the user's edit.
_SAVE_ATTEMPTS = 2
_SAVE_RETRY_S = 0.5


class OfficeComError(Exception):
    """Any COM or Win32 step that refused. The message is the whole story."""


class DocumentBusyError(OfficeComError):
    """The document is already open in an instance we did not launch."""


class InstanceBusyError(OfficeComError):
    """The *application* is already running, and it only runs one of itself.

    PowerPoint's class factory is multi-use, so the instance a launch would get
    is the user's own (see the module docstring). Refusing is the only answer
    that keeps their session out of our job object — and it is raised *before*
    any COM object is created, so nothing of theirs is touched on the way out.

    Distinct from :class:`DocumentBusyError`: nothing is wrong with the document,
    and closing PowerPoint is the fix rather than closing a file.
    """


class ForeignWindowError(OfficeComError):
    """The document opened in a frame belonging to another process.

    Which proves the window this launch contained was never ours — so the job
    holding it must be released *without* its kill flag. Terminating it would
    end an Office instance the user started, taking their unsaved work with it.
    """


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
    if not WINDOWS:  # pragma: no cover - platform split
        return {}
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


def instance_already_running(kind: HostAppKind) -> bool:
    """Is an instance of this application already up? The launch-time pre-flight.

    **Only meaningful for the single-instance kinds**, and only ever used to
    refuse: for Word and Excel a running instance is irrelevant, because
    ``DispatchEx`` gives us our own process either way.

    ``GetActiveObject`` is the exact question — it asks the Running Object Table
    whether this ProgID has a live registered instance — and it is the *cheap*
    one: measured 2026-08-09, 0.000 s to say "none" and 0.14 s to find a
    user-started PowerPoint. It runs on the apartment thread, where COM is
    initialised, which is why the capabilities report uses the window probe
    (:func:`application_window_exists`) instead: that one needs no apartment and
    so can answer a REST request directly.

    A probe that cannot run answers ``False``. That is the deliberate direction:
    a false negative costs one refused launch further down (the ownership check
    catches an adopted pid regardless), where a false positive would refuse a
    launch that was perfectly safe.
    """
    if not WINDOWS:  # pragma: no cover - platform split
        return False
    prog_id = PROG_IDS.get(kind)
    if prog_id is None:  # pragma: no cover - every kind has a ProgID
        return False
    try:
        win32com.client.GetActiveObject(prog_id)
    except Exception:
        # The documented failure for "nothing registered" (MK_E_UNAVAILABLE), and
        # the honest answer for a COM that would not answer at all.
        return False
    return True


def application_window_exists(kind: HostAppKind) -> bool:
    """Does this application have a frame window open right now?

    The apartment-free half of :func:`instance_already_running`, for the
    capabilities report: pure ``EnumWindows``, no COM, so it answers on whatever
    thread the request arrived on. It misses an instance running *without* a
    window — an ``/AUTOMATION`` PowerPoint some other tool is driving — which is
    why it is only the advisory answer and never the gate. The launch pre-flight
    above is exact, and the pid-snapshot ownership check behind it catches
    anything both of them miss.
    """
    return bool(frame_windows(kind))


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
    somewhere else, :class:`InstanceBusyError` when a single-instance
    application is already running, and :class:`OfficeComError` for everything
    else.
    """
    if not WINDOWS:  # pragma: no cover - platform split
        raise OfficeComError("native Office hosting needs Windows")
    prog_id = PROG_IDS.get(kind)
    if prog_id is None:
        raise OfficeComError(f"{kind} cannot be hosted")
    # Before any COM object exists, for the kinds where creating one would bind
    # to the user's session rather than start our own (see the module docstring).
    # Refusing here is what makes the rest of this module's containment safe: a
    # process we did not start must never reach `_contain`.
    #
    # A check, not an interlock: an instance that appears between this answer and
    # the `DispatchEx` below is not excluded by it. The snapshots are taken as
    # late as possible for that reason, and the two backstops the module
    # docstring sets out — `adopted` for anything that predates the snapshot, and
    # `_identify` refusing to choose between two new processes — are what cover
    # the remainder.
    if kind in SINGLE_INSTANCE_KINDS and instance_already_running(kind):
        raise InstanceBusyError(
            f"{kind} is already running, and it runs one instance for the whole session; "
            f"close it and try again, and Workbench will start its own"
        )
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
    try:
        instance = _identify(app, kind, before_pids, before_frames)
    except OfficeComError:
        # Nothing was contained, so there is no job to release and no pid we are
        # sure of — which is the whole reason this failed. The COM object is the
        # one thing that does name our own instance exactly, so it is what ends
        # it; every window this call declined to claim is left alone.
        _quit_quietly(app, kind)
        raise
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
    except ForeignWindowError:
        # The frame we contained is not the one our own document landed in, so
        # the process in that job belongs to somebody else. Let it go untouched
        # — `kill=False` clears the kill-on-close flag first — and end only our
        # own instance, which we can reach precisely through its COM object.
        log.warning("office_host.released_foreign_job", kind=kind, pid=instance.pid)
        _quit_quietly(app, kind)
        _release_job(instance, kill=False)
        raise
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

    And "new" is never allowed to be ambiguous. Excel is simply asked which
    frame it owns. Word cannot be, so the candidate set is read twice and must
    name exactly one process: two new pids means a second instance appeared
    during the launch, and no amount of looking will say which one this call
    started. Failing there costs a retry; guessing costs the user their window.
    """
    _com_call(setattr, app, "Visible", True)
    # Alerts off before anything is opened: a "file in use" prompt would block
    # the open behind a modal on a window the user cannot see yet.
    _silence_alerts(app, kind)
    # And, for Excel, walled off from the user's other workbooks before the open
    # — the `/x` isolation, done in-process (see the module docstring).
    _isolate_instance(app, kind)
    deadline = time.monotonic() + FRAME_WAIT_S
    seen: dict[int, int] = {}
    while time.monotonic() < deadline:
        owned = _own_frame(app, kind)
        if owned is not None:
            window, pid = owned
            return _instance(app, kind, window, pid, before_pids)
        new = {
            window: pid
            for window, pid in frame_windows(kind).items()
            if window not in before_frames
        }
        if not new:
            time.sleep(_IDENTIFY_POLL_S)
            continue
        if not seen:
            seen = new
            time.sleep(_IDENTIFY_SETTLE_S)
            continue
        # Both looks count: a frame that has come *or gone* since the first one
        # is still a window that was new while this launch was starting.
        candidates = {**seen, **new}
        pids = set(candidates.values())
        if len(pids) > 1:
            raise OfficeComError(
                f"{len(pids)} new {FRAME_CLASSES[kind]} windows from different processes "
                f"appeared while {kind} was starting; refusing to guess which one is ours"
            )
        window, pid = next(iter(candidates.items()))
        return _instance(app, kind, window, pid, before_pids)
    raise OfficeComError(f"{kind} started but never showed a {FRAME_CLASSES[kind]} window")


def _instance(
    app: Any, kind: HostAppKind, window: int, pid: int, before_pids: set[int]
) -> OfficeInstance:
    return OfficeInstance(
        kind=kind,
        pid=pid,
        window_id=window,
        adopted=pid in before_pids,
        app=app,
    )


def _own_frame(app: Any, kind: HostAppKind) -> tuple[int, int] | None:
    """``(hwnd, pid)`` of the frame this very ``Application`` object owns.

    Excel answers ``Application.Hwnd``, which turns identification from "the
    window that is new" into "the window this COM server says is its own" — no
    inference, no race, nothing to disambiguate. Word has no such property
    before a document is open (measured), so it gets ``None`` and falls back to
    the snapshot comparison above. ``None`` also covers "not yet": the frame is
    asked for on every poll tick until it exists.
    """
    if kind != "excel":
        return None
    try:
        window = int(app.Hwnd)
        if not window or win32gui.GetClassName(window) != FRAME_CLASSES[kind]:
            return None
        return window, int(win32process.GetWindowThreadProcessId(window)[1])
    except Exception:
        return None


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
        root = _root_window(int(_com_call(getattr, document.ActiveWindow, "Hwnd")))
    elif instance.kind == "excel":
        document = _com_call(app.Workbooks.Open, str(path), 0, False)
        root = _root_window(int(_com_call(getattr, app, "Hwnd")))
    else:
        # PowerPoint has no AddToRecentFiles parameter and no usable window
        # handle to read back (module docstring), so the frame is confirmed from
        # the window list instead. ``WithWindow=True`` is what makes there be one.
        document = _com_call(
            app.Presentations.Open,
            str(path),
            False,  # ReadOnly
            False,  # Untitled
            True,  # WithWindow
        )
        root = _powerpoint_frame(instance)
    _assert_frame_class(root, instance.kind)
    if root != instance.window_id:
        # The document landed in a frame other than the one this launch made.
        # Refusing is the only safe answer either way — but *which* answer
        # depends on whose process is behind the frame we contained. Our own
        # instance with a second frame is still ours to reap; a different pid
        # means the containment grabbed a stranger, and killing it is precisely
        # the outcome this module exists to prevent.
        if _window_pid(root) == instance.pid:
            raise OfficeComError(
                f"{instance.kind} opened the document in another frame of its own instance"
            )
        raise ForeignWindowError(
            f"{instance.kind} opened the document in a window we did not start: "
            f"the frame contained as pid {instance.pid} belongs to another process"
        )
    if bool(_com_call(getattr, document, "ReadOnly")):
        # Office silently downgraded us because somebody else holds the file.
        # Hosting a read-only copy would be a panel that quietly cannot save.
        raise DocumentBusyError(
            f"{path.name} opened read-only: it is already open in another {instance.kind} instance"
        )
    instance.document = document


def _root_window(window: int) -> int:
    """The top-level ancestor of a window COM handed us."""
    return int(win32gui.GetAncestor(window, GA_ROOT) or window)


def _powerpoint_frame(instance: OfficeInstance) -> int:
    """Which frame the presentation landed in, with no handle to ask COM for.

    ``Application.HWND`` and ``DocumentWindow.HWND`` both refuse to be read
    (module docstring), so this is the substitute — and it is a real check, not a
    shrug. Measured 2026-08-09: ``Presentations.Open`` opens into the frame the
    launch already produced rather than adding one, so the expected answer is the
    frame we contained, still alive and still owned by our pid.

    The fallback covers the one benign way that can be false — PowerPoint
    replacing its start-screen frame with the presentation's — and only when our
    process owns exactly one frame, so it can never widen into a guess. There the
    instance is *re-pointed* at the replacement rather than merely told about it:
    the pid is unchanged, so the containment taken at launch still holds, and the
    window id is what the embed will later hand the shell. Anything else is
    ambiguity, and ambiguity is refused here exactly as it is in
    :func:`_identify`: the launch fails, and the caller reaps the process it
    started rather than reparenting a window it cannot name.

    Every candidate is filtered by pid first, which is what makes the fallback
    safe: one frame on screen is not one frame *of ours*.
    """
    ours = {window for window, pid in frame_windows("powerpoint").items() if pid == instance.pid}
    if instance.window_id in ours:
        return instance.window_id
    if len(ours) == 1:
        replacement = next(iter(ours))
        log.info(
            "office_host.powerpoint_frame_replaced",
            pid=instance.pid,
            was=instance.window_id,
            now=replacement,
        )
        instance.window_id = replacement
        return replacement
    raise OfficeComError(
        f"powerpoint opened the presentation but its process (pid {instance.pid}) owns "
        f"{len(ours)} frame windows; refusing to guess which one holds the document"
    )


def _window_pid(window: int) -> int | None:
    try:
        return int(win32process.GetWindowThreadProcessId(window)[1])
    except Exception:  # pragma: no cover - a window that vanished mid-check
        return None


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


#: ``DisplayAlerts`` off and on, per application. Three applications, three
#: unrelated encodings of the same idea — ``wdAlertsNone``/``wdAlertsAll`` are
#: ``0``/``-1``, Excel takes a plain boolean, and ``ppAlertsNone``/``ppAlertsAll``
#: are ``1``/``2`` (measured 2026-08-09; note that PowerPoint's "off" is the
#: truthy value, so any attempt to share Word's constants here would silently
#: turn alerts *on* for PowerPoint). A table because a conditional expression
#: over three kinds is where that mistake gets made.
_ALERTS: dict[HostAppKind, tuple[Any, Any]] = {
    "word": (0, -1),
    "excel": (False, True),
    "powerpoint": (1, 2),
}


def _silence_alerts(app: Any, kind: HostAppKind) -> None:
    with_value = _ALERTS[kind][0]
    try:
        _com_call(setattr, app, "DisplayAlerts", with_value)
    except Exception as error:  # not fatal: it only changes what a failure looks like
        log.debug("office_host.alerts_not_silenced", kind=kind, detail=str(error))


def _isolate_instance(app: Any, kind: HostAppKind) -> None:
    """Keep the user's other workbooks out of the instance we launched.

    The in-process equivalent of ``excel.exe /x``. Excel answers DDE open
    requests, so a workbook launched from Explorer or the shell can land in *any*
    running instance, ours included — which would put a document the user opened
    into the window Workbench is hosting, exactly the takeover the ownership
    rules exist to prevent. ``IgnoreRemoteRequests = True`` closes that door.

    Excel only: Word runs no such server, and the property does not exist on it.
    **Nor on PowerPoint** — measured, ``AttributeError`` — which is one of the
    reasons PowerPoint is refused outright when an instance is already running
    rather than walled off like Excel: there is no wall to build, so the
    protection has to be "do not share the process at all" (module docstring).
    Non-fatal — a wall that will not go up only widens what a stray open could
    do, it does not fail the launch — and set on the same principle as
    :func:`_silence_alerts`: before ``Documents.Open`` can be raced.
    """
    if kind != "excel":
        return
    try:
        _com_call(setattr, app, "IgnoreRemoteRequests", True)
    except Exception as error:  # not fatal: it only changes what a stray open does
        log.debug("office_host.excel_not_isolated", detail=str(error))


def _restore_alerts(app: Any, kind: HostAppKind) -> None:
    """Hand the application its own prompts back before letting go of it.

    The mirror of :func:`_silence_alerts`, and used on one path only: a window
    that is about to become the user's again must be able to ask them the
    questions we spent the session suppressing — starting with "do you want to
    save your changes?".
    """
    if app is None:
        return
    value = _ALERTS[kind][1]
    try:
        _com_call(setattr, app, "DisplayAlerts", value)
    except Exception as error:  # not fatal: the window is the user's either way
        log.debug("office_host.alerts_not_restored", kind=kind, detail=str(error))


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

    Raises :class:`OfficeComError` when the instance could not be saved, or
    would not close after a save that worked. Both are recorded by the caller as
    ``close_failed``, and in both the process is deliberately released rather
    than killed.
    """
    if not is_running(instance):
        _release_job(instance, kill=False)
        return
    if instance.escaped:
        raise OfficeComError(
            "this instance was released with unsaved changes and is now the user's"
        )

    if not _save(instance):
        # The changes are not on disk, and this is the last moment anyone could
        # keep them. Going on would not merely risk losing them, it *is* the
        # loss: `_quit` closes with wdDoNotSaveChanges, and the "keep your
        # changes?" prompt that normally stands in the way was silenced at
        # launch, so the discard would be both certain and invisible.
        #
        # Nor is asking Word to prompt an option here — that modal blocks the
        # COM call with no timeout, which is the hang this whole module is shaped
        # around. So the instance stops being ours: its own alerts back on, the
        # job's kill flag cleared, its window left on the user's desktop with
        # their edit still in it. The service shows `close_failed` and re-asks
        # every sweep until the window is gone.
        _restore_alerts(instance.app, instance.kind)
        instance.app = None
        instance.document = None
        instance.escaped = True
        _release_job(instance, kill=False)
        log.warning("office_host.released_unsaved", kind=instance.kind, pid=instance.pid)
        raise OfficeComError(
            f"the document could not be saved, so it was not closed; {instance.kind} is "
            "still open with unsaved changes"
        )

    _quit(instance)
    if _wait_for_exit(instance, grace_s):
        _release_job(instance, kill=False)
        return
    # Saved, so nothing is lost by ending a process that ignored its own Quit.
    log.info("office_host.killing_after_quit_ignored", pid=instance.pid)
    _release_job(instance, kill=True)


def _save(instance: OfficeInstance) -> bool:
    """True when the document is on disk — including when it never changed.

    Tried more than once on purpose. The failures measured behind ``Save()`` are
    transient ones — a share that blinked, an antivirus still holding the file —
    and a second attempt costs half a second against an outcome (see
    :func:`close`) that costs the user their edit.
    """
    document = instance.document
    if document is None:
        return True
    for attempt in range(1, _SAVE_ATTEMPTS + 1):
        try:
            if bool(_com_call(getattr, document, "Saved")):
                return True
            _com_call(document.Save)
            return True
        except Exception as error:
            log.warning(
                "office_host.save_failed",
                pid=instance.pid,
                attempt=attempt,
                of=_SAVE_ATTEMPTS,
                detail=str(error),
            )
            if attempt < _SAVE_ATTEMPTS:
                time.sleep(_SAVE_RETRY_S)
    return False


def _quit(instance: OfficeInstance) -> None:
    """Close the document and end the application. **Only ever after a save
    that succeeded** — :func:`_close_document` discards, and :func:`close` is
    the one caller, which does not reach here otherwise."""
    document, app = instance.document, instance.app
    # Drop our references either way: an outstanding COM proxy is one of the
    # reasons an Office process outlives its own Quit.
    instance.document = None
    instance.app = None
    try:
        if document is not None:
            # "Discard" is the safe word here and nowhere else: the bytes are
            # already on disk, and asking Office to decide a second time could
            # re-open the very dialog we are escaping.
            _close_document(document, instance.kind)
    except Exception as error:
        log.debug("office_host.document_close_failed", pid=instance.pid, detail=str(error))
    _quit_quietly(app, instance.kind)


def _close_document(document: Any, kind: HostAppKind) -> None:
    if kind == "word":
        _com_call(document.Close, 0)  # wdDoNotSaveChanges
    elif kind == "excel":
        _com_call(document.Close, False)
    else:
        # ``Presentation.Close`` takes no SaveChanges argument — PowerPoint reads
        # ``Saved`` instead. It is already True here (``close`` only reaches this
        # after a save that worked), so there is nothing to prompt about and
        # nothing to discard.
        _com_call(document.Close)


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


# ---- reading and writing the live document -----------------------------------
#
# The COM half of the document bridge (``real_document_bridge.py``). Every
# function here is synchronous and runs on the one apartment thread that created
# the ``Application`` — COM objects belong to the apartment that made them, so a
# read must run where the launch did. They reach the live in-memory document
# through the ``OfficeInstance`` the launch is holding; none opens a new one, and
# a write is scoped to the one addressed paragraph or cell (the fidelity rule),
# never a ``SaveAs`` or a whole-document rewrite.

#: Trailing markers Word puts on a paragraph's ``Range.Text`` — the paragraph
#: mark itself (``\r``), and the cell/row markers a paragraph inside a table
#: carries. Stripped so a read returns the same clean string the fake mints.
_PARAGRAPH_MARKS = "\r\n\x07\x0b\x0c"

#: ``0x800401FD CO_E_OBJNOTCONNECTED`` as a signed int32. Computed *from* the hex
#: so the decimal can never drift away from the constant it names — the earlier
#: hand-typed literal was ``-2147221021``, which is actually ``0x800401E3``.
CO_E_OBJNOTCONNECTED = int("0x800401FD", 16) - 2**32  # -2147220995

#: HRESULTs that mean the COM object is gone: the instance closed under a call
#: that was in flight. Mapped to ``DocGoneError`` rather than the catch-all, so a
#: read that races a close reports the honest reason.
_DEAD_HRESULTS = frozenset(
    {
        -2147417848,  # RPC_E_DISCONNECTED
        -2147023174,  # 0x800706BA RPC server unavailable
        CO_E_OBJNOTCONNECTED,  # 0x800401FD CO_E_OBJNOTCONNECTED
        -2146959355,  # 0x80080005 CO_E_SERVER_EXEC_FAILURE
    }
)


def is_object_gone(error: Exception) -> bool:
    """Does this COM failure mean the instance closed underneath the call?"""
    return _hresult(error) in _DEAD_HRESULTS


def _cell_text(value: Any) -> str:
    """One Excel cell value as the string a reader sees.

    Integral floats lose their ``.0`` (Excel hands back ``1000.0`` for a cell
    that shows ``1000``); booleans render as Excel does; a blank cell is the
    empty string, never ``None``, so the grid stays rectangular.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _as_matrix(values: Any) -> list[list[Any]]:
    """``Range.Value`` normalised to a 2D list.

    Excel returns a scalar for one cell, a tuple of tuples for a rectangle, and
    ``None`` for a range with nothing in it — one shape to iterate keeps the
    caller from re-deciding per read.
    """
    if values is None:
        return []
    if not isinstance(values, tuple):
        return [[values]]
    if values and isinstance(values[0], tuple):
        return [list(row) for row in values]
    return [list(values)]


def word_paragraph_count(instance: OfficeInstance) -> int:
    """How many paragraphs the live Word body has."""
    return int(_com_call(getattr, instance.document.Paragraphs, "Count"))


def word_paragraph_texts(instance: OfficeInstance) -> list[str]:
    """Every paragraph's text, cleaned of its trailing paragraph mark.

    Read straight from ``Document.Paragraphs(i).Range.Text`` — the live,
    on-screen body including edits the user has not saved — which is exactly what
    hosting a real Word instead of a preview exists to expose.
    """
    document = instance.document
    count = int(_com_call(getattr, document.Paragraphs, "Count"))
    texts: list[str] = []
    for index in range(1, count + 1):
        paragraph = _com_call(document.Paragraphs.Item, index)
        raw = str(_com_call(getattr, paragraph.Range, "Text"))
        texts.append(raw.rstrip(_PARAGRAPH_MARKS))
    return texts


def set_word_paragraph_text(instance: OfficeInstance, paragraph: int, text: str) -> None:
    """Replace the text of one paragraph (zero-based), and nothing else.

    The paragraph's *own* terminal mark is re-supplied so the replace stays
    *within* the paragraph: the document's paragraph count never changes and no
    neighbour is disturbed — the fidelity the write seam owes. Applied to the
    live in-memory instance, so the user can undo it; the file is never rewritten
    here.

    Which mark matters. ``Document.Paragraphs`` includes the paragraphs inside
    table cells, and the *last* paragraph in a cell ends not with ``\\r`` but
    with the end-of-cell marker ``\\x07`` (one of :data:`_PARAGRAPH_MARKS`).
    Forcing ``\\r`` onto such a paragraph rewrites that structural marker and can
    merge or split cells or strand a stray paragraph. So the terminator that is
    already there is read back and re-supplied unchanged; only a paragraph whose
    range carries no recognised terminal mark falls back to a plain ``\\r``.
    """
    document = instance.document
    target = _com_call(document.Paragraphs.Item, paragraph + 1)
    current = str(_com_call(getattr, target.Range, "Text"))
    mark = current[-1] if current and current[-1] in _PARAGRAPH_MARKS else "\r"
    _com_call(setattr, target.Range, "Text", text + mark)


def excel_sheet_names(instance: OfficeInstance) -> list[str]:
    """Every worksheet name, in tab order."""
    worksheets = instance.document.Worksheets
    count = int(_com_call(getattr, worksheets, "Count"))
    return [
        str(_com_call(getattr, _com_call(worksheets.Item, index), "Name"))
        for index in range(1, count + 1)
    ]


def excel_worksheet(instance: OfficeInstance, sheet: str) -> Any:
    """The worksheet named ``sheet``, or ``None`` if the book has no such tab.

    Found by name rather than by ``Worksheets(sheet)`` so a missing sheet is a
    plain ``None`` the caller turns into the shared ``range_invalid`` refusal,
    not a raw COM exception.
    """
    worksheets = instance.document.Worksheets
    count = int(_com_call(getattr, worksheets, "Count"))
    for index in range(1, count + 1):
        worksheet = _com_call(worksheets.Item, index)
        if str(_com_call(getattr, worksheet, "Name")) == sheet:
            return worksheet
    return None


def excel_grid(worksheet: Any) -> dict[tuple[int, int], str]:
    """The worksheet's used range as a sparse ``(row, col) -> text`` map from A1.

    ``UsedRange`` may start below and right of A1; its ``Row``/``Column`` anchor
    the grid so the coordinates match the fake's A1-based ones. Read in one
    ``Value`` call rather than cell by cell, and blank cells are dropped so the
    used-range dimensions come out ``0, 0`` for an empty sheet.
    """
    used = _com_call(getattr, worksheet, "UsedRange")
    base_row = int(_com_call(getattr, used, "Row")) - 1
    base_col = int(_com_call(getattr, used, "Column")) - 1
    grid: dict[tuple[int, int], str] = {}
    for row_offset, row in enumerate(_as_matrix(_com_call(getattr, used, "Value"))):
        for col_offset, value in enumerate(row):
            text = _cell_text(value)
            if text:
                grid[(base_row + row_offset, base_col + col_offset)] = text
    return grid


def powerpoint_slide_count(instance: OfficeInstance) -> int:
    """How many slides the live deck has."""
    return int(_com_call(getattr, instance.document.Slides, "Count"))


def powerpoint_slides(instance: OfficeInstance) -> list[SlideContent]:
    """Every slide's title and shape text, in deck order.

    Read straight from the live ``Presentation`` — including edits the user has
    not saved, which is the whole reason for hosting a real PowerPoint rather
    than rendering a preview.

    A slide has no paragraph stream: its text lives in *shapes*, and a shape only
    has any if it carries a text frame with something in it. Both guards are
    asked before the text is touched, because reading ``TextRange.Text`` off a
    picture or an empty placeholder raises rather than returning "". A shape that
    refuses to be read is skipped rather than failing the whole deck — one
    unreadable SmartArt should cost its own text, not the other fifty slides.
    """
    document = instance.document
    count = int(_com_call(getattr, document.Slides, "Count"))
    slides: list[SlideContent] = []
    for index in range(1, count + 1):
        slide = _com_call(document.Slides.Item, index)
        title, title_id = _slide_title(slide)
        slides.append(SlideContent(title=title, texts=_shape_texts(slide, title_id)))
    return slides


def _slide_title(slide: Any) -> tuple[str | None, int | None]:
    """The slide's title text and the *id* of the shape holding it.

    ``HasTitle`` is asked first: reading ``Shapes.Title`` on a layout without one
    raises, and a layout with an *empty* title is a title of ``""`` — reported as
    None so the reader never shows an empty heading it cannot distinguish from a
    missing one.

    The id travels with it because the title placeholder is also an ordinary
    member of ``Shapes``: without it the title comes back twice, once as the
    block's heading and once as its first line — measured against a real deck.
    Token cost is the reason that matters, and identity is the reason it is an id
    rather than a string comparison: a body line that legitimately repeats the
    title is the user's text and stays.
    """
    try:
        if not bool(_com_call(getattr, slide.Shapes, "HasTitle")):
            return None, None
        title = _com_call(getattr, slide.Shapes, "Title")
        text = str(_com_call(getattr, title.TextFrame.TextRange, "Text")).strip()
        shape_id = int(_com_call(getattr, title, "Id"))
    except Exception:  # a layout that will not answer has no title we can name
        return None, None
    return text or None, shape_id


def _shape_texts(slide: Any, title_id: int | None) -> list[str]:
    texts: list[str] = []
    count = int(_com_call(getattr, slide.Shapes, "Count"))
    for index in range(1, count + 1):
        try:
            shape = _com_call(slide.Shapes.Item, index)
            if title_id is not None and int(_com_call(getattr, shape, "Id")) == title_id:
                continue  # already the block's heading
            if not bool(_com_call(getattr, shape, "HasTextFrame")):
                continue
            frame = _com_call(getattr, shape, "TextFrame")
            if not bool(_com_call(getattr, frame, "HasText")):
                continue
            text = str(_com_call(getattr, frame.TextRange, "Text")).strip()
        except Exception as error:
            # A shape that will not be read (SmartArt, an OLE object, a chart)
            # costs its own text and nothing else.
            log.debug("office_host.shape_unreadable", shape=index, detail=str(error))
            continue
        if text:
            texts.append(text)
    return texts


def naive_local(value: Any) -> Any:
    """Strip the UTC offset COM attaches to a datetime, keeping the wall clock.

    **Drop it, never honour it.** Measured on Windows 11 in ``W. Europe Standard
    Time`` with pywin32 311 (``scripts/dev/probe_live_com.py`` reproduces it):
    a cell holding the naive local ``2024-10-27 02:00`` comes back as
    ``pywintypes.datetime(2024, 10, 27, 2, 0, tzinfo=TimeZoneInfo('GMT Standard
    Time', True))`` — an offset that is **not** this machine's zone and carries
    no information about the value. The digits are already the wall clock the
    user typed, so::

        .replace(tzinfo=None)      -> 2024-10-27 02:00   correct
        .astimezone().replace(...) -> 2024-10-27 03:00   WRONG BY AN HOUR

    and it is wrong on precisely the fall-back date the DST gate exists for.
    ``.astimezone`` is the plausible-looking "normalisation" that silently moves
    every hourly row of an autumn day; this function is here so that mistake has
    one place it cannot be made.

    Applied at the COM boundary, so no tz-aware value ever leaves this module.
    Anything that is not a ``datetime`` is returned untouched — a float stays a
    float, and that is the whole point of the value-typed read path (a tolerance
    band is about precision ``str()`` throws away).
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def excel_range_values(worksheet: Any, a1_range: str) -> list[list[Any]]:
    """One A1 range as a rectangle of **typed** values — never text.

    The value-preserving sibling of :func:`excel_grid`, added rather than folded
    into it because ``office_read`` is a shipped contract whose shape does not
    move: that function runs every cell through :func:`_cell_text`, so ``1234.5``
    arrives as ``"1234.5"``, an integral float loses its ``.0`` and a date
    becomes a string. Reconciling parsed-back strings would throw away exactly
    the precision a tolerance band is about, and would hand the numeric gate a
    date it has to re-parse — which is how a naive local timestamp becomes a
    value the offset refusal has to judge.

    Read in one ``Value`` call (a rectangle, not a cell at a time), normalised
    through :func:`naive_local` so nothing tz-aware crosses this seam, and
    shaped by :func:`_as_matrix` so a single cell and an 8,760-row column come
    back as the same list-of-rows.

    Raises whatever COM raises for a malformed range; the bridge above maps it to
    the shared ``range_invalid`` refusal.
    """
    target = _com_call(worksheet.Range, a1_range)
    return [
        [naive_local(value) for value in row]
        for row in _as_matrix(_com_call(getattr, target, "Value"))
    ]


def excel_used_rows(worksheet: Any) -> int:
    """The last row of the worksheet's used range, 1-based (``0`` when blank).

    Bounds a column read: asking for ``A2:B1048576`` would make Excel hand back
    a million mostly-empty rows, and the used range is the sheet's own answer to
    "where does the data stop".
    """
    used = _com_call(getattr, worksheet, "UsedRange")
    first = int(_com_call(getattr, used, "Row"))
    count = int(_com_call(getattr, _com_call(getattr, used, "Rows"), "Count"))
    return first + count - 1 if count else 0


def workbook_saved(instance: OfficeInstance) -> bool:
    """``Workbook.Saved`` — False when the live instance holds unsaved edits.

    The whole front gate of the reconciliation check turns on this one boolean:
    False means the ``.xlsx`` on disk is **not** the workbook the user is
    looking at, so a gate that read the file would report about numbers that
    have already changed.
    """
    return bool(_com_call(getattr, instance.document, "Saved"))


#: ``Application.CalculationState`` → the state name. Excel's own enum:
#: ``xlDone`` 0, ``xlCalculating`` 1, ``xlPending`` 2.
_CALCULATION_STATES: dict[int, CalculationState] = {0: "done", 1: "calculating", 2: "pending"}


def calculation_state(instance: OfficeInstance) -> CalculationState:
    """Whether Excel has finished calculating, or is still mid-flight.

    An unknown value is reported as ``pending`` rather than ``done``: the
    conservative reading is the only safe default here, since ``done`` is the
    one answer that lets a reconciliation proceed.
    """
    raw = int(
        _com_call(getattr, _com_call(getattr, instance.document, "Application"), "CalculationState")
    )
    return _CALCULATION_STATES.get(raw, "pending")


def set_excel_cell(worksheet: Any, row: int, col: int, value: str) -> None:
    """Set one cell (zero-based ``row``/``col``), and nothing else.

    ``Cells(r, c).Value`` is assigned directly; an empty value clears the cell
    (``None``), mirroring the fake. No other cell is touched and the file is not
    rewritten — the live instance changes, the user can undo it.
    """
    cell = _com_call(worksheet.Cells.Item, row + 1, col + 1)
    _com_call(setattr, cell, "Value", value if value != "" else None)
