"""The Office host domain layer, up close.

Everything here runs with no Microsoft Office, no Rust and no window manager —
that is the point of the seam. The fake backend walks the same lifecycle the
Win32 one will, so the states a user can actually get stuck in (the launch that
never produces a window, the embed that is refused, the application that dies
under us, the close that lands mid-embed) are reachable and asserted in CI.

The owner decisions are tested as rules, not as comments: a process we did not
launch is never adopted, and PowerPoint — hostable since 2026-08-09, but only
when Workbench started the process — is refused with a reason of its own when one
is already running, and reported as unavailable *before* a launch is attempted.
The refusal names *whose* instance is in the way, because only one of the two has
a window the user can close: a deck Workbench is hosting is docked in a panel,
and a top-level window probe cannot even see it once the frame is reparented.
"""

import asyncio
import json
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.office_host import (
    HOSTABLE_KINDS,
    HostState,
    OfficeHostEvent,
    OfficeHostInfo,
    PanelRect,
    host_app_kind,
)
from workbench_server.services.event_bus import EventBus
from workbench_server.services.office_host.backend import HostBackendError, HostHandle
from workbench_server.services.office_host.fake_backend import (
    FIRST_FAKE_PID,
    FakeFailure,
    FakeHostBackend,
    failure_for,
)
from workbench_server.services.office_host.service import (
    DEFAULT_RECT,
    LAUNCH_TIMEOUT_S,
    MAX_HOSTS,
    OPERATION_TIMEOUT_S,
    HostNotFoundError,
    HostRefusedError,
    HostStateError,
    OfficeHostService,
    build_backend,
    detect_office,
)
from workbench_server.services.office_host.shell_backend import ShellHostBackend
from workbench_server.services.office_host.shell_channel import ShellChannel
from workbench_server.services.office_host.state import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    ForeignProcessError,
    HostLifecycle,
    IllegalTransitionError,
    can_transition,
    is_terminal,
)
from workbench_server.services.workspace import PathOutsideWorkspaceError, Workspace

DOCX_BYTES = b"PK\x03\x04 fake office bytes"
RECT = PanelRect(x=10, y=20, width=640, height=480)
MOVED = PanelRect(x=0, y=0, width=1024, height=768)


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[BaseModel] = []

    def publish(self, event: BaseModel) -> None:
        self.published.append(event)
        super().publish(event)

    def hosts(self) -> list[OfficeHostInfo]:
        return [e.host for e in self.published if isinstance(e, OfficeHostEvent)]

    def states(self) -> list[HostState]:
        return [host.state for host in self.hosts()]


def document(root: Path, name: str = "report.docx") -> str:
    (root / name).write_bytes(DOCX_BYTES)
    return name


def make_service(
    root: Path,
    backend: FakeHostBackend | None = None,
    *,
    mode: str = "on",
    fake: bool = True,
    office_installed: bool = False,
    running_kinds: frozenset[str] = frozenset(),
    poll_interval_s: float = 60.0,
    launch_timeout_s: float = LAUNCH_TIMEOUT_S,
    operation_timeout_s: float = OPERATION_TIMEOUT_S,
) -> tuple[OfficeHostService, RecordingBus]:
    """A service on a real workspace with a scripted backend.

    ``office_installed`` and ``running_kinds`` are injected rather than probed:
    the suite must give the same answer on the author's machine (which has Office,
    and may well have PowerPoint open) and on CI (which has neither, and no window
    station to enumerate).
    """
    bus = RecordingBus()
    service = OfficeHostService(
        Workspace(root),
        bus,
        backend,
        mode=mode,  # type: ignore[arg-type]
        fake=fake,
        detector=lambda: office_installed,
        running_probe=lambda kind: kind in running_kinds,
        poll_interval_s=poll_interval_s,
        launch_timeout_s=launch_timeout_s,
        operation_timeout_s=operation_timeout_s,
    )
    return service, bus


async def until(predicate: Callable[[], bool], tries: int = 500, delay: float = 0.0) -> None:
    """Yield to the loop until ``predicate`` holds. ``delay`` stays 0 for the
    service-level tests — everything the fake backend does is in-process, so a
    scheduling turn is all it needs — and is raised only when a real HTTP round
    trip is in the way."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(delay)
    raise AssertionError("condition never became true")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowLaunch(FakeHostBackend):
    """A launch held open until the test lets it finish.

    The real one costs about a second of awaited work, which is long enough for
    a panel to be laid out, resized and closed underneath it — so the tests that
    care about that window need it to take as long as they say.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def launch(self, path: Path, kind: Any, host_id: str) -> HostHandle:
        await self.gate.wait()
        return await super().launch(path, kind, host_id)


class HangingBackend(FakeHostBackend):
    """A backend that forgets to time itself out.

    The contract says every method comes back and the implementation bounds its
    own work; this is the one that does not, which is the whole reason the
    service keeps a ceiling of its own. Whatever it is told to hang on never
    returns at all.
    """

    def __init__(self, hang: str) -> None:
        super().__init__()
        self.hang = hang
        self.forever = asyncio.Event()  # never set, by design

    async def _maybe_hang(self, method: str) -> None:
        if self.hang == method:
            await self.forever.wait()

    async def launch(self, path: Path, kind: Any, host_id: str) -> HostHandle:
        await self._maybe_hang("launch")
        return await super().launch(path, kind, host_id)

    async def embed(self, handle: HostHandle, rect: PanelRect) -> None:
        await self._maybe_hang("embed")
        await super().embed(handle, rect)

    async def set_bounds(self, handle: HostHandle, rect: PanelRect) -> None:
        await self._maybe_hang("set_bounds")
        await super().set_bounds(handle, rect)

    async def close(self, handle: HostHandle) -> None:
        await self._maybe_hang("close")
        await super().close(handle)


class RefusingClose(FakeHostBackend):
    """Word with a "Save changes?" modal in front of it: the close bounces.

    The process stays alive and stays *ours* — which is exactly the case where
    reporting the host as closed and forgetting it would strand a real window.
    """

    def __init__(self, refusals: int) -> None:
        super().__init__()
        self.refusals = refusals

    async def close(self, handle: HostHandle) -> None:
        if self.refusals > 0:
            self.refusals -= 1
            self.calls.append(("close_refused", str(handle.pid)))
            raise HostBackendError("the document has unsaved changes")
        await super().close(handle)


# ---- the state machine -------------------------------------------------------


def lifecycle(state: HostState = "launching", clock: FakeClock | None = None) -> HostLifecycle:
    life = HostLifecycle("host-1", "report.docx", "word", clock=clock or FakeClock())
    life.state = state
    return life


def all_states() -> Iterator[HostState]:
    yield from LEGAL_TRANSITIONS


def test_every_legal_transition_is_accepted() -> None:
    for source in all_states():
        for target in LEGAL_TRANSITIONS[source]:
            life = lifecycle(source)
            life.to(target)
            assert life.state == target


