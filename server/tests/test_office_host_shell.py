"""The real host backend's two halves, without Office and without a shell.

The COM half (`office_com`) is exercised through the seams that are *not* COM,
and through its **decisions**. Whether `DispatchEx` really starts Word is
verified by running it — a mock there would only assert that the mock was
called. But *which window this launch is allowed to claim*, and *whether a
document may be closed*, are ordinary branching code that happens to sit next to
COM calls, and they are the two places where being wrong costs the user a
window or an edit. Those are tested here, against documents and applications
that answer the way Office does — including the ways it fails.

The shell half is fully testable here: a channel is a socket-shaped object with
two methods, so every branch that matters (no shell, a refusal, a timeout, a
disconnect mid-command, a second window displacing the first) is a unit test.
"""

import asyncio
import contextlib
import json
import os
from pathlib import Path

import pytest

from workbench_server.models.office_host import (
    HostAppKind,
    HostCommand,
    HostCommandAck,
    PanelRect,
)
from workbench_server.services.office_host import office_com
from workbench_server.services.office_host.backend import EmbedRefusedError, HostHandle
from workbench_server.services.office_host.shell_backend import ShellHostBackend
from workbench_server.services.office_host.shell_channel import (
    ShellChannel,
    ShellCommandError,
    ShellUnavailableError,
)

RECT = PanelRect(x=0, y=0, width=800, height=600)
HANDLE = HostHandle(pid=4321, window_id=0xABCD)


class FakeSocket:
    """A shell that answers however the test says.

    ``answer`` decides the ack for each command; ``None`` means "say nothing",
    which is how a wedged webview is tested without waiting for a real timeout.
    """

    def __init__(self, answer: object = True) -> None:
        self.answer = answer
        self.sent: list[HostCommand] = []
        self._acks: asyncio.Queue[str] = asyncio.Queue()
        self._closed = asyncio.Event()

    async def send_text(self, data: str) -> None:
        command = HostCommand.model_validate_json(data)
        self.sent.append(command)
        if self.answer is None:
            return
        ok = self.answer is True
        self._acks.put_nowait(
            HostCommandAck(
                command_id=command.command_id,
                ok=ok,
                code=None if ok else str(self.answer),
                message=None if ok else "the shell said no",
            ).model_dump_json()
        )

    async def receive_text(self) -> str:
        getter = asyncio.create_task(self._acks.get())
        closed = asyncio.create_task(self._closed.wait())
        done, pending = await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if getter in done:
            return getter.result()
        raise ConnectionError("the shell disconnected")

    def disconnect(self) -> None:
        self._closed.set()

    def actions(self) -> list[str]:
        return [command.action for command in self.sent]


async def attached(channel: ShellChannel, socket: FakeSocket) -> asyncio.Task[None]:
    """Serve ``socket`` on ``channel`` and wait until it is really attached."""
    task = asyncio.create_task(_serve(channel, socket))
    for _ in range(200):
        # `attached` alone is not enough for the second socket in a test: it is
        # already true from the first, and returning here would race the
        # displacement this file is asserting.
        if channel._socket is socket:
            return task
        await asyncio.sleep(0)
    raise AssertionError("the channel never attached")


async def _serve(channel: ShellChannel, socket: FakeSocket) -> None:
    # The router does the same: a socket that closed is not an error here.
    with contextlib.suppress(ConnectionError):
        await channel.serve(socket)


# ---- the channel -------------------------------------------------------------


async def test_a_command_reaches_the_shell_and_its_ack_comes_back() -> None:
    channel = ShellChannel()
    socket = FakeSocket()
    task = await attached(channel, socket)

    await channel.call("host-1", "embed", window_id=99, rect=RECT)

    assert socket.actions() == ["embed"]
    sent = socket.sent[0]
    assert (sent.host_id, sent.window_id, sent.rect) == ("host-1", 99, RECT)
    socket.disconnect()
    await task


async def test_nothing_is_sent_when_no_shell_is_attached() -> None:
    channel = ShellChannel()
    with pytest.raises(ShellUnavailableError):
        await channel.call("host-1", "detach")
    with pytest.raises(ShellUnavailableError):
        channel.post("host-1", "set_bounds", rect=RECT)


