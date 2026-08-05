"""The Office host domain layer, up close.

Everything here runs with no Microsoft Office, no Rust and no window manager —
that is the point of the seam. The fake backend walks the same lifecycle the
Win32 one will, so the states a user can actually get stuck in (the launch that
never produces a window, the embed that is refused, the application that dies
under us, the close that lands mid-embed) are reachable and asserted in CI.

The two owner decisions of 2026-08-05 are tested as rules, not as comments:
PowerPoint is refused, and a process we did not launch is never adopted.
"""

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
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
from workbench_server.services.office_host.backend import HostHandle
from workbench_server.services.office_host.fake_backend import (
    FIRST_FAKE_PID,
    FakeFailure,
    FakeHostBackend,
    failure_for,
)
from workbench_server.services.office_host.service import (
    DEFAULT_RECT,
    MAX_HOSTS,
    HostNotFoundError,
    HostRefusedError,
    HostStateError,
    OfficeHostService,
    build_backend,
    detect_office,
)
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
    poll_interval_s: float = 60.0,
) -> tuple[OfficeHostService, RecordingBus]:
    """A service on a real workspace with a scripted backend.

    ``office_installed`` is injected rather than probed: the suite must give the
    same answer on the author's machine (which has Office) and on CI (which does
    not).
    """
    bus = RecordingBus()
    service = OfficeHostService(
        Workspace(root),
        bus,
        backend,
        mode=mode,  # type: ignore[arg-type]
        fake=fake,
        detector=lambda: office_installed,
        poll_interval_s=poll_interval_s,
    )
    return service, bus


async def until(predicate: Callable[[], bool], tries: int = 500) -> None:
    """Yield to the loop until ``predicate`` holds. No sleeps: everything the
    fake backend does is in-process, so a scheduling turn is all it needs."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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

    class SlowLaunch(FakeHostBackend):
        def __init__(self) -> None:
            super().__init__()
            self.gate = asyncio.Event()

        async def launch(self, path: Path, kind: Any) -> HostHandle:
            await self.gate.wait()
            return await super().launch(path, kind)

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


async def test_powerpoint_is_refused_with_a_reason(tmp_path: Path) -> None:
    """Preview-only in v1: PowerPoint is single-instance and exposes no window
    handle to prove ownership with, so hosting it risks reparenting the user's
    own open presentation."""
    backend = FakeHostBackend()
    service, _ = make_service(tmp_path, backend)
    (tmp_path / "deck.pptx").write_bytes(DOCX_BYTES)

    with pytest.raises(HostRefusedError) as raised:
        await service.open("deck.pptx", RECT)

    assert raised.value.reason == "powerpoint_preview_only"
    assert host_app_kind("deck.pptx") == "powerpoint"  # the type is known...
    assert "powerpoint" not in HOSTABLE_KINDS  # ...it is simply not hosted
    assert backend.calls == []  # nothing was launched to find out
    assert service.snapshot().hosts == []  # and no phantom host is left behind


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
    assert "powerpoint" not in caps.hostable_kinds
    assert caps.fallback == ("native" if hosting else "preview")
    assert caps.detail


def test_auto_does_not_host_natively_today(tmp_path: Path) -> None:
    """The owner decision, encoded: native hosting becomes the default only once
    hang isolation is proven, so `auto` on a real machine hosts nothing."""
    assert build_backend("auto", fake=False) is None
    service, _ = make_service(tmp_path, None, mode="auto", fake=False, office_installed=True)
    caps = service.capabilities(onlyoffice_enabled=True)

    assert caps.native_hosting is False
    assert caps.office_detected is True  # Office is here; we still do not host it
    assert caps.fallback == "onlyoffice"
    assert "hang isolation" in caps.detail


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
    assert caps["hostable_kinds"] == ["word", "excel"]
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


def test_powerpoint_over_http_is_a_refusal_with_a_message(tmp_path: Path) -> None:
    (tmp_path / "deck.pptx").write_bytes(DOCX_BYTES)
    with TestClient(hosting_app(tmp_path)) as client:
        response = client.post("/api/office/host", json={"path": "deck.pptx"})
        assert response.status_code == 409
        assert "preview-only" in response.json()["detail"]
        assert client.get("/api/office/hosts").json()["hosts"] == []


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
def test_shutdown_closes_hosts_the_app_still_holds(tmp_path: Path) -> None:
    document(tmp_path)
    app = hosting_app(tmp_path)
    with TestClient(app) as client:
        host_id = client.post("/api/office/host", json={"path": "report.docx"}).json()["host_id"]
    # The lifespan has exited: the service reaped what it was holding.
    hosts = {host.host_id: host for host in app.state.office_host.snapshot().hosts}
    assert hosts[host_id].state == "closed"
    assert hosts[host_id].reason == "server_shutdown"