def test_every_other_transition_is_rejected() -> None:
    """Including a state to itself: a host that is already embedding does not
    get to be embedded twice, and the second caller has to find out."""
    for source in all_states():
        for target in set(all_states()) - LEGAL_TRANSITIONS[source]:
            life = lifecycle(source)
            with pytest.raises(IllegalTransitionError):
                life.to(target)
            assert life.state == source


def test_a_settled_host_never_moves_again() -> None:
    for terminal in TERMINAL_STATES:
        assert is_terminal(terminal)
        assert not any(can_transition(terminal, target) for target in all_states())


def test_the_lifecycle_starts_launching_and_carries_no_reason() -> None:
    life = lifecycle()
    assert life.state == "launching"
    assert life.reason is None
    assert life.pid is None
    assert life.info().host_id == "host-1"


def test_a_reason_belongs_to_the_state_that_ended_the_host() -> None:
    life = lifecycle("launching")
    life.to("embedding", reason="user_closed")
    assert life.reason is None  # a live state carries no reason, whatever is passed
    life.to("embedded")
    life.to("crashed", reason="process_exited")
    assert life.reason == "process_exited"


def test_since_marks_when_the_current_state_was_entered() -> None:
    clock = FakeClock()
    life = lifecycle("launching", clock)
    assert life.since == 1000.0
    clock.advance(5)
    life.to("embedding")
    assert life.since == 1005.0
    assert life.info().since == 1005.0


def test_a_host_binds_one_process_for_life() -> None:
    """The never-adopt rule, at its smallest: the pid recorded at launch is the
    only one this host will ever answer for."""
    life = lifecycle()
    life.bind_pid(4242)
    life.bind_pid(4242)  # idempotent: the same instance, re-announced
    with pytest.raises(ForeignProcessError):
        life.bind_pid(9999)
    assert life.pid == 4242


# ---- opening, moving, closing ------------------------------------------------


async def test_opening_walks_launching_embedding_embedded(tmp_path: Path) -> None:
    backend = FakeHostBackend()
    service, bus = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)

    assert bus.states() == ["launching", "embedding", "embedded"]
    assert info.state == "embedded"
    assert info.reason is None
    assert info.kind == "word"
    assert info.pid == FIRST_FAKE_PID
    assert backend.calls == [("launch", "report.docx"), ("embed", "640x480")]
    assert service.snapshot().hosts == [info]


async def test_opening_an_xlsx_docks_as_excel(tmp_path: Path) -> None:
    """The Word lifecycle, walked for a workbook. A ``.xlsx`` resolves to the
    excel host and the fake backend embeds it with no Excel and no window — the
    proof that hosting is not Word-only at the domain layer, driven the way the
    E2E drives the UI half."""
    backend = FakeHostBackend()
    service, bus = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path, "book.xlsx"), RECT)

    assert bus.states() == ["launching", "embedding", "embedded"]
    assert info.state == "embedded"
    assert info.reason is None
    assert info.kind == "excel"
    assert host_app_kind("book.xlsx") == "excel"
    assert "excel" in HOSTABLE_KINDS
    assert backend.calls == [("launch", "book.xlsx"), ("embed", "640x480")]


async def test_an_xlsx_embed_that_is_refused_settles_failed(tmp_path: Path) -> None:
    """The refused-embed path is not Word's alone: a workbook whose window will
    not dock ends ``failed``/``embed_refused``, which is what turns the panel
    over to the preview editor (the E2E asserts the UI half)."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path, "book-refuse-embed.xlsx"), RECT)

    assert (info.state, info.reason, info.kind) == ("failed", "embed_refused", "excel")


async def test_a_host_opened_without_bounds_gets_the_default_rect(tmp_path: Path) -> None:
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    await service.open(document(tmp_path))
    assert ("embed", f"{DEFAULT_RECT.width}x{DEFAULT_RECT.height}") in backend.calls


async def test_bounds_move_an_embedded_window(tmp_path: Path) -> None:
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)
    moved = await service.set_bounds(info.host_id, MOVED)
    assert moved.state == "embedded"
    assert backend.calls[-1] == ("set_bounds", "0,0 1024x768")


async def test_bounds_that_arrive_during_the_embed_are_applied_afterwards(tmp_path: Path) -> None:
    """A panel resized while Word is still starting must not be left at the
    rectangle it had a second ago — there is no second event to correct it."""
    backend = FakeHostBackend()
    backend.embed_gate = asyncio.Event()
    service, bus = make_service(tmp_path, backend)
    opening = asyncio.create_task(service.open(document(tmp_path), RECT))
    await until(lambda: "embedding" in bus.states())

    host_id = bus.hosts()[-1].host_id
    await service.set_bounds(host_id, MOVED)
    assert ("set_bounds", "0,0 1024x768") not in backend.calls  # nothing to move yet
    backend.embed_gate.set()
    info = await opening

    assert info.state == "embedded"
    assert backend.calls[-1] == ("set_bounds", "0,0 1024x768")


async def test_bounds_that_arrive_during_the_launch_are_where_the_window_lands(
    tmp_path: Path,
) -> None:
    """A resize during the ~1s launch is not merely late — without care it is
    *lost*. The open call carried a rectangle too, and re-reading that one when
    the launch finally returns overwrites the newer answer with the older one.
    There is no second event to correct it: the panel already told us once."""
    backend = SlowLaunch()
    service, bus = make_service(tmp_path, backend)
    opening = asyncio.create_task(service.open(document(tmp_path), RECT))
    await until(lambda: bus.states() == ["launching"])

    host_id = bus.hosts()[-1].host_id
    await service.set_bounds(host_id, MOVED)  # the panel moved while Word started
    backend.gate.set()
    info = await opening

    assert info.state == "embedded"
    # Embedded *at* the new rectangle — not embedded at the stale one, and not
    # embedded at the stale one and quietly moved afterwards either.
    assert ("embed", "1024x768") in backend.calls
    assert ("embed", "640x480") not in backend.calls
    assert ("set_bounds", "0,0 1024x768") not in backend.calls


async def test_a_hidden_panel_hides_the_window_it_holds(tmp_path: Path) -> None:
    """A hosted window is a real window: switching editor tabs puts a `div`
    behind another one and leaves a Word painted over it. Something has to say
    so, and saying it twice must not cost a Win32 call."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)

    await service.set_visible(info.host_id, False)
    assert backend.calls[-1] == ("set_visible", "hide")
    await service.set_visible(info.host_id, False)  # the same answer again
    assert backend.calls.count(("set_visible", "hide")) == 1

    await service.set_visible(info.host_id, True)
    assert backend.calls[-1] == ("set_visible", "show")