async def test_a_refusal_carries_the_shells_own_code() -> None:
    channel = ShellChannel()
    socket = FakeSocket(answer="embed_refused")
    task = await attached(channel, socket)

    with pytest.raises(ShellCommandError) as refused:
        await channel.call("host-1", "embed", window_id=1, rect=RECT)
    assert refused.value.code == "embed_refused"
    socket.disconnect()
    await task


async def test_a_shell_that_never_answers_fails_on_the_channels_own_timeout() -> None:
    channel = ShellChannel(timeout_s=0.05)
    socket = FakeSocket(answer=None)  # receives, says nothing
    task = await attached(channel, socket)

    with pytest.raises(ShellCommandError) as timed_out:
        await channel.call("host-1", "detach")
    assert timed_out.value.code is None
    assert "did not answer" in str(timed_out.value)
    socket.disconnect()
    await task


async def test_a_disconnect_fails_every_command_in_flight() -> None:
    """Otherwise the request that asked for it — and the shutdown behind it —
    waits for an ack from a window that has gone."""
    channel = ShellChannel(timeout_s=30)
    socket = FakeSocket(answer=None)
    task = await attached(channel, socket)

    pending = asyncio.create_task(channel.call("host-1", "close"))
    for _ in range(200):
        if socket.sent:
            break
        await asyncio.sleep(0)
    socket.disconnect()
    with pytest.raises(ShellCommandError, match="disconnected"):
        await pending
    await task


async def test_an_unparsable_frame_does_not_take_the_socket_down() -> None:
    channel = ShellChannel()
    socket = FakeSocket()
    task = await attached(channel, socket)

    channel._on_message("{not json")
    channel._on_message(json.dumps({"type": "host_command_ack"}))  # missing fields
    assert channel.attached

    await channel.call("host-1", "detach")
    socket.disconnect()
    await task


async def test_a_second_window_displaces_the_first() -> None:
    """A webview that reloaded before its old socket was reaped. Refusing would
    leave hosting broken until a restart; the newest one wins."""
    channel = ShellChannel()
    first, second = FakeSocket(), FakeSocket()
    first_task = await attached(channel, first)
    second_task = await attached(channel, second)

    await channel.call("host-1", "detach")
    assert second.actions() == ["detach"]
    assert first.actions() == []

    # The displaced socket closing must not un-attach the live one.
    first.disconnect()
    await first_task
    assert channel.attached
    second.disconnect()
    await second_task
    assert not channel.attached


async def test_a_posted_command_does_not_wait_for_an_ack() -> None:
    channel = ShellChannel(timeout_s=30)
    socket = FakeSocket(answer=None)
    task = await attached(channel, socket)

    channel.post("host-1", "set_bounds", rect=RECT)
    for _ in range(200):
        if socket.sent:
            break
        await asyncio.sleep(0)
    assert socket.actions() == ["set_bounds"]
    socket.disconnect()
    await task


# ---- the backend on top of it -------------------------------------------------


async def test_the_window_verbs_become_shell_commands() -> None:
    channel = ShellChannel()
    backend = ShellHostBackend(channel)
    socket = FakeSocket()
    task = await attached(channel, socket)

    await backend.embed(HANDLE, RECT)
    await backend.set_visible(HANDLE, False)
    await backend.detach(HANDLE)

    assert socket.actions() == ["embed", "set_visible", "detach"]
    assert socket.sent[0].window_id == HANDLE.window_id
    assert socket.sent[1].visible is False
    socket.disconnect()
    await task


async def test_hosting_is_unavailable_until_a_shell_is_attached() -> None:
    channel = ShellChannel()
    backend = ShellHostBackend(channel)
    assert backend.ready() is False

    socket = FakeSocket()
    task = await attached(channel, socket)
    assert backend.ready() is True
    socket.disconnect()
    await task
    assert backend.ready() is False


async def test_embedding_without_a_shell_is_a_refusal_not_a_crash() -> None:
    backend = ShellHostBackend(ShellChannel())
    with pytest.raises(EmbedRefusedError, match="not attached"):
        await backend.embed(HANDLE, RECT)


async def test_a_resize_with_no_shell_is_dropped_rather_than_failing_the_host() -> None:
    """A page reload drops the socket while the panels stay docked in the shell
    process. Failing the host there would close a document over a reload."""
    backend = ShellHostBackend(ShellChannel())
    await backend.set_bounds(HANDLE, RECT)  # no raise


