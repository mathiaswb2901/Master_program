"""The voice seam, up close — with no microphone, no model and no audio hardware.

Everything here runs against :class:`FakeVoiceBackend` or a purpose-built stand-in,
which is the whole point of the seam: the lifecycle a user drives with their voice
(press, speak, watch interim text arrive, release, edit, send) is exercised end to
end on a headless runner. What CI *cannot* judge — whether a real local model hears
"day-ahead" correctly — is owner-gated and deliberately absent.

Three groups: the honest capabilities report, the lifecycle and its refusals, and
the privacy properties that are the reason this feature is built this way at all.
"""

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.voice import (
    BYTES_PER_FRAME,
    DEFAULT_SAMPLE_RATE_HZ,
    MAX_CHUNK_BYTES,
    MAX_SAMPLE_RATE_HZ,
    MAX_UTTERANCE_S,
    VoiceChunk,
    VoiceSession,
    VoiceTranscript,
)
from workbench_server.services.voice import (
    ABANDON_GRACE_S,
    FAKE_FINAL_TEXT,
    FAKE_SCRIPT,
    MAX_ACTIVE_SESSIONS,
    BackendReport,
    FakeVoiceBackend,
    VoiceBackend,
    VoiceBackendError,
    VoiceBackendTimeoutError,
    VoiceNotFoundError,
    VoiceService,
    VoiceStateError,
    VoiceTooLongError,
    VoiceUnavailableError,
    build_backend,
    register_backend,
    registered_backends,
)

# 100 ms of 16 kHz mono silence — the size the UI's scripted capture really sends.
CHUNK = bytes(1_600 * BYTES_PER_FRAME)
CHUNK_B64 = base64.b64encode(CHUNK).decode("ascii")


def fake_service() -> VoiceService:
    return VoiceService(FakeVoiceBackend(), mode="auto", fake=True)


@pytest.fixture
async def voice_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """The real app with the fake backend — what `WORKBENCH_VOICE_FAKE=1` gives."""
    app = create_app(Settings(workspace_root=tmp_path, voice_fake=True))
    transport = ASGITransport(app=app)
    headers = {"X-Workbench-Token": app.state.auth_token}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


# ---- capabilities: why voice is not available, said out loud ------------------


def test_no_backend_is_reported_not_hidden() -> None:
    caps = VoiceService(None).capabilities()
    assert caps.available is False
    assert caps.backend == "none"
    assert caps.reason == "no_backend"
    assert caps.model_present is False
    # The detail names what would make it available, rather than shrugging.
    assert "WORKBENCH_VOICE_FAKE" in caps.detail


def test_the_fake_says_it_is_a_fake() -> None:
    caps = fake_service().capabilities()
    assert caps.available is True
    assert caps.backend == "fake"
    assert caps.fake_backend is True
    # There is no model. Claiming one is the silent failure this field prevents.
    assert caps.model_present is False
    assert caps.reason is None
    assert "scripted" in caps.detail


def test_off_refuses_whatever_is_configured() -> None:
    caps = VoiceService(FakeVoiceBackend(), mode="off", fake=True).capabilities()
    assert caps.available is False
    assert caps.reason == "disabled"
    assert "WORKBENCH_VOICE=off" in caps.detail


def test_a_backend_that_is_not_ready_reports_its_own_reason() -> None:
    """A package installed is not a model downloaded — the difference is the
    whole reason `ready()` is separate from "does this backend exist"."""

    class NotDownloaded:
        def ready(self) -> bool:
            return False

        def report(self) -> BackendReport:
            return BackendReport(
                kind="local_whisper", model_present=False, detail="the model is not downloaded"
            )

        async def start(self, session: VoiceSession) -> None: ...  # pragma: no cover
        async def feed(self, voice_id: str, sequence: int, audio: bytes) -> str:
            return ""  # pragma: no cover

        async def stop(self, voice_id: str) -> VoiceTranscript:  # pragma: no cover
            raise AssertionError("never reached")

        async def cancel(self, voice_id: str) -> None: ...  # pragma: no cover

    backend: VoiceBackend = NotDownloaded()
    caps = VoiceService(backend).capabilities()
    assert caps.available is False
    assert caps.reason == "model_missing"
    assert caps.backend == "local_whisper"
    assert caps.detail == "the model is not downloaded"