async def test_a_tab_hidden_during_the_launch_does_not_come_back_as_a_window(
    tmp_path: Path,
) -> None:
    """The same race as the resize during a launch, and worse: the window is
    *shown* by the embed, so a tab switched away from while Word was starting
    would put a real document over whatever the user moved to."""
    backend = SlowLaunch()
    service, bus = make_service(tmp_path, backend)
    opening = asyncio.create_task(service.open(document(tmp_path), RECT))
    await until(lambda: bus.states() == ["launching"])

    host_id = bus.hosts()[-1].host_id
    await service.set_visible(host_id, False)
    assert ("set_visible", "hide") not in backend.calls  # nothing to hide yet
    backend.gate.set()
    info = await opening

    assert info.state == "embedded"
    assert backend.calls[-1] == ("set_visible", "hide")


async def test_showing_or_hiding_a_settled_host_is_a_conflict(tmp_path: Path) -> None:
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)
    await service.close(info.host_id)

    with pytest.raises(HostStateError):
        await service.set_visible(info.host_id, False)


async def test_capabilities_report_whether_a_shell_is_attached(tmp_path: Path) -> None:
    """The UI degrades from this: the same server serves a browser tab, which
    can never host anything, and the fake backend hosts nothing anywhere."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend, mode="on", fake=True)
    assert service.capabilities(onlyoffice_enabled=True).native_hosting is True

    backend.available = False  # what "no shell attached" looks like from here
    caps = service.capabilities(onlyoffice_enabled=True)
    assert caps.native_hosting is False
    assert caps.hostable_kinds == []
    assert caps.fallback == "onlyoffice"
    with pytest.raises(HostRefusedError) as refused:
        await service.open(document(tmp_path), RECT)
    assert refused.value.reason == "native_hosting_disabled"


async def test_closing_settles_the_host_and_reaps_the_instance(tmp_path: Path) -> None:
    backend = FakeHostBackend()
    service, bus = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)
    closed = await service.close(info.host_id)

    assert closed.state == "closed"
    assert closed.reason == "user_closed"
    assert ("close", str(FIRST_FAKE_PID)) in backend.calls
    assert bus.states() == ["launching", "embedding", "embedded", "closed"]
    # The record survives: it is the answer to "what happened to that document".
    assert [host.state for host in service.snapshot().hosts] == ["closed"]


async def test_closing_twice_is_the_same_answer_not_an_error(tmp_path: Path) -> None:
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)
    first = await service.close(info.host_id)
    second = await service.close(info.host_id)

    assert (second.state, second.reason) == (first.state, first.reason)
    assert backend.calls.count(("close", str(FIRST_FAKE_PID))) == 1


async def test_moving_a_settled_host_is_refused(tmp_path: Path) -> None:
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)
    await service.close(info.host_id)
    with pytest.raises(HostStateError):
        await service.set_bounds(info.host_id, MOVED)


async def test_an_unknown_host_id_is_not_found(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, FakeHostBackend())
    with pytest.raises(HostNotFoundError):
        await service.close("host-nope")


async def test_detaching_keeps_the_document_open_and_reopening_re_embeds(tmp_path: Path) -> None:
    backend = FakeHostBackend()
    service, bus = make_service(tmp_path, backend)
    path = document(tmp_path)
    info = await service.open(path, RECT)

    detached = await service.detach(info.host_id)
    assert detached.state == "detached"
    assert detached.reason is None  # not terminal: the document is still open
    assert ("detach", str(FIRST_FAKE_PID)) in backend.calls

    again = await service.open(path, MOVED)
    assert again.host_id == info.host_id  # the same instance, back in the panel
    assert again.state == "embedded"
    assert bus.states() == [
        "launching",
        "embedding",
        "embedded",
        "detached",
        "embedding",
        "embedded",
    ]
    assert backend.calls.count(("launch", "report.docx")) == 1


async def test_detaching_a_host_that_is_not_embedded_is_refused(tmp_path: Path) -> None:
    backend = FakeHostBackend(failure="launch_failed")
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)
    with pytest.raises(HostStateError):
        await service.detach(info.host_id)


async def test_a_second_open_of_the_same_document_reuses_the_host(tmp_path: Path) -> None:
    """Two panels reparenting one window is not a thing that can work, so the
    second request is the first host — moved, if it brought bounds."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    path = document(tmp_path)
    first = await service.open(path, RECT)
    second = await service.open(path, MOVED)

    assert second.host_id == first.host_id
    assert backend.calls.count(("launch", "report.docx")) == 1
    assert backend.calls[-1] == ("set_bounds", "0,0 1024x768")


# ---- every way it can go wrong -----------------------------------------------


@pytest.mark.parametrize(
    ("failure", "reason"),
    [("launch_timeout", "launch_timeout"), ("launch_failed", "launch_failed")],
)
async def test_a_launch_that_never_arrives_settles_failed(
    tmp_path: Path, failure: FakeFailure, reason: str
) -> None:
    backend = FakeHostBackend(failure=failure)
    service, bus = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)

    assert info.state == "failed"
    assert info.reason == reason
    assert info.pid is None  # nothing was launched, so nothing is claimed
    assert bus.states() == ["launching", "failed"]
    assert [call for call, _ in backend.calls] == ["launch"]


async def test_a_refused_embed_reaps_the_instance_we_started(tmp_path: Path) -> None:
    """The launch worked, so an Office process exists that nobody can see. It is
    ours, and it has to be closed before the host settles."""
    backend = FakeHostBackend(failure="embed_refused")
    service, bus = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)

    assert (info.state, info.reason) == ("failed", "embed_refused")
    assert info.pid == FIRST_FAKE_PID
    assert ("close", str(FIRST_FAKE_PID)) in backend.calls
    assert bus.states() == ["launching", "embedding", "failed"]


async def test_a_crash_after_embedding_is_noticed_by_the_poll(tmp_path: Path) -> None:
    backend = FakeHostBackend(failure="crash_after_embed")
    service, bus = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)
    assert info.state == "embedded"  # it really did embed; the death comes after

    await service.poll_once()
    crashed = service.get(info.host_id)
    assert (crashed.state, crashed.reason) == ("crashed", "process_exited")
    assert bus.states()[-1] == "crashed"
    # Nothing to close: the process is already gone.
    assert ("close", str(FIRST_FAKE_PID)) not in backend.calls