async def test_polling_an_instance_this_backend_never_launched_says_gone() -> None:
    backend = ShellHostBackend(ShellChannel())
    assert await backend.poll(HANDLE) == "gone"


async def test_closing_an_instance_this_backend_never_launched_is_a_no_op() -> None:
    """The shell half is still attempted — its panel may exist — but there is no
    process to reap and nothing to raise about."""
    channel = ShellChannel()
    backend = ShellHostBackend(channel)
    socket = FakeSocket()
    task = await attached(channel, socket)
    await backend.close(HANDLE)
    assert socket.actions() == ["close"]
    socket.disconnect()
    await task


async def test_a_shell_that_refuses_the_close_still_lets_the_process_be_reaped() -> None:
    channel = ShellChannel()
    backend = ShellHostBackend(channel)
    socket = FakeSocket(answer="unknown_host")
    task = await attached(channel, socket)
    await backend.close(HANDLE)  # the refusal is suppressed, not raised
    socket.disconnect()
    await task


# ---- the COM half's non-COM seams ---------------------------------------------


def test_a_document_is_open_elsewhere_only_when_the_running_object_table_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement that chose the ROT over the ``~$`` owner file: a stale
    lock file must not make a document permanently un-hostable."""
    document = tmp_path / "report.docx"
    document.write_bytes(b"PK")
    (tmp_path / "~$report.docx").write_bytes(b"stale lock from a crash")

    monkeypatch.setattr(office_com, "running_documents", set)
    assert office_com.document_is_open_elsewhere(document) is False

    monkeypatch.setattr(office_com, "running_documents", lambda: {str(document).lower()})
    assert office_com.document_is_open_elsewhere(document) is True


def test_the_ownership_snapshot_is_every_process_not_a_guess() -> None:
    """`launch` refuses any window whose pid was already running, so a snapshot
    that missed processes would be a snapshot that adopts one."""
    assert os.getpid() in office_com.running_pids()


# ---- the COM half's decisions --------------------------------------------------
#
# Office stands in as objects that answer the way it does, because what is under
# test is never "was Save() called" but "what did this code decide to do about
# the answer" — and the wrong decision here is a destroyed edit or a terminated
# window that was never ours.


class FakeWindow:
    def __init__(self, hwnd: int) -> None:
        self.Hwnd = hwnd


class FakeDocument:
    """A document that can refuse to save, the way a share that blinked does."""

    def __init__(
        self,
        *,
        hwnd: int = 0x99,
        saved: bool = False,
        save_errors: int = 0,
        read_only: bool = False,
    ) -> None:
        self.ActiveWindow = FakeWindow(hwnd)
        self.ReadOnly = read_only
        self.Saved = saved
        self.saves = 0
        self.closed: list[object] = []
        self._save_errors = save_errors

    def Save(self) -> None:
        self.saves += 1
        if self._save_errors:
            self._save_errors -= 1
            raise OSError("the network share went away")
        self.Saved = True

    def Close(self, save_changes: object) -> None:
        self.closed.append(save_changes)


class FakeDocuments:
    def __init__(self, document: FakeDocument) -> None:
        self._document = document
        self.opened: list[str] = []

    def Open(self, path: str, *args: object) -> FakeDocument:
        self.opened.append(path)
        return self._document


class FakeApp:
    """An ``Application``: records what was set on it and whether it was ended."""

    def __init__(self, *, hwnd: int | None = None, document: FakeDocument | None = None) -> None:
        self.alerts: list[object] = []
        self.Visible = False
        self.quits = 0
        self._hwnd = hwnd
        #: What ``IgnoreRemoteRequests`` was last set to, or None if never — the
        #: `/x` isolation Excel gets and Word does not.
        self.ignore_remote_requests: object | None = None
        self.Documents = FakeDocuments(document or FakeDocument())
        self.Workbooks = self.Documents

    @property
    def Hwnd(self) -> int:
        if self._hwnd is None:  # Word has none before a document is open
            raise AttributeError("Application.Hwnd")
        return self._hwnd

    @property
    def DisplayAlerts(self) -> object:
        return self.alerts[-1] if self.alerts else None

    @DisplayAlerts.setter
    def DisplayAlerts(self, value: object) -> None:
        self.alerts.append(value)

    @property
    def IgnoreRemoteRequests(self) -> object:
        return self.ignore_remote_requests

    @IgnoreRemoteRequests.setter
    def IgnoreRemoteRequests(self, value: object) -> None:
        self.ignore_remote_requests = value

    def Quit(self, *args: object) -> None:
        self.quits += 1


class FakeWin32:
    """The two Win32 lookups the decisions rest on: a window's class, and its
    process. Stands in for both ``win32gui`` and ``win32process``."""

    def __init__(self, classes: dict[int, str], pids: dict[int, int]) -> None:
        self._classes = classes
        self._pids = pids

    def GetClassName(self, window: int) -> str:
        return self._classes.get(window, "SomethingElse")

    def GetAncestor(self, window: int, _flag: int) -> int:
        return window

    def GetWindowThreadProcessId(self, window: int) -> tuple[int, int]:
        return (0, self._pids[window])


class FakeCom:
    """Stands in for ``win32com``: ``client.DispatchEx`` is the only path
    reached, and it hands back the application the test wrote."""

    def __init__(self, app: FakeApp) -> None:
        self.app = app
        self.client = self

    def DispatchEx(self, prog_id: str) -> FakeApp:
        return self.app


def an_instance(
    document: FakeDocument | None,
    app: FakeApp,
    *,
    kind: HostAppKind = "word",
    window_id: int = 0xABCD,
) -> office_com.OfficeInstance:
    return office_com.OfficeInstance(
        kind=kind, pid=4321, window_id=window_id, adopted=False, app=app, document=document
    )


def running_while_we_hold_it(instance: office_com.OfficeInstance) -> bool:
    """`_quit` drops the COM references and the process exits behind them, so
    "do we still hold a proxy" is the honest stand-in for "is it running"."""
    return instance.app is not None


# ---- closing: an edit is never discarded on the user's behalf ------------------


def test_a_document_that_will_not_save_is_never_closed_behind_the_users_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this file exists for.

    `Save()` fails — a share that went away, a permission error, a format
    dialog. Closing anyway means ``wdDoNotSaveChanges`` on a document with
    changes in it, and because alerts were silenced at launch the user is not
    even asked. The close must refuse instead, and hand the window back.
    """
    monkeypatch.setattr(office_com, "_SAVE_RETRY_S", 0.0)
    monkeypatch.setattr(office_com, "is_running", lambda instance: True)
    document = FakeDocument(save_errors=99)
    app = FakeApp()
    instance = an_instance(document, app)

    with pytest.raises(office_com.OfficeComError, match="could not be saved"):
        office_com.close(instance)

    assert document.closed == []  # the edit is still in the document
    assert app.quits == 0  # and the application was not ended under it
    assert instance.escaped is True  # it is the user's now, not ours to kill
    assert app.alerts[-1] == -1  # wdAlertsAll: it can ask them to save again

    # And it stays theirs: a later sweep must not have another go at it.
    with pytest.raises(office_com.OfficeComError, match="now the user's"):
        office_com.close(instance)


