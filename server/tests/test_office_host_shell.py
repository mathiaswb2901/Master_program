"""The real host backend's two halves, without Office and without a shell.

The COM half (`office_com`) is exercised through the seams that are *not* COM:
which document counts as open elsewhere, and what the ownership snapshot is for.
Everything that actually starts Word is verified by running it — see the PR body
— because a mock of `DispatchEx` would only assert that the mock was called.

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

from workbench_server.models.office_host import HostCommand, HostCommandAck, PanelRect
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