@pytest.mark.timeout(30)
async def test_the_poll_task_notices_a_crash_nobody_asked_about(tmp_path: Path) -> None:
    """The loop, not just the sweep: a window the user quits from Word's own
    File menu must stop being reported as embedded."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend, poll_interval_s=0.01)
    info = await service.open(document(tmp_path), RECT)
    service.start()
    try:
        backend.kill(FIRST_FAKE_PID)
        for _ in range(500):
            if service.get(info.host_id).state == "crashed":
                break
            await asyncio.sleep(0.01)
        assert service.get(info.host_id).state == "crashed"
    finally:
        await service.shutdown()


async def test_closing_while_the_embed_is_in_flight_never_reaches_embedded(
    tmp_path: Path,
) -> None:
    backend = FakeHostBackend()
    backend.embed_gate = asyncio.Event()
    service, bus = make_service(tmp_path, backend)
    opening = asyncio.create_task(service.open(document(tmp_path), RECT))
    await until(lambda: "embedding" in bus.states())

    host_id = bus.hosts()[-1].host_id
    closed = await service.close(host_id)
    assert (closed.state, closed.reason) == ("closed", "user_closed")

    backend.embed_gate.set()
    settled = await opening

    assert settled.state == "closed"
    assert bus.states() == ["launching", "embedding", "closed"]
    # Reaped exactly once, by whichever path got there first.
    assert backend.calls.count(("close", str(FIRST_FAKE_PID))) == 1


async def test_closing_while_the_launch_is_in_flight_still_reaps_it(tmp_path: Path) -> None:
    """The window did not exist when the user gave up on it, and existed a
    moment later. Nobody else is going to close that process."""
    backend = SlowLaunch()
    service, bus = make_service(tmp_path, backend)
    opening = asyncio.create_task(service.open(document(tmp_path), RECT))
    await until(lambda: "launching" in bus.states())

    host_id = bus.hosts()[-1].host_id
    await service.close(host_id)
    backend.gate.set()
    settled = await opening

    assert settled.state == "closed"
    assert backend.calls.count(("close", str(FIRST_FAKE_PID))) == 1
    assert ("embed", "640x480") not in backend.calls


# ---- a backend that never comes back -----------------------------------------
# The Protocol asks every implementation to bound its own work and raise. The
# service does not take that on trust: one backend that forgets would otherwise
# hang the request that started it, and with it the shutdown that would have
# reaped the window. The ceiling is a backstop, and these are the tests that say
# it is really there.


@pytest.mark.timeout(30)
async def test_a_launch_that_never_returns_is_cut_off(tmp_path: Path) -> None:
    backend = HangingBackend("launch")
    service, bus = make_service(tmp_path, backend, launch_timeout_s=0.01)
    info = await service.open(document(tmp_path), RECT)

    assert (info.state, info.reason) == ("failed", "launch_timeout")
    assert info.pid is None
    assert bus.states() == ["launching", "failed"]


@pytest.mark.timeout(30)
async def test_an_embed_that_never_returns_is_cut_off_and_the_instance_reaped(
    tmp_path: Path,
) -> None:
    """The launch worked, so there is a process; the embed never finished, so
    nobody can see it. Same obligation as a refused embed: reap it."""
    backend = HangingBackend("embed")
    service, _ = make_service(tmp_path, backend, operation_timeout_s=0.01)
    info = await service.open(document(tmp_path), RECT)

    assert (info.state, info.reason) == ("failed", "backend_timeout")
    assert ("close", str(FIRST_FAKE_PID)) in backend.calls


@pytest.mark.timeout(30)
async def test_a_move_that_never_returns_settles_the_host(tmp_path: Path) -> None:
    backend = HangingBackend("set_bounds")
    service, _ = make_service(tmp_path, backend, operation_timeout_s=0.01)
    info = await service.open(document(tmp_path), RECT)
    moved = await service.set_bounds(info.host_id, MOVED)

    assert (moved.state, moved.reason) == ("failed", "backend_timeout")
    assert ("close", str(FIRST_FAKE_PID)) in backend.calls


@pytest.mark.timeout(30)
async def test_a_close_that_never_returns_does_not_stall_the_shutdown(tmp_path: Path) -> None:
    """The lifespan has to finish. A backend hanging on close must cost the
    ceiling and no more — and must not be recorded as a close that happened."""
    backend = HangingBackend("close")
    service, _ = make_service(tmp_path, backend, operation_timeout_s=0.01)
    info = await service.open(document(tmp_path), RECT)

    await service.shutdown()

    settled = service.get(info.host_id)
    assert (settled.state, settled.reason) == ("closed", "server_shutdown")
    assert settled.close_failed is True  # we asked, and never got an answer


# ---- a close the instance refuses --------------------------------------------


async def test_a_close_the_instance_refuses_is_not_reported_as_done(tmp_path: Path) -> None:
    """A bounced close reaped nothing. The host settles either way — settling is
    final — but the record must not claim the process went with it, and the
    sweep has to be able to ask again."""
    backend = RefusingClose(refusals=1)
    service, bus = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)

    closed = await service.close(info.host_id)
    assert (closed.state, closed.reason) == ("closed", "user_closed")
    assert closed.close_failed is True
    assert bus.hosts()[-1].close_failed is True  # and the UI is told

    await service.poll_once()  # the modal was dismissed; ask again

    settled = service.get(info.host_id)
    assert settled.close_failed is False
    assert ("close", str(FIRST_FAKE_PID)) in backend.calls
    assert bus.hosts()[-1].close_failed is False


async def test_a_refused_close_stops_being_chased_once_the_process_is_gone(
    tmp_path: Path,
) -> None:
    """However it went — our close finally landing, or the user quitting Word
    from its own File menu — a process that is gone is not chased again."""
    backend = RefusingClose(refusals=99)
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)
    assert (await service.close(info.host_id)).close_failed is True

    backend.kill(FIRST_FAKE_PID)
    await service.poll_once()
    assert service.get(info.host_id).close_failed is False

    calls = len(backend.calls)
    await service.poll_once()
    assert len(backend.calls) == calls  # nothing left to ask


async def test_a_host_that_still_owes_a_close_is_pruned_last(tmp_path: Path) -> None:
    """Its record is the only handle to a process still on the user's screen, so
    it outlives every settled host that really did go away."""
    backend = RefusingClose(refusals=1)
    service, _ = make_service(tmp_path, backend)
    stuck = await service.open(document(tmp_path, "stuck.docx"), RECT)
    await service.close(stuck.host_id)
    assert service.get(stuck.host_id).close_failed is True

    for index in range(MAX_HOSTS + 4):
        info = await service.open(document(tmp_path, f"doc-{index:03d}.docx"), RECT)
        await service.close(info.host_id)

    hosts = service.snapshot().hosts
    assert len(hosts) <= MAX_HOSTS
    assert stuck.host_id in {host.host_id for host in hosts}


# ---- ownership: never adopt a process we did not launch ----------------------


async def test_a_document_already_open_elsewhere_is_refused_not_taken_over(
    tmp_path: Path,
) -> None:
    """The backend found the document open in an instance it did not start.
    Reparenting it would hijack the user's own window, and closing our panel
    would then close their work — so it is a refusal with a reason, and we do
    not so much as close it on the way out."""
    backend = FakeHostBackend(failure="already_open")
    service, bus = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)

    assert (info.state, info.reason) == ("failed", "document_open_elsewhere")
    assert info.pid is None  # we never claimed it
    assert [call for call, _ in backend.calls] == ["launch"]  # no embed, no close
    assert bus.states() == ["launching", "failed"]


async def test_a_handle_for_another_process_is_refused_even_for_a_resize(
    tmp_path: Path,
) -> None:
    """The pid is bound at launch, so a backend cannot substitute a window we
    did not start later in the host's life — not even for something as innocent
    as a move."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)

    service._hosts[info.host_id].handle = HostHandle(pid=4321, window_id=99)
    refused = await service.set_bounds(info.host_id, MOVED)

    assert (refused.state, refused.reason) == ("failed", "document_open_elsewhere")
    assert ("set_bounds", "0,0 1024x768") not in backend.calls
    assert ("close", "4321") not in backend.calls  # never ours, never closed