def test_a_save_that_fails_once_is_retried_rather_than_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured failures are transient. Escaping on the first one would
    leave a Word on the desktop for a blip that a second try walks straight
    through."""
    monkeypatch.setattr(office_com, "_SAVE_RETRY_S", 0.0)
    monkeypatch.setattr(office_com, "is_running", running_while_we_hold_it)
    document = FakeDocument(save_errors=1)
    instance = an_instance(document, FakeApp())

    office_com.close(instance)

    assert document.saves == 2
    assert document.Saved is True
    assert instance.escaped is False


def test_a_saved_document_is_closed_and_its_application_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same branch: once the bytes are on disk, the
    discarding close is the right call and must still happen."""
    monkeypatch.setattr(office_com, "is_running", running_while_we_hold_it)
    document = FakeDocument()
    app = FakeApp()
    instance = an_instance(document, app)

    office_com.close(instance)

    assert document.saves == 1
    assert document.closed == [0]  # wdDoNotSaveChanges, and nothing is lost by it
    assert app.quits == 1
    assert instance.escaped is False


def test_a_document_with_no_changes_is_not_written_before_it_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(office_com, "is_running", running_while_we_hold_it)
    document = FakeDocument(saved=True)
    instance = an_instance(document, FakeApp())

    office_com.close(instance)

    assert document.saves == 0
    assert document.closed == [0]