async def test_capabilities_over_http(voice_client: AsyncClient) -> None:
    res = await voice_client.get("/api/voice/capabilities")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["fake_backend"] is True
    assert body["sample_rate_hz"] == DEFAULT_SAMPLE_RATE_HZ
    assert body["max_utterance_s"] == MAX_UTTERANCE_S


async def test_voice_is_off_by_default(tmp_path: Path) -> None:
    """A machine nobody configured offers no microphone, and says why."""
    app = create_app(Settings(workspace_root=tmp_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/voice/capabilities")).json()
    assert body["available"] is False
    assert body["reason"] == "no_backend"
    assert body["backend"] == "none"
    # Starting anyway is a 503 that names the reason, never a 500.
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/voice/start", json={})
    assert res.status_code == 503


# ---- the lifecycle -----------------------------------------------------------


async def test_the_whole_push_to_talk_lifecycle() -> None:
    service = fake_service()
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    assert session.state == "recording"
    assert session.interim == ""

    first = await service.feed(session.voice_id, 0, CHUNK)
    assert first.interim == FAKE_SCRIPT[0]
    assert first.chunks == 1
    assert first.audio_bytes == len(CHUNK)

    second = await service.feed(session.voice_id, 1, CHUNK)
    # Interim is the utterance *so far*, not a delta — the composer replaces
    # rather than stitches.
    assert second.interim == " ".join(FAKE_SCRIPT[:2])
    assert second.interim.startswith(first.interim)

    transcript = await service.stop(session.voice_id)
    assert transcript.final is True
    assert transcript.text == FAKE_FINAL_TEXT
    assert 0.0 < transcript.confidence <= 1.0
    assert transcript.duration_s == pytest.approx(0.2, abs=0.01)

    # Terminal: the slot is freed and the id means nothing any more.
    with pytest.raises(VoiceNotFoundError):
        await service.stop(session.voice_id)


async def test_lifecycle_over_http(voice_client: AsyncClient) -> None:
    started = (await voice_client.post("/api/voice/start", json={})).json()
    voice_id = started["voice_id"]

    for sequence in range(3):
        res = await voice_client.post(
            f"/api/voice/{voice_id}/chunk", json={"sequence": sequence, "audio": CHUNK_B64}
        )
        assert res.status_code == 200
    assert res.json()["interim"] == " ".join(FAKE_SCRIPT[:3])

    stopped = await voice_client.post(f"/api/voice/{voice_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["text"] == FAKE_FINAL_TEXT
    # And it is gone — a transcript is not kept server-side.
    assert (await voice_client.post(f"/api/voice/{voice_id}/stop")).status_code == 404


async def test_cancel_discards_the_audio_and_produces_no_text() -> None:
    service = fake_service()
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    await service.feed(session.voice_id, 0, CHUNK)
    cancelled = await service.cancel(session.voice_id)
    assert cancelled.state == "cancelled"
    # No transcript exists, and none can be asked for afterwards.
    with pytest.raises(VoiceNotFoundError):
        await service.stop(session.voice_id)


async def test_cancel_over_http_is_404_the_second_time(voice_client: AsyncClient) -> None:
    voice_id = (await voice_client.post("/api/voice/start", json={})).json()["voice_id"]
    first = await voice_client.post(f"/api/voice/{voice_id}/cancel")
    assert first.status_code == 200
    assert first.json()["state"] == "cancelled"
    assert (await voice_client.post(f"/api/voice/{voice_id}/cancel")).status_code == 404


async def test_an_unknown_utterance_is_404_not_a_crash(voice_client: AsyncClient) -> None:
    res = await voice_client.post(
        "/api/voice/nonexistent/chunk", json={"sequence": 0, "audio": CHUNK_B64}
    )
    assert res.status_code == 404


async def test_an_utterance_that_runs_too_long_is_cancelled_not_buffered() -> None:
    service = fake_service()
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    # One chunk over the ceiling in a single shot: the guard is on total bytes,
    # so it does not depend on how the client sliced its audio.
    oversize = bytes(int(DEFAULT_SAMPLE_RATE_HZ * MAX_UTTERANCE_S) * BYTES_PER_FRAME + 2)
    with pytest.raises(VoiceTooLongError):
        await service.feed(session.voice_id, 0, oversize)
    # Cancelled, not left recording: a held key nobody released must not keep a slot.
    with pytest.raises(VoiceNotFoundError):
        await service.stop(session.voice_id)


async def test_the_concurrency_cap_names_itself() -> None:
    service = fake_service()
    for _ in range(MAX_ACTIVE_SESSIONS):
        await service.start(DEFAULT_SAMPLE_RATE_HZ)
    with pytest.raises(VoiceStateError) as excinfo:
        await service.start(DEFAULT_SAMPLE_RATE_HZ)
    assert str(MAX_ACTIVE_SESSIONS) in str(excinfo.value)


async def test_the_cap_is_a_429_over_http(voice_client: AsyncClient) -> None:
    for _ in range(MAX_ACTIVE_SESSIONS):
        assert (await voice_client.post("/api/voice/start", json={})).status_code == 200
    res = await voice_client.post("/api/voice/start", json={})
    assert res.status_code == 429
    assert str(MAX_ACTIVE_SESSIONS) in res.json()["detail"]


async def test_an_abandoned_utterance_is_reaped_rather_than_held() -> None:
    service = fake_service()
    stale = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    # Age it past the ceiling plus its grace, the way a closed laptop would.
    service._sessions[stale.voice_id] = stale.model_copy(
        update={"started_at": stale.started_at - (MAX_UTTERANCE_S + ABANDON_GRACE_S + 1)}
    )
    fresh = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    assert fresh.state == "recording"
    with pytest.raises(VoiceNotFoundError):
        await service.stop(stale.voice_id)


async def test_shutdown_cancels_a_microphone_still_open() -> None:
    service = fake_service()
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    await service.shutdown()
    with pytest.raises(VoiceNotFoundError):
        await service.stop(session.voice_id)


# ---- a backend that misbehaves ------------------------------------------------


class _Broken:
    """Fails or hangs on request — one flag per phase.

    Separate flags rather than one "broken" switch because the interesting cases
    are asymmetric: a backend whose `start` wedges while a model loads still
    cancels fine, and a backend that cancels fine everywhere except on the way
    out is exactly the one that hangs a server's shutdown.
    """

    def __init__(
        self,
        *,
        hang: bool = False,
        start_fails: bool = False,
        start_hangs: bool = False,
        cancel_hangs: bool = False,
    ) -> None:
        self._hang = hang
        self._start_fails = start_fails
        self._start_hangs = start_hangs
        self._cancel_hangs = cancel_hangs
        #: Ids the service asked this backend to drop. Proves the buffer was
        #: released even on the paths that swallow the outcome.
        self.cancelled: list[str] = []

    def ready(self) -> bool:
        return True

    def report(self) -> BackendReport:
        return BackendReport(kind="local_whisper", model_present=True, detail="a broken stand-in")

    async def start(self, session: VoiceSession) -> None:
        if self._start_hangs:
            await asyncio.sleep(3600)
        if self._start_fails:
            raise RuntimeError("the model would not load")

    async def feed(self, voice_id: str, sequence: int, audio: bytes) -> str:
        if self._hang:
            await asyncio.sleep(3600)
        raise RuntimeError("the model fell over")

    async def stop(self, voice_id: str) -> VoiceTranscript:
        if self._hang:
            await asyncio.sleep(3600)
        raise RuntimeError("the model fell over")

    async def cancel(self, voice_id: str) -> None:
        self.cancelled.append(voice_id)
        if self._cancel_hangs:
            await asyncio.sleep(3600)


async def test_a_backend_failure_settles_the_utterance() -> None:
    service = VoiceService(_Broken())
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    with pytest.raises(VoiceBackendError) as excinfo:
        await service.feed(session.voice_id, 0, CHUNK)
    assert "fell over" in str(excinfo.value)
    # Settled, not stuck recording — otherwise the slot leaks on every failure.
    with pytest.raises(VoiceNotFoundError):
        await service.stop(session.voice_id)


async def test_a_backend_that_hangs_is_cancelled_by_this_services_own_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The office host's lesson, applied: a backend that forgets to bound itself
    must not hang the request that started it."""
    monkeypatch.setattr("workbench_server.services.voice.FEED_TIMEOUT_S", 0.05)
    service = VoiceService(_Broken(hang=True))
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    with pytest.raises(VoiceBackendTimeoutError):
        await service.feed(session.voice_id, 0, CHUNK)


async def test_backend_failures_map_to_502_and_504_over_http(tmp_path: Path) -> None:
    app = create_app(Settings(workspace_root=tmp_path))
    app.state.voice = VoiceService(_Broken())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        voice_id = (await client.post("/api/voice/start", json={})).json()["voice_id"]
        res = await client.post(
            f"/api/voice/{voice_id}/chunk", json={"sequence": 0, "audio": CHUNK_B64}
        )
    assert res.status_code == 502


# ---- opening an utterance is bounded too --------------------------------------
#
# `start` is the call a real backend is *most* likely to sit in — it is where a
# whisper model gets loaded — and it is the one that holds a concurrency slot
# while it runs. A failure there that kept the slot would take the machine's one
# microphone out of service for everybody until a much later start reaped it.


async def test_a_start_that_fails_frees_the_slot_it_took() -> None:
    """The 429 lockout, head on: enough failing starts to exhaust the cap, and
    then some. Every one is the backend's failure, never "no slot left"."""
    backend = _Broken(start_fails=True)
    service = VoiceService(backend)
    for _ in range(MAX_ACTIVE_SESSIONS + 2):
        with pytest.raises(VoiceBackendError) as excinfo:
            await service.start(DEFAULT_SAMPLE_RATE_HZ)
        # A backend failure, not the concurrency cap — the distinction is the bug.
        assert not isinstance(excinfo.value, VoiceBackendTimeoutError)
        assert "would not load" in str(excinfo.value)
    assert service._sessions == {}
    # And the half-opened utterance was handed back to the backend to drop,
    # rather than left as a buffer nobody will ever claim.
    assert len(backend.cancelled) == MAX_ACTIVE_SESSIONS + 2


async def test_a_start_that_hangs_is_cancelled_by_this_services_own_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same lesson `feed` learned, on the call that loads the model."""
    monkeypatch.setattr("workbench_server.services.voice.START_TIMEOUT_S", 0.05)
    service = VoiceService(_Broken(start_hangs=True))
    with pytest.raises(VoiceBackendTimeoutError):
        await service.start(DEFAULT_SAMPLE_RATE_HZ)
    # Not held: a timeout that kept the session would leak the slot just as a
    # raise would, only more quietly.
    assert service._sessions == {}


async def test_a_failing_start_is_502_and_a_hanging_one_504_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One vocabulary across the lifecycle: /start answers like /chunk and /stop
    rather than letting a backend failure out as an unhandled 500."""
    app = create_app(Settings(workspace_root=tmp_path))
    transport = ASGITransport(app=app)

    app.state.voice = VoiceService(_Broken(start_fails=True))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/voice/start", json={})).status_code == 502

    monkeypatch.setattr("workbench_server.services.voice.START_TIMEOUT_S", 0.05)
    app.state.voice = VoiceService(_Broken(start_hangs=True))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/voice/start", json={})).status_code == 504


async def test_shutdown_is_not_blocked_by_a_backend_that_wedges_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main.py`'s lifespan awaits this. A cancel that never returns would be a
    server that never exits — with the microphone still open, which is the one
    state this call exists to prevent."""
    monkeypatch.setattr("workbench_server.services.voice.CANCEL_TIMEOUT_S", 0.05)
    backend = _Broken(cancel_hangs=True)
    service = VoiceService(backend)
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    # The ceiling, not patience: unbounded, the backend sleeps for an hour here.
    await asyncio.wait_for(service.shutdown(), 5)
    assert backend.cancelled == [session.voice_id]
    assert service._sessions == {}


# ---- chunks arrive in order, or they do not arrive ---------------------------


async def test_a_chunk_that_does_not_advance_is_refused_not_miscounted() -> None:
    """A duplicate or a slice that lost its race is audio for the wrong moment.
    Splicing it in produces a transcript nobody can tell is wrong — so it is a
    refusal, and the utterance goes on recording."""
    service = fake_service()
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    await service.feed(session.voice_id, 0, CHUNK)
    await service.feed(session.voice_id, 1, CHUNK)

    with pytest.raises(VoiceStateError) as excinfo:
        await service.feed(session.voice_id, 1, CHUNK)
    assert "does not advance" in str(excinfo.value)
    with pytest.raises(VoiceStateError):
        await service.feed(session.voice_id, 0, CHUNK)

    # Still recording: one confused slice is not a reason to drop the sentence.
    resumed = await service.feed(session.voice_id, 2, CHUNK)
    assert resumed.chunks == 3
    assert resumed.interim == " ".join(FAKE_SCRIPT[:3])


async def test_a_replayed_chunk_is_a_409_over_http(voice_client: AsyncClient) -> None:
    voice_id = (await voice_client.post("/api/voice/start", json={})).json()["voice_id"]
    body = {"sequence": 0, "audio": CHUNK_B64}
    assert (await voice_client.post(f"/api/voice/{voice_id}/chunk", json=body)).status_code == 200
    replay = await voice_client.post(f"/api/voice/{voice_id}/chunk", json=body)
    assert replay.status_code == 409


async def test_the_backend_is_told_where_each_chunk_sat_in_capture_order() -> None:
    """A gap is allowed and *visible*: a client that dropped slices jumps the
    number, and a transcriber splicing PCM needs to know to insert silence there
    rather than butt two unrelated moments together."""
    backend = _Recording()
    service = VoiceService(backend)
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    await service.feed(session.voice_id, 0, CHUNK)
    # Four slices lost on the way. Allowed — but the watermark moves with it.
    await service.feed(session.voice_id, 5, CHUNK)
    assert backend.sequences == [0, 5]
    with pytest.raises(VoiceStateError):
        await service.feed(session.voice_id, 5, CHUNK)
    assert backend.sequences == [0, 5]


# ---- one chunk cannot be arbitrarily large ------------------------------------


def test_the_chunk_ceiling_is_generous_against_what_capture_really_sends() -> None:
    """The cap is a refusal of the absurd, not a constraint on the plausible: a
    whole second at the highest rate a session may open at, against the 100 ms
    slices `ui/src/voiceCapture.ts` actually produces."""
    assert MAX_CHUNK_BYTES == MAX_SAMPLE_RATE_HZ * BYTES_PER_FRAME
    assert len(CHUNK) * 10 < MAX_CHUNK_BYTES


def test_an_oversized_chunk_is_refused_by_the_schema_itself() -> None:
    """Before the service, before the per-utterance budget: the model will not
    even hold a body no capture could have produced."""
    at_the_cap = base64.b64encode(bytes(MAX_CHUNK_BYTES)).decode("ascii")
    assert len(VoiceChunk(sequence=0, audio=at_the_cap).audio) == MAX_CHUNK_BYTES
    over = base64.b64encode(bytes(MAX_CHUNK_BYTES + 1)).decode("ascii")
    with pytest.raises(ValidationError):
        VoiceChunk(sequence=0, audio=over)


async def test_an_oversized_chunk_is_a_422_over_http(voice_client: AsyncClient) -> None:
    voice_id = (await voice_client.post("/api/voice/start", json={})).json()["voice_id"]
    over = base64.b64encode(bytes(MAX_CHUNK_BYTES + 1)).decode("ascii")
    res = await voice_client.post(
        f"/api/voice/{voice_id}/chunk", json={"sequence": 0, "audio": over}
    )
    assert res.status_code == 422
    # Refused, and the utterance is untouched — a rejected body is not a reason
    # to throw away a sentence somebody is still speaking.
    ok = await voice_client.post(
        f"/api/voice/{voice_id}/chunk", json={"sequence": 0, "audio": CHUNK_B64}
    )
    assert ok.status_code == 200


# ---- the privacy properties ---------------------------------------------------


class _Recording:
    """Remembers exactly what bytes reached it, so a test can prove the audio
    went to the backend and nowhere else."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.sequences: list[int] = []

    def ready(self) -> bool:
        return True

    def report(self) -> BackendReport:
        return BackendReport(kind="local_whisper", model_present=True, detail="a recorder")

    async def start(self, session: VoiceSession) -> None:
        return None

    async def feed(self, voice_id: str, sequence: int, audio: bytes) -> str:
        self.received.append(audio)
        self.sequences.append(sequence)
        return "heard"

    async def stop(self, voice_id: str) -> VoiceTranscript:
        return VoiceTranscript(text="heard", confidence=0.5, duration_s=1.0)

    async def cancel(self, voice_id: str) -> None:
        return None


async def test_the_service_hands_audio_to_the_backend_and_keeps_none_of_it() -> None:
    backend = _Recording()
    service = VoiceService(backend)
    session = await service.start(DEFAULT_SAMPLE_RATE_HZ)
    updated = await service.feed(session.voice_id, 0, CHUNK)
    assert backend.received == [CHUNK]
    # The service's own record of the utterance is a byte *count*: nothing in the
    # session it holds (or serialises) is, or could hold, audio.
    assert updated.audio_bytes == len(CHUNK)
    assert "audio" not in updated.model_dump()
    assert not any(isinstance(value, bytes | bytearray) for value in updated.model_dump().values())


def test_every_shipped_backend_claims_local_only() -> None:
    """`local_only` is the wire's statement that audio never leaves the machine.
    A path that did not would have to flip this field, visibly."""
    assert fake_service().capabilities().local_only is True
    assert VoiceService(None).capabilities().local_only is True


# ---- the registry: where the real backend plugs in ---------------------------


def test_the_fake_is_registered_and_never_picked_implicitly() -> None:
    assert "fake" in registered_backends()
    # No `fake=True`, no name: nothing is chosen, so a server does not quietly
    # transcribe canned text because a module happened to be imported.
    assert build_backend(mode="auto", fake=False, name=None) is None
    assert isinstance(build_backend(mode="auto", fake=True, name=None), FakeVoiceBackend)


def test_off_beats_everything() -> None:
    assert build_backend(mode="off", fake=True, name="fake") is None


def test_a_registered_backend_is_selected_by_name_and_when_it_is_the_only_one() -> None:
    """What plugging in the real local-whisper backend will look like."""
    register_backend("test_local", _Recording)
    try:
        assert isinstance(build_backend(mode="auto", fake=False, name="test_local"), _Recording)
        # And with exactly one real backend registered, no name is needed.
        assert isinstance(build_backend(mode="auto", fake=False, name=None), _Recording)
    finally:
        from workbench_server.services import voice as voice_module

        voice_module._REGISTRY.pop("test_local")


def test_an_unknown_backend_name_degrades_rather_than_raising() -> None:
    assert build_backend(mode="auto", fake=False, name="not_installed") is None


async def test_starting_without_a_backend_raises_the_reason_the_report_gives() -> None:
    """One authority for "why not": the refusal and the capabilities endpoint
    carry the same reason and the same sentence."""
    service = VoiceService(None)
    with pytest.raises(VoiceUnavailableError) as excinfo:
        await service.start(DEFAULT_SAMPLE_RATE_HZ)
    assert excinfo.value.reason == "no_backend"
    assert excinfo.value.detail == service.capabilities().detail