# ---- policy refusals ---------------------------------------------------------


async def test_a_deck_docks_like_a_document(tmp_path: Path) -> None:
    """PowerPoint is hosted, not previewed: the same launching -> embedding ->
    embedded walk Word and Excel take, as a ``powerpoint`` host."""
    backend = FakeHostBackend()
    service, events = make_service(tmp_path, backend)
    (tmp_path / "deck.pptx").write_bytes(DOCX_BYTES)

    info = await service.open("deck.pptx", RECT)

    assert host_app_kind("deck.pptx") == "powerpoint"
    assert "powerpoint" in HOSTABLE_KINDS
    assert (info.kind, info.state, info.reason) == ("powerpoint", "embedded", None)
    assert events.states() == ["launching", "embedding", "embedded"]
    assert backend.calls[0] == ("launch", "deck.pptx")


async def test_a_deck_is_refused_while_powerpoint_is_already_running(tmp_path: Path) -> None:
    """The single-instance rule, end to end.

    PowerPoint's COM class factory is multi-use, so a launch made while one is
    running returns the *user's* instance — which must never reach the job object
    that reaps ours. The backend refuses before creating anything, and the host
    settles with a reason that names the fix (close PowerPoint), not a generic
    failure.
    """
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    (tmp_path / "app-running-deck.pptx").write_bytes(DOCX_BYTES)

    info = await service.open("app-running-deck.pptx", RECT)

    assert (info.state, info.reason) == ("failed", "powerpoint_already_running")
    # Nothing was embedded, and no process was left behind to reap: the refusal
    # happens before anything is launched.
    assert [call for call, _ in backend.calls] == ["launch"]


async def test_a_file_that_is_not_a_document_is_refused(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, FakeHostBackend())
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(HostRefusedError) as raised:
        await service.open("notes.txt", RECT)
    assert raised.value.reason == "unsupported_file"


@pytest.mark.parametrize(
    ("mode", "fake"),
    [
        ("off", True),  # the knob wins over the fake backend
        ("off", False),
        ("auto", False),  # today's default: no native hosting
        ("on", False),  # asked for, but no backend exists on this machine yet
    ],
)
async def test_hosting_is_refused_when_no_backend_may_run(
    tmp_path: Path, mode: str, fake: bool
) -> None:
    service, _ = make_service(
        tmp_path,
        build_backend(mode, fake),  # type: ignore[arg-type]
        mode=mode,
        fake=fake,
    )
    with pytest.raises(HostRefusedError) as raised:
        await service.open(document(tmp_path), RECT)
    assert raised.value.reason == "native_hosting_disabled"


async def test_off_wins_over_a_backend_somebody_wired_in(tmp_path: Path) -> None:
    """The knob is a promise: with it off, no path in this service starts Word."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend, mode="off")
    with pytest.raises(HostRefusedError):
        await service.open(document(tmp_path), RECT)
    assert backend.calls == []


async def test_a_path_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, FakeHostBackend())
    with pytest.raises(PathOutsideWorkspaceError):
        await service.open("../secrets.docx", RECT)


async def test_a_document_that_is_not_there_is_refused(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, FakeHostBackend())
    with pytest.raises(FileNotFoundError):
        await service.open("missing.docx", RECT)


# ---- shutdown ----------------------------------------------------------------


async def test_shutdown_reaps_every_live_host(tmp_path: Path) -> None:
    """A hosted window outliving the server is an Office instance nobody owns,
    still wearing our panel's chrome."""
    backend = FakeHostBackend()
    service, bus = make_service(tmp_path, backend)
    first = await service.open(document(tmp_path, "one.docx"), RECT)
    second = await service.open(document(tmp_path, "two.xlsx"), RECT)
    gone = await service.open(document(tmp_path, "three.docx"), RECT)
    await service.close(gone.host_id)

    await service.shutdown()

    states = {host.host_id: host for host in service.snapshot().hosts}
    assert states[first.host_id].reason == "server_shutdown"
    assert states[second.host_id].reason == "server_shutdown"
    assert all(host.state == "closed" for host in states.values())
    # The one that was already closed is not closed a second time.
    assert states[gone.host_id].reason == "user_closed"
    assert [call for call, _ in backend.calls].count("close") == 3
    assert bus.states()[-2:] == ["closed", "closed"]


async def test_shutdown_is_safe_with_no_backend_at_all(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, None, mode="auto", fake=False)
    service.start()
    await service.shutdown()
    assert service.snapshot().hosts == []


async def test_settled_hosts_are_pruned_so_the_map_stays_bounded(tmp_path: Path) -> None:
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    for index in range(MAX_HOSTS + 4):
        info = await service.open(document(tmp_path, f"doc-{index:03d}.docx"), RECT)
        await service.close(info.host_id)
    assert len(service.snapshot().hosts) <= MAX_HOSTS