# ---- identifying: never a coin toss between two new windows -------------------


def test_two_new_windows_from_different_processes_are_refused_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user double-clicked a document while an agent launch was starting.

    Both frames are new and neither pid was running before, so "the new one" no
    longer names a single window. Picking either is a coin toss whose losing
    side puts *their* window in a job object with KILL_ON_JOB_CLOSE.
    """
    monkeypatch.setattr(office_com, "_IDENTIFY_SETTLE_S", 0.0)
    monkeypatch.setattr(office_com, "frame_windows", lambda kind: {0x11: 900, 0x22: 901})

    with pytest.raises(office_com.OfficeComError, match="refusing to guess"):
        office_com._identify(FakeApp(), "word", {1}, {})


def test_a_second_frame_of_our_own_process_is_not_an_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two frames, one pid, is the case the module always handled: it is one
    instance with two windows, and the process behind it is unambiguous."""
    monkeypatch.setattr(office_com, "_IDENTIFY_SETTLE_S", 0.0)
    monkeypatch.setattr(office_com, "frame_windows", lambda kind: {0x11: 900, 0x22: 900})

    instance = office_com._identify(FakeApp(), "word", {1}, {})

    assert instance.pid == 900
    assert instance.adopted is False


def test_the_frame_that_was_already_there_is_never_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(office_com, "_IDENTIFY_SETTLE_S", 0.0)
    monkeypatch.setattr(office_com, "frame_windows", lambda kind: {0x10: 7, 0x22: 901})

    instance = office_com._identify(FakeApp(), "word", {7}, {0x10: 7})

    assert (instance.window_id, instance.pid) == (0x22, 901)
    assert instance.adopted is False


def test_a_new_frame_behind_an_old_process_is_reported_as_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DispatchEx` is supposed to make a private instance. When the window it
    produces belongs to a process that was already running, the launch has to
    say so rather than reparent somebody's session."""
    monkeypatch.setattr(office_com, "_IDENTIFY_SETTLE_S", 0.0)
    monkeypatch.setattr(office_com, "frame_windows", lambda kind: {0x22: 901})

    assert office_com._identify(FakeApp(), "word", {901}, {}).adopted is True


def test_excel_is_asked_which_frame_it_owns_instead_of_being_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Application.Hwnd` is a correlation, not an inference: a second Excel
    starting mid-launch cannot confuse it, because there is no candidate set."""
    win32 = FakeWin32(classes={0x55: "XLMAIN"}, pids={0x55: 4242})
    monkeypatch.setattr(office_com, "win32gui", win32, raising=False)
    monkeypatch.setattr(office_com, "win32process", win32, raising=False)
    monkeypatch.setattr(office_com, "frame_windows", lambda kind: {0x55: 4242, 0x66: 4243})

    instance = office_com._identify(FakeApp(hwnd=0x55), "excel", set(), {})

    assert (instance.window_id, instance.pid) == (0x55, 4242)


def test_excel_is_walled_off_from_the_users_other_workbooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `/x` isolation, in-process: our private Excel is a DDE server, so
    without ``IgnoreRemoteRequests`` the next workbook the user opens from
    Explorer could land in the window Workbench is hosting."""
    win32 = FakeWin32(classes={0x55: "XLMAIN"}, pids={0x55: 4242})
    monkeypatch.setattr(office_com, "win32gui", win32, raising=False)
    monkeypatch.setattr(office_com, "win32process", win32, raising=False)
    monkeypatch.setattr(office_com, "frame_windows", lambda kind: {0x55: 4242})
    app = FakeApp(hwnd=0x55)

    office_com._identify(app, "excel", set(), {})

    assert app.ignore_remote_requests is True


def test_word_is_not_asked_to_ignore_remote_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Word runs no DDE server and has no such property; touching it would raise
    where the isolation must stay Excel-only."""
    monkeypatch.setattr(office_com, "_IDENTIFY_SETTLE_S", 0.0)
    monkeypatch.setattr(office_com, "frame_windows", lambda kind: {0x22: 900})
    app = FakeApp()

    office_com._identify(app, "word", {1}, {})

    assert app.ignore_remote_requests is None


# ---- opening: a stranger's process is released, never terminated --------------


def _word_at(root_pid: int) -> FakeWin32:
    return FakeWin32(classes={0x99: "OpusApp"}, pids={0x99: root_pid})


def test_a_frame_belonging_to_another_process_is_named_as_foreign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(office_com, "win32gui", _word_at(5555), raising=False)
    monkeypatch.setattr(office_com, "win32process", _word_at(5555), raising=False)
    instance = an_instance(None, FakeApp(), window_id=0x11)

    with pytest.raises(office_com.ForeignWindowError):
        office_com._open_document(instance, Path("report.docx"))


def test_a_second_frame_of_our_own_instance_is_a_plain_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Still wrong, still refused — but ours, so the reaping that follows is
    allowed to be a kill."""
    monkeypatch.setattr(office_com, "win32gui", _word_at(4321), raising=False)
    monkeypatch.setattr(office_com, "win32process", _word_at(4321), raising=False)
    instance = an_instance(None, FakeApp(), window_id=0x11)

    with pytest.raises(office_com.OfficeComError, match="another frame of its own"):
        office_com._open_document(instance, Path("report.docx"))
    assert instance.document is None


def _launch_against(
    monkeypatch: pytest.MonkeyPatch, app: FakeApp, win32: FakeWin32, frames: dict[int, int]
) -> list[bool]:
    """Run `launch` with only the Win32 edges replaced, and report every
    `_release_job` decision it made."""
    released: list[bool] = []
    readings = iter([{}, frames, frames])

    monkeypatch.setattr(office_com, "WINDOWS", True)
    monkeypatch.setattr(office_com, "_IDENTIFY_SETTLE_S", 0.0)
    monkeypatch.setattr(office_com, "document_is_open_elsewhere", lambda path: False)
    monkeypatch.setattr(office_com, "running_pids", set)
    monkeypatch.setattr(office_com, "frame_windows", lambda kind: next(readings))
    monkeypatch.setattr(office_com, "win32com", FakeCom(app), raising=False)
    monkeypatch.setattr(office_com, "win32gui", win32, raising=False)
    monkeypatch.setattr(office_com, "win32process", win32, raising=False)
    monkeypatch.setattr(office_com, "_contain", lambda instance: None)
    monkeypatch.setattr(
        office_com,
        "_release_job",
        lambda instance, *, kill: released.append(kill),
    )
    return released


def test_a_refused_identification_still_ends_the_instance_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing to name the window must not leave a Word behind. Nothing was
    contained, so the COM object is the only thing that names our own instance
    exactly — and it is the only thing touched."""
    app = FakeApp()
    released = _launch_against(monkeypatch, app, _word_at(900), {0x11: 900, 0x22: 901})
    document = tmp_path / "report.docx"
    document.write_bytes(b"PK")

    with pytest.raises(office_com.OfficeComError, match="refusing to guess"):
        office_com.launch(document, "word")

    assert app.quits == 1
    assert released == []  # no job was ever taken, so none is released or killed


def test_a_document_that_lands_in_a_strangers_window_never_kills_that_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst case the ownership rules exist for. If identification ever did
    contain a window the user had just opened, the self-check catches it — and
    the cleanup must not then terminate the job holding *their* process."""
    app = FakeApp(document=FakeDocument(hwnd=0x99))
    released = _launch_against(monkeypatch, app, _word_at(5555), {0x88: 900})
    document = tmp_path / "report.docx"
    document.write_bytes(b"PK")

    with pytest.raises(office_com.ForeignWindowError):
        office_com.launch(document, "word")

    assert released == [False]  # the flag is cleared; the stranger survives
    assert app.quits == 1  # and only our own instance is ended, through COM


def test_a_launch_that_fails_for_any_other_reason_still_reaps_what_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release above is narrow on purpose: an instance that *is* ours and
    cannot be opened is still killed, or a failed launch would leak a Word."""
    app = FakeApp(document=FakeDocument(hwnd=0x99, read_only=True))
    released = _launch_against(monkeypatch, app, _word_at(900), {0x99: 900})
    document = tmp_path / "report.docx"
    document.write_bytes(b"PK")

    with pytest.raises(office_com.DocumentBusyError):
        office_com.launch(document, "word")

    assert released == [True]