async def test_a_live_host_is_never_pruned(tmp_path: Path) -> None:
    """Pruning a live host would lose the only handle to a window somebody still
    has to close."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    live = [await service.open(document(tmp_path, f"live-{i:03d}.docx"), RECT) for i in range(4)]
    for index in range(MAX_HOSTS + 4):
        info = await service.open(document(tmp_path, f"doc-{index:03d}.docx"), RECT)
        await service.close(info.host_id)

    hosts = {host.host_id for host in service.snapshot().hosts}
    assert {host.host_id for host in live} <= hosts
    await service.shutdown()


# ---- capabilities ------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "fake", "hosting"),
    [
        ("on", True, True),
        ("on", False, False),
        ("auto", True, True),
        ("auto", False, False),
        ("off", True, False),
        ("off", False, False),
    ],
)
def test_capabilities_report_hosting_honestly(
    tmp_path: Path, mode: str, fake: bool, hosting: bool
) -> None:
    service, _ = make_service(
        tmp_path,
        build_backend(mode, fake),  # type: ignore[arg-type]
        mode=mode,
        fake=fake,
    )
    caps = service.capabilities(onlyoffice_enabled=False)

    assert caps.office_native == mode
    assert caps.native_hosting is hosting
    assert caps.fake_backend is (fake and hosting)
    assert caps.hostable_kinds == (list(HOSTABLE_KINDS) if hosting else [])
    # All three applications, PowerPoint included: under the fake backend there
    # is no real instance to collide with, so nothing is held back and
    # ``kind_notes`` is empty. The conditional half is its own test below.
    assert ("powerpoint" in caps.hostable_kinds) is hosting
    assert caps.kind_notes == {}
    assert caps.fallback == ("native" if hosting else "preview")
    assert caps.detail


def test_powerpoint_is_held_back_while_one_is_already_running(tmp_path: Path) -> None:
    """The conditional half of PowerPoint hosting, reported before it is hit.

    A real backend with a PowerPoint already up cannot host one: the launch would
    bind to the user's instance. So the kind leaves ``hostable_kinds`` and the
    reason arrives with it, which is what lets the UI open the preview directly
    instead of attempting a launch that is going to be refused.
    """
    service, _ = make_service(
        tmp_path, FakeHostBackend(), fake=False, running_kinds=frozenset({"powerpoint"})
    )

    caps = service.capabilities(onlyoffice_enabled=False)

    assert caps.native_hosting is True
    assert caps.hostable_kinds == ["word", "excel"]
    assert "powerpoint" in caps.kind_notes
    assert "already running" in caps.kind_notes["powerpoint"]
    # Word and Excel are unaffected: a running instance is irrelevant to them.
    assert set(caps.kind_notes) == {"powerpoint"}


def test_a_running_word_holds_nothing_back(tmp_path: Path) -> None:
    """Only the single-instance kinds can produce a note. ``DispatchEx`` gives
    Word and Excel their own process however many are open."""
    service, _ = make_service(
        tmp_path, FakeHostBackend(), fake=False, running_kinds=frozenset({"word", "excel"})
    )

    caps = service.capabilities(onlyoffice_enabled=False)

    assert caps.kind_notes == {}
    assert caps.hostable_kinds == ["word", "excel", "powerpoint"]


def test_the_fake_backend_never_consults_the_window_probe(tmp_path: Path) -> None:
    """CI runs the fake on machines with no Office and no window station, and the
    fake starts no processes — so there is no machine-wide instance to collide
    with and the probe is not consulted at all.

    Its own hosted decks are a different question, answered from the host map
    rather than from a probe, and the test below asserts that one.
    """
    service, _ = make_service(
        tmp_path, FakeHostBackend(), fake=True, running_kinds=frozenset({"powerpoint"})
    )

    caps = service.capabilities(onlyoffice_enabled=False)

    assert caps.kind_notes == {}
    assert "powerpoint" in caps.hostable_kinds


async def test_the_deck_workbench_is_hosting_holds_powerpoint_back(tmp_path: Path) -> None:
    """The collision a window probe structurally cannot see.

    Docking reparents the frame into a child window, so the top-level
    ``EnumWindows`` walk behind ``running_probe`` stops finding it: the probe here
    says nothing is running (``running_kinds`` is empty) *while Workbench is
    holding the one instance there is*. Answered from the host map instead — or
    the second deck a user clicks would be sent to a launch the pre-flight is
    going to refuse, with a card telling them to close a PowerPoint that has no
    window on their desktop.
    """
    service, _ = make_service(tmp_path, FakeHostBackend(), fake=False, running_kinds=frozenset())
    (tmp_path / "deck-one.pptx").write_bytes(DOCX_BYTES)

    assert "powerpoint" in service.capabilities(onlyoffice_enabled=False).hostable_kinds
    docked = await service.open("deck-one.pptx", RECT)
    assert docked.state == "embedded"

    caps = service.capabilities(onlyoffice_enabled=False)

    assert caps.hostable_kinds == ["word", "excel"]
    note = caps.kind_notes["powerpoint"]
    # The sentence names the tab to close, because that is the only window there
    # is — "close PowerPoint" would name nothing the user can find.
    assert "deck-one.pptx" in note
    assert "Close that tab" in note
    assert set(caps.kind_notes) == {"powerpoint"}

    # And the kind comes back the moment that deck settles: a terminal host holds
    # no process, so it must not hold the application either.
    await service.close(docked.host_id)
    assert service.capabilities(onlyoffice_enabled=False).kind_notes == {}
    assert "powerpoint" in service.capabilities(onlyoffice_enabled=False).hostable_kinds


async def test_a_second_deck_that_races_the_report_names_the_tab_to_close(
    tmp_path: Path,
) -> None:
    """The same collision when ``kind_notes`` was read a moment too early.

    The panel reads capabilities when it mounts, so a deck docked between that
    read and this launch still reaches the backend and is still refused. What
    must not happen is the *generic* refusal: the instance in the way is the one
    Workbench is hosting, so the reason — and the card it renders — has to name
    the tab, not a PowerPoint window that does not exist.
    """
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    (tmp_path / "deck-one.pptx").write_bytes(DOCX_BYTES)
    (tmp_path / "deck-two-app-running.pptx").write_bytes(DOCX_BYTES)

    first = await service.open("deck-one.pptx", RECT)
    assert first.state == "embedded"

    second = await service.open("deck-two-app-running.pptx", RECT)

    assert (second.state, second.reason) == ("failed", "powerpoint_hosted_here")
    # Nothing was closed to make room: the deck the user is looking at survives a
    # refusal aimed at a different one.
    assert service.get(first.host_id).state == "embedded"

    # With that deck gone the refusal is the other one again — the user really
    # does have a PowerPoint of their own in the way, and the fix is theirs.
    await service.close(first.host_id)
    third = await service.open("deck-two-app-running.pptx", RECT)
    assert (third.state, third.reason) == ("failed", "powerpoint_already_running")


async def test_a_deck_walks_the_whole_lifecycle(tmp_path: Path) -> None:
    """Dock, move, hide, detach, re-embed, close — the full fake-first walk, with
    no PowerPoint, no window and no Rust. This is what CI drives."""
    backend = FakeHostBackend()
    service, events = make_service(tmp_path, backend)
    (tmp_path / "deck.pptx").write_bytes(DOCX_BYTES)

    info = await service.open("deck.pptx", RECT)
    assert (info.kind, info.state) == ("powerpoint", "embedded")

    await service.set_bounds(info.host_id, MOVED)
    await service.set_visible(info.host_id, False)
    detached = await service.detach(info.host_id)
    assert detached.state == "detached"

    # Re-opening a detached deck re-embeds the window we already have rather than
    # launching a second PowerPoint — which for this kind is not merely wasteful,
    # it is impossible.
    again = await service.open("deck.pptx", RECT)
    assert (again.host_id, again.state) == (info.host_id, "embedded")

    closed = await service.close(info.host_id)
    assert (closed.state, closed.reason) == ("closed", "user_closed")
    assert closed.close_failed is False
    assert [call for call, _ in backend.calls] == [
        "launch",
        "embed",
        "set_bounds",
        "set_visible",
        "detach",
        "embed",
        # The re-embed re-applies the hidden state rather than assuming a fresh
        # window is visible: the panel was behind another tab when it detached,
        # and a real PowerPoint reappearing over the user's work is exactly the
        # bug `set_visible` exists to prevent.
        "set_visible",
        "close",
    ]
    assert backend.calls[-2] == ("set_visible", "hide")
    assert events.states()[-1] == "closed"


def test_auto_hosts_natively_only_where_it_actually_can(tmp_path: Path) -> None:
    """The owner decision, re-encoded now that hang isolation is proven.

    `auto` no longer means "never": it means "wherever the machine can". With no
    shell to host into — the browser tab this same server also serves — that is
    still nowhere, and the answer says which of the two it is.
    """
    # No channel: nothing the backend could send a `SetParent` to.
    assert build_backend("auto", fake=False) is None
    service, _ = make_service(tmp_path, None, mode="auto", fake=False, office_installed=True)
    caps = service.capabilities(onlyoffice_enabled=True)

    assert caps.native_hosting is False
    assert caps.shell_attached is False
    assert caps.office_detected is True
    assert caps.fallback == "onlyoffice"
    assert "desktop shell" in caps.detail


def test_auto_builds_the_real_backend_when_a_channel_exists() -> None:
    """The other half: on Windows with a channel, `auto` is the real thing."""
    backend = build_backend("auto", fake=False, channel=ShellChannel())
    if sys.platform != "win32":  # pragma: no cover - the CI matrix is Windows
        assert backend is None
        return
    assert isinstance(backend, ShellHostBackend)
    # …and it still refuses to claim it can host with nobody attached.
    assert backend.ready() is False


def test_off_beats_a_channel_and_a_working_machine() -> None:
    assert build_backend("off", fake=False, channel=ShellChannel()) is None


def test_capabilities_fall_back_to_onlyoffice_when_it_is_configured(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, None, mode="off", fake=False)
    assert service.capabilities(onlyoffice_enabled=True).fallback == "onlyoffice"
    assert service.capabilities(onlyoffice_enabled=False).fallback == "preview"


def test_the_fake_backend_says_so_in_the_capabilities(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, FakeHostBackend(), mode="on", fake=True)
    caps = service.capabilities(onlyoffice_enabled=False)
    assert caps.fake_backend is True
    assert "fake host backend" in caps.detail


def test_office_detection_is_a_filesystem_probe(tmp_path: Path) -> None:
    assert detect_office([tmp_path]) is False
    exe = tmp_path / "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert detect_office([tmp_path]) is True
    assert detect_office([tmp_path / "nope"]) is False


# ---- the fake backend itself -------------------------------------------------


@pytest.mark.parametrize(
    ("name", "failure"),
    [
        ("budget-fail-launch.xlsx", "launch_failed"),
        ("memo-hang-launch.docx", "launch_timeout"),
        ("memo-refuse-embed.docx", "embed_refused"),
        ("memo-crash-after-embed.docx", "crash_after_embed"),
        ("memo-already-open.docx", "already_open"),
        ("report.docx", None),
    ],
)
def test_failure_branches_are_chosen_by_the_document_name(
    name: str, failure: FakeFailure | None
) -> None:
    assert failure_for(Path(name)) == failure


async def test_a_document_named_for_a_branch_drives_it_end_to_end(tmp_path: Path) -> None:
    """The trigger path, not just the helper: this is what lets a future E2E
    reach a failure state from the UI with no special API."""
    service, _ = make_service(tmp_path, FakeHostBackend())
    info = await service.open(document(tmp_path, "memo-refuse-embed.docx"), RECT)
    assert (info.state, info.reason) == ("failed", "embed_refused")


async def test_the_fake_backend_starts_no_process(tmp_path: Path) -> None:
    """The pids it hands out are counters. Nothing here can be killed, and
    nothing survives the test — which is what makes this runnable in CI."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    info = await service.open(document(tmp_path), RECT)
    assert info.pid is not None
    assert info.pid >= FIRST_FAKE_PID
    assert await backend.poll(HostHandle(pid=info.pid, window_id=0)) == "alive"
    await service.close(info.host_id)
    assert await backend.poll(HostHandle(pid=info.pid, window_id=0)) == "gone"


# ---- integration: the real app ----------------------------------------------


def hosting_app(tmp_path: Path, **overrides: Any) -> Any:
    return create_app(
        Settings(
            workspace_root=tmp_path,
            claude_projects_dir=tmp_path / "projects",
            office_native="on",
            office_fake=True,
            **overrides,
        )
    )


def hosting_client(app: Any) -> AsyncClient:
    """The app over HTTP, without a portal in the way, so a test can have two
    requests genuinely in flight at once."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def read_host_frames(events: Any, until_state: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        frame: dict[str, Any] = json.loads(events.receive_text())
        if frame["type"] != "office_host":
            continue
        frames.append(frame)
        if frame["host"]["state"] == until_state:
            return frames


@pytest.mark.timeout(60)
def test_a_host_opened_over_http_arrives_on_the_events_socket(tmp_path: Path) -> None:
    """The whole seam, through the real app: REST in, typed frames out on the
    bus every other view already listens to, and `GET /api/office/hosts` telling
    a reconnecting client the same story."""
    document(tmp_path)
    with TestClient(hosting_app(tmp_path)) as client:
        with client.websocket_connect("/ws/events") as events:
            opened = client.post(
                "/api/office/host",
                json={
                    "path": "report.docx",
                    "rect": {"x": 10, "y": 20, "width": 640, "height": 480},
                },
            )
            assert opened.status_code == 200
            info = opened.json()
            assert info["state"] == "embedded"
            assert info["kind"] == "word"
            assert info["pid"] is not None

            frames = read_host_frames(events, "embedded")
            assert [f["host"]["state"] for f in frames] == ["launching", "embedding", "embedded"]
            assert {f["host"]["host_id"] for f in frames} == {info["host_id"]}

            listed = client.get("/api/office/hosts").json()["hosts"]
            assert listed == [info]

            moved = client.post(
                f"/api/office/host/{info['host_id']}/bounds",
                json={"rect": {"x": 0, "y": 0, "width": 1024, "height": 768}},
            )
            assert moved.status_code == 200

            closed = client.post(f"/api/office/host/{info['host_id']}/close").json()
            assert (closed["state"], closed["reason"]) == ("closed", "user_closed")
            assert read_host_frames(events, "closed")[-1]["host"]["reason"] == "user_closed"

        assert client.get("/api/office/hosts").json()["hosts"][0]["state"] == "closed"


def test_capabilities_over_http(tmp_path: Path) -> None:
    with TestClient(hosting_app(tmp_path)) as client:
        caps = client.get("/api/office/capabilities").json()
    assert caps["native_hosting"] is True
    assert caps["fake_backend"] is True
    assert caps["hostable_kinds"] == ["word", "excel", "powerpoint"]
    assert caps["kind_notes"] == {}
    assert caps["onlyoffice"] is False
    assert caps["fallback"] == "native"


def test_capabilities_on_a_default_machine_report_no_native_hosting(tmp_path: Path) -> None:
    """The shipped default: `auto`, no fake backend, so the UI degrades to the
    OnlyOffice path exactly as it does today."""
    with TestClient(create_app(Settings(workspace_root=tmp_path))) as client:
        caps = client.get("/api/office/capabilities").json()
        assert caps["office_native"] == "auto"
        assert caps["native_hosting"] is False
        assert caps["hostable_kinds"] == []
        assert client.post("/api/office/host", json={"path": "report.docx"}).status_code == 503
        # The browser path is untouched by any of this.
        assert client.get("/api/office/status").json()["enabled"] is False


def test_the_shell_channel_is_what_makes_a_shell_attached(tmp_path: Path) -> None:
    """The socket *is* the presence: there is no hello message and no
    heartbeat, so connecting and disconnecting is the whole protocol for
    "something here can host a window"."""
    with TestClient(hosting_app(tmp_path)) as client:
        assert client.get("/api/office/capabilities").json()["shell_attached"] is False
        with client.websocket_connect("/ws/office-host"):
            assert client.get("/api/office/capabilities").json()["shell_attached"] is True
        # The disconnect is seen as the endpoint's `finally` runs, which is not
        # synchronous with the context manager exiting on this side.
        for _ in range(50):
            if client.get("/api/office/capabilities").json()["shell_attached"] is False:
                break
            time.sleep(0.02)
        assert client.get("/api/office/capabilities").json()["shell_attached"] is False


def test_visibility_over_http(tmp_path: Path) -> None:
    document(tmp_path)
    with TestClient(hosting_app(tmp_path)) as client:
        host = client.post(
            "/api/office/host", json={"path": "report.docx", "rect": RECT.model_dump()}
        ).json()
        assert (
            client.post(
                f"/api/office/host/{host['host_id']}/visible", json={"visible": False}
            ).status_code
            == 200
        )
        assert (
            client.post("/api/office/host/host-nope/visible", json={"visible": True}).status_code
            == 404
        )
        client.post(f"/api/office/host/{host['host_id']}/close")
        # A settled host has no window to show or hide: a conflict, not a 500.
        assert (
            client.post(
                f"/api/office/host/{host['host_id']}/visible", json={"visible": True}
            ).status_code
            == 409
        )


def test_powerpoint_over_http_docks_like_the_others(tmp_path: Path) -> None:
    (tmp_path / "deck.pptx").write_bytes(DOCX_BYTES)
    with TestClient(hosting_app(tmp_path)) as client:
        host = client.post("/api/office/host", json={"path": "deck.pptx"}).json()
        assert (host["kind"], host["state"]) == ("powerpoint", "embedded")
        assert client.get("/api/office/hosts").json()["hosts"][0]["kind"] == "powerpoint"


def test_a_deck_refused_over_http_says_powerpoint_is_the_thing_in_use(tmp_path: Path) -> None:
    """A settled ``failed`` host, not an HTTP error: the launch was attempted and
    the backend refused it, which is a state the panel renders rather than an
    exception the client has to interpret.

    The status is asserted for the same reason the router's table no longer names
    this reason: an "in use" outcome is decided *after* the host record exists, so
    it can only come back as a 200 with the reason in the body. A status mapping
    for it would be one that never fires.
    """
    (tmp_path / "app-running.pptx").write_bytes(DOCX_BYTES)
    with TestClient(hosting_app(tmp_path)) as client:
        response = client.post("/api/office/host", json={"path": "app-running.pptx"})
        assert response.status_code == 200
        host = response.json()
        assert (host["state"], host["reason"]) == ("failed", "powerpoint_already_running")


def test_http_errors_for_the_paths_a_client_can_get_wrong(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    with TestClient(hosting_app(tmp_path)) as client:
        assert client.post("/api/office/host", json={"path": "notes.txt"}).status_code == 415
        assert client.post("/api/office/host", json={"path": "gone.docx"}).status_code == 404
        assert client.post("/api/office/host", json={"path": "../x.docx"}).status_code == 400
        assert client.post("/api/office/host/host-nope/close").status_code == 404
        assert (
            client.post(
                "/api/office/host/host-nope/bounds",
                json={"rect": {"x": 0, "y": 0, "width": 10, "height": 10}},
            ).status_code
            == 404
        )


@pytest.mark.timeout(60)
async def test_a_resize_during_the_launch_reaches_the_window_over_http(tmp_path: Path) -> None:
    """The same regression as the unit test above, driven the way the panel will
    drive it: `POST /host` on mount, `POST /bounds` on the first layout pass,
    the second landing while the first is still waiting on Word. Two real
    requests, overlapping, through the real app."""
    document(tmp_path)
    app = hosting_app(tmp_path)
    hosts: OfficeHostService = app.state.office_host
    backend = SlowLaunch()
    # The one seam a test has for this: the app picks its backend at build time.
    hosts._backend = backend

    async with hosting_client(app) as client:
        opening = asyncio.create_task(
            client.post("/api/office/host", json={"path": "report.docx", "rect": RECT.model_dump()})
        )
        await until(lambda: bool(hosts.snapshot().hosts), delay=0.005)
        host_id = hosts.snapshot().hosts[0].host_id

        moved = await client.post(
            f"/api/office/host/{host_id}/bounds", json={"rect": MOVED.model_dump()}
        )
        assert moved.status_code == 200
        assert moved.json()["state"] == "launching"  # nothing to move yet, only to remember

        backend.gate.set()
        opened = await opening

    assert opened.status_code == 200
    assert opened.json()["state"] == "embedded"
    assert ("embed", "1024x768") in backend.calls
    assert ("embed", "640x480") not in backend.calls


async def test_an_open_that_loses_the_reuse_race_is_a_conflict_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`open` re-embeds a host that is already live, so it can find that host
    settled under it and raise `HostStateError` — which every other host-state
    conflict in this router reports as 409 and this one used to let through as a
    bare 500. Raised at the service boundary: the reuse path has no await of its
    own to lose the race in yet, and the router's job is the mapping."""
    document(tmp_path)
    app = hosting_app(tmp_path)

    async def already_settled(*args: Any, **kwargs: Any) -> OfficeHostInfo:
        raise HostStateError("host-1", "closed", "move")

    monkeypatch.setattr(app.state.office_host, "open", already_settled)
    async with hosting_client(app) as client:
        response = await client.post("/api/office/host", json={"path": "report.docx"})

    assert response.status_code == 409
    assert "closed" in response.json()["detail"]


@pytest.mark.timeout(60)
def test_shutdown_closes_hosts_the_app_still_holds(tmp_path: Path) -> None:
    document(tmp_path)
    app = hosting_app(tmp_path)
    with TestClient(app) as client:
        host_id = client.post("/api/office/host", json={"path": "report.docx"}).json()["host_id"]
    # The lifespan has exited: the service reaped what it was holding.
    hosts = {host.host_id: host for host in app.state.office_host.snapshot().hosts}
    assert hosts[host_id].state == "closed"
    assert hosts[host_id].reason == "server_shutdown"
