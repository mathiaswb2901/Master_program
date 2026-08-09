"""Voice input: the backend seam, the scripted stand-in, and the lifecycle above them.

This module is the whole server half of M7 plan §3, and it ships **the seam, not
the voice**. Three things live here:

1. :class:`VoiceBackend` — the contract a transcriber must satisfy. Small,
   honest, and every method something a local model can really do.
2. :class:`FakeVoiceBackend` — the in-process stand-in behind
   ``WORKBENCH_VOICE_FAKE=1``. It walks the entire lifecycle (start → chunks →
   interim → final) deterministically, with **no microphone, no model and no
   audio hardware anywhere**, which is what makes the push-to-talk journey a
   headless-CI test rather than something only the owner can try.
3. :class:`VoiceService` — the lifecycle, the bounds, and the honest
   capabilities report the UI degrades from.

**Where the real backend plugs in.** :func:`register_backend` is the named
registry point. A local-whisper implementation is one module that does::

    class LocalWhisperBackend:          # satisfies VoiceBackend structurally
        def ready(self) -> bool: ...
        def report(self) -> BackendReport: ...
        async def start(self, session: VoiceSession) -> None: ...
        async def feed(self, voice_id: str, audio: bytes) -> str: ...
        async def stop(self, voice_id: str) -> VoiceTranscript: ...
        async def cancel(self, voice_id: str) -> None: ...

    register_backend("local_whisper", LocalWhisperBackend)

and then ``WORKBENCH_VOICE_BACKEND=local_whisper`` (or simply being the only
registered backend) selects it. Nothing above this line changes: not the wire
types, not the router, not the composer. That backend is **owner-gated** — it
brings a new runtime dependency and a large model download — and this PR
deliberately does not write it.

**The privacy posture, restated where the code is.** Every backend that will
ever be registered here transcribes on this machine. Audio is handed to
:meth:`VoiceBackend.feed` and is not written to disk, not logged, and not sent
anywhere by this service; the service itself keeps **no audio at all**, only a
byte count, so the buffer that exists is the backend's and its lifetime is the
utterance. A cloud transcriber is not "a backend someone could register" — it
would have to flip :attr:`VoiceCapabilities.local_only` on the wire, which is
exactly the visible change the field exists to force.

**Where the domain vocabulary goes.** The plan wants MW/MWh, EUR/MWh, gate
closure, day-ahead and the asset names biasing the transcriber. That is an
*initial prompt to the local model*, so it belongs inside the real backend's
:meth:`~VoiceBackend.start`, where the model handle is — not on the wire, and
not in this service. It is owner-gated with the model itself (it can only be
tuned against real speech) and nothing here fabricates one.
"""

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from workbench_server.models.voice import (
    BYTES_PER_FRAME,
    MAX_UTTERANCE_S,
    VoiceBackendKind,
    VoiceCapabilities,
    VoiceMode,
    VoiceSession,
    VoiceTranscript,
    VoiceUnavailableReason,
)

log = structlog.get_logger()

#: How many utterances may be in flight at once on one server.
#:
#: A machine has one microphone, but two windows on the same server can each
#: hold a capture open, and a client that crashes mid-utterance leaves one
#: behind. The cap is what turns "a leak" into "a refusal that names the cap".
MAX_ACTIVE_SESSIONS = 4

#: Grace beyond :data:`MAX_UTTERANCE_S` after which a still-recording session is
#: assumed abandoned and reaped. A held key that is never released — a crashed
#: tab, a closed laptop — must not hold a slot forever.
ABANDON_GRACE_S = 30.0

#: Ceilings this service applies to a backend call, whatever the backend thinks.
#: The office host learned this one the expensive way: an implementation that
#: forgets to bound itself otherwise hangs the request that started it. ``feed``
#: is on the interactive path and gets a short one; ``stop`` may really be
#: running a model over a two-minute utterance and gets a long one.
FEED_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 90.0


# ---- the seam ----------------------------------------------------------------


@dataclass(frozen=True)
class BackendReport:
    """What a backend says about itself, for the capabilities endpoint.

    Separate from :class:`VoiceCapabilities` because the *policy* half (is voice
    switched off? is the fake answering?) belongs to the service, and a backend
    reporting on policy it does not own is how two authorities for one fact
    appear.
    """

    kind: VoiceBackendKind
    #: A local model is present on this machine and can transcribe now.
    model_present: bool
    #: One line for the UI and the logs, in this backend's own words — e.g.
    #: "faster-whisper small.en, on this machine" or "the model is not
    #: downloaded (run: …)".
    detail: str


class VoiceBackend(Protocol):
    """What a transcriber must provide, and nothing more.

    Every method is ``async`` because none of it is cheap: a real model runs on
    a worker thread and a two-minute utterance is seconds of work. Making that
    explicit here keeps the service written for the real cost from the start.

    **Every method must come back.** A backend is expected to bound its own
    work; the service does not trust that and applies its own ceiling
    (:data:`FEED_TIMEOUT_S`, :data:`STOP_TIMEOUT_S`), cancelling the coroutine
    when it runs out. A backend must therefore treat cancellation as a real
    outcome and leave nothing running behind it.
    """

    def ready(self) -> bool:
        """Can this backend transcribe *right now*?

        Not the same question as "does this backend exist": a local model that
        has not been downloaded yet is a backend that exists and is not ready,
        and the difference is the whole point of the capabilities endpoint.
        """
        ...

    def report(self) -> BackendReport:
        """Name yourself, honestly, for the capabilities report."""
        ...

    async def start(self, session: VoiceSession) -> None:
        """Begin one utterance. The domain-vocabulary initial prompt goes here."""
        ...

    async def feed(self, voice_id: str, audio: bytes) -> str:
        """Ingest one chunk and return the interim transcript **so far**.

        The whole utterance as heard to this point, not the delta — a composer
        that has to stitch deltas together is a composer that gets the stitching
        wrong. ``""`` is the honest answer while there is not yet enough audio
        to say anything, and it is not an error.
        """
        ...

    async def stop(self, voice_id: str) -> VoiceTranscript:
        """Finish the utterance and return the final transcript."""
        ...

    async def cancel(self, voice_id: str) -> None:
        """Abandon the utterance and discard its audio. Never produces text."""
        ...


class VoiceBackendError(Exception):
    """The backend refused or gave up. A 502 to the caller, never a crash."""


class VoiceBackendTimeoutError(VoiceBackendError):
    """The backend ran past this service's ceiling and was cancelled."""


# ---- the registry ------------------------------------------------------------

#: Name -> how to build it. **This is the named plug-in point**: a real backend
#: lands as one module that calls :func:`register_backend` at import time, and
#: nothing else in the tree changes.
_REGISTRY: dict[str, Callable[[], VoiceBackend]] = {}


def register_backend(name: str, factory: Callable[[], VoiceBackend]) -> None:
    """Register a backend under a name ``WORKBENCH_VOICE_BACKEND`` can select.

    Idempotent by replacement, so re-importing a module in a test suite is not a
    failure; a *different* implementation taking a live name is a real change
    and is logged.
    """
    if name in _REGISTRY:
        log.info("voice.backend_replaced", backend=name)
    _REGISTRY[name] = factory


def registered_backends() -> tuple[str, ...]:
    """Every registered backend name, sorted. The honest list for a log line."""
    return tuple(sorted(_REGISTRY))


def build_backend(*, mode: VoiceMode, fake: bool, name: str | None) -> VoiceBackend | None:
    """Pick the backend this server will use, or None when there is none.

    None is not an error — it is the common case on a machine where nobody has
    installed a model, and :meth:`VoiceService.capabilities` reports it as
    ``no_backend`` so the UI can say why the microphone is not offered.
    """
    if mode == "off":
        return None
    if fake:
        return _REGISTRY["fake"]()
    if name is not None:
        factory = _REGISTRY.get(name)
        if factory is None:
            log.warning("voice.backend_unknown", requested=name, registered=registered_backends())
            return None
        return factory()
    # No explicit choice: use the one real backend if there is exactly one. The
    # fake is never picked implicitly — a server transcribing canned text
    # because a module happened to be imported would be the worst kind of quiet.
    real = [key for key in _REGISTRY if key != "fake"]
    if len(real) == 1:
        return _REGISTRY[real[0]]()
    return None


# ---- the fake ----------------------------------------------------------------

#: What the fake "hears", one word per chunk.
#:
#: Deliberately a sentence from this app's own domain rather than "hello world":
#: the words a real local model will need biasing toward are exactly these
#: (day-ahead, spread, MWh), so a screenshot of the fake is a screenshot of what
#: the feature is for. Nothing about the audio is inspected — the fake counts
#: chunks, which is what makes the journey deterministic.
FAKE_SCRIPT: tuple[str, ...] = (
    "summarise",
    "the",
    "day-ahead",
    "spread",
    "for",
    "tomorrow",
)

#: The final transcript the fake always produces, however few chunks arrived.
#: Deterministic on purpose: a journey that pressed the key briefly and one that
#: held it must assert the same string.
FAKE_FINAL_TEXT = " ".join(FAKE_SCRIPT)

#: The fake's fixed confidence. High, but not 1.0 — a transcriber that claims
#: certainty is a transcriber whose confidence field means nothing.
FAKE_CONFIDENCE = 0.98

#: What the fake assumes one chunk holds, for its duration arithmetic only.
#: 100 ms at 16 kHz — the size the UI's scripted capture really sends.
FAKE_FRAMES_PER_CHUNK = 1_600


class FakeVoiceBackend:
    """The scripted stand-in (``WORKBENCH_VOICE_FAKE=1``).

    The counterpart of ``services/fake_agent.py`` and
    ``services/office_host/fake_backend.py``: the same lifecycle, deterministic,
    and **nothing real anywhere**. No microphone is opened, no model is loaded,
    no audio is decoded — the bytes handed to :meth:`feed` are counted and
    dropped, and the words come from :data:`FAKE_SCRIPT`.

    Never enabled by default, and ``main.py`` logs a warning on startup when it
    is: a composer that looks like it is listening while the words are canned
    would be a worse lie than a composer with no microphone button at all.
    """

    def __init__(self) -> None:
        #: voice_id -> chunks fed. The whole state a scripted transcriber needs.
        self._heard: dict[str, int] = {}
        self._rates: dict[str, int] = {}

    def ready(self) -> bool:
        return True

    def report(self) -> BackendReport:
        return BackendReport(
            kind="fake",
            # There is no model. Saying otherwise is the exact silent failure
            # `model_present` exists to prevent.
            model_present=False,
            detail="the fake voice backend is active: the words are scripted, nothing is heard",
        )

    async def start(self, session: VoiceSession) -> None:
        self._heard[session.voice_id] = 0
        self._rates[session.voice_id] = session.sample_rate_hz

    async def feed(self, voice_id: str, audio: bytes) -> str:
        # `audio` is counted and dropped. Read once so a caller passing an empty
        # chunk does not silently advance the script.
        if not audio:
            return self._interim(voice_id)
        self._heard[voice_id] = self._heard.get(voice_id, 0) + 1
        return self._interim(voice_id)

    def _interim(self, voice_id: str) -> str:
        words = min(self._heard.get(voice_id, 0), len(FAKE_SCRIPT))
        return " ".join(FAKE_SCRIPT[:words])

    async def stop(self, voice_id: str) -> VoiceTranscript:
        chunks = self._heard.pop(voice_id, 0)
        rate = self._rates.pop(voice_id, 0)
        # Duration from what was actually fed, so a journey that asserts it is
        # asserting something the lifecycle produced rather than a constant.
        frames = chunks * FAKE_FRAMES_PER_CHUNK
        duration = frames / rate if rate > 0 else 0.0
        return VoiceTranscript(
            text=FAKE_FINAL_TEXT,
            confidence=FAKE_CONFIDENCE,
            duration_s=duration,
            final=True,
        )

    async def cancel(self, voice_id: str) -> None:
        self._heard.pop(voice_id, None)
        self._rates.pop(voice_id, None)


register_backend("fake", FakeVoiceBackend)


# ---- the service -------------------------------------------------------------


class VoiceUnavailableError(Exception):
    """Voice cannot start here. Carries the reason the capabilities report gives."""

    def __init__(self, reason: VoiceUnavailableReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class VoiceNotFoundError(Exception):
    """No such utterance — it was never started, or it has already settled."""


class VoiceStateError(Exception):
    """The utterance is not in a state where this makes sense (a chunk after stop)."""


class VoiceTooLongError(Exception):
    """The utterance ran past :data:`~workbench_server.models.voice.MAX_UTTERANCE_S`."""


class VoiceService:
    """The push-to-talk lifecycle: bounded, honest, and backend-agnostic.

    Holds **no audio**. Chunks are handed straight to the backend and only their
    size is remembered, which is both the privacy posture (nothing is buffered
    here to leak or to log) and the memory bound (a held key costs a counter).
    """

    def __init__(
        self,
        backend: VoiceBackend | None,
        *,
        mode: VoiceMode = "auto",
        fake: bool = False,
    ) -> None:
        self._backend = backend
        self._mode: VoiceMode = mode
        self._fake = fake
        self._sessions: dict[str, VoiceSession] = {}
        #: Serialises the bookkeeping, not the backend calls: two chunks of the
        #: *same* utterance are ordered by the client, and two different
        #: utterances must not be able to interleave a start past the cap.
        self._lock = asyncio.Lock()

    # ---- capabilities -------------------------------------------------------

    @property
    def available(self) -> bool:
        """Can an utterance start right now — policy AND a ready backend."""
        return self._mode != "off" and self._backend is not None and self._backend.ready()

    def capabilities(self) -> VoiceCapabilities:
        """What this machine can actually do with a microphone, said plainly."""
        report = self._backend.report() if self._backend is not None else None
        reason, detail = self._verdict(report)
        return VoiceCapabilities(
            available=self.available,
            backend=report.kind if report is not None else "none",
            mode=self._mode,
            fake_backend=self._fake and self._backend is not None,
            model_present=report.model_present if report is not None else False,
            # True for every backend this repo ships (see the module docstring).
            local_only=True,
            reason=reason,
            detail=detail,
        )

    def _verdict(self, report: BackendReport | None) -> tuple[VoiceUnavailableReason | None, str]:
        if self._mode == "off":
            return "disabled", "voice input is off (WORKBENCH_VOICE=off)"
        if self._backend is None or report is None:
            return (
                "no_backend",
                "no voice backend is configured — voice input needs a local "
                "transcriber on this machine (WORKBENCH_VOICE_FAKE=1 walks the "
                "lifecycle with scripted text)",
            )
        if not self._backend.ready():
            return "model_missing", report.detail
        return None, report.detail

    # ---- the lifecycle ------------------------------------------------------

    async def start(self, sample_rate_hz: int) -> VoiceSession:
        """Begin one push-to-talk utterance."""
        backend = self._backend
        if backend is None or self._mode == "off" or not backend.ready():
            reason, detail = self._verdict(None if backend is None else backend.report())
            raise VoiceUnavailableError(reason or "no_backend", detail)
        async with self._lock:
            self._reap()
            if len(self._sessions) >= MAX_ACTIVE_SESSIONS:
                raise VoiceStateError(
                    f"{MAX_ACTIVE_SESSIONS} utterances are already in flight — "
                    "release or cancel one before starting another"
                )
            session = VoiceSession(
                voice_id=uuid.uuid4().hex[:12],
                state="recording",
                started_at=time.time(),
                sample_rate_hz=sample_rate_hz,
            )
            self._sessions[session.voice_id] = session
        await backend.start(session)
        log.info("voice.started", voice_id=session.voice_id, sample_rate_hz=sample_rate_hz)
        return session

    async def feed(self, voice_id: str, sequence: int, audio: bytes) -> VoiceSession:
        """Ingest one chunk and answer with the utterance's interim text."""
        session = self._recording(voice_id)
        budget = int(session.sample_rate_hz * MAX_UTTERANCE_S) * BYTES_PER_FRAME
        if session.audio_bytes + len(audio) > budget:
            await self.cancel(voice_id)
            raise VoiceTooLongError(
                f"an utterance may run to {MAX_UTTERANCE_S:.0f}s — this one was cancelled"
            )
        backend = self._require_backend()
        try:
            interim = await asyncio.wait_for(backend.feed(voice_id, audio), FEED_TIMEOUT_S)
        except TimeoutError as exc:
            await self._fail(voice_id)
            raise VoiceBackendTimeoutError(
                f"the voice backend did not answer within {FEED_TIMEOUT_S:.0f}s"
            ) from exc
        except Exception as exc:
            # Any other backend failure is a 502, not a traceback out of a
            # request handler: the composer degrades, the utterance settles.
            await self._fail(voice_id)
            raise VoiceBackendError(str(exc) or type(exc).__name__) from exc
        # Re-read: `cancel` may have landed while the backend was working, and a
        # settled utterance must not be dragged back into `recording`.
        live = self._sessions.get(voice_id)
        if live is None or live.state != "recording":
            raise VoiceStateError("the utterance settled while this chunk was in flight")
        updated = live.model_copy(
            update={
                "audio_bytes": live.audio_bytes + len(audio),
                "chunks": max(live.chunks + 1, sequence + 1),
                "interim": interim,
            }
        )
        self._sessions[voice_id] = updated
        return updated

    async def stop(self, voice_id: str) -> VoiceTranscript:
        """Release: finish the utterance and hand back the final transcript."""
        session = self._recording(voice_id)
        self._sessions[voice_id] = session.model_copy(update={"state": "transcribing"})
        backend = self._require_backend()
        try:
            transcript = await asyncio.wait_for(backend.stop(voice_id), STOP_TIMEOUT_S)
        except TimeoutError as exc:
            await self._fail(voice_id)
            raise VoiceBackendTimeoutError(
                f"the voice backend did not finish within {STOP_TIMEOUT_S:.0f}s"
            ) from exc
        except Exception as exc:
            # As in `feed`: the utterance settles and the caller gets a 502.
            await self._fail(voice_id)
            raise VoiceBackendError(str(exc) or type(exc).__name__) from exc
        # Terminal: the utterance is done and its slot is freed. The transcript
        # is the answer to this call and is held nowhere — what the user said is
        # theirs, and it lives in their composer, not in a server-side history.
        self._sessions.pop(voice_id, None)
        log.info(
            "voice.transcribed",
            voice_id=voice_id,
            duration_s=round(transcript.duration_s, 2),
            characters=len(transcript.text),
        )
        return transcript

    async def cancel(self, voice_id: str) -> VoiceSession:
        """Abandon the utterance: the audio is discarded and no text is produced."""
        session = self._sessions.pop(voice_id, None)
        if session is None:
            raise VoiceNotFoundError(voice_id)
        if self._backend is not None:
            await self._backend.cancel(voice_id)
        log.info("voice.cancelled", voice_id=voice_id)
        return session.model_copy(update={"state": "cancelled"})

    async def shutdown(self) -> None:
        """Cancel every utterance still in flight. A held microphone must not
        outlive the server that was listening."""
        for voice_id in list(self._sessions):
            try:
                await self.cancel(voice_id)
            except VoiceNotFoundError:  # pragma: no cover - raced with a stop
                continue

    # ---- internals ----------------------------------------------------------

    def _require_backend(self) -> VoiceBackend:
        """The backend behind a live utterance. A session cannot exist without
        one, so this is a narrowing that also refuses the impossible case rather
        than asserting it away."""
        if self._backend is None:  # pragma: no cover - no session can exist here
            raise VoiceUnavailableError("no_backend", "no voice backend is configured")
        return self._backend

    def _recording(self, voice_id: str) -> VoiceSession:
        session = self._sessions.get(voice_id)
        if session is None:
            raise VoiceNotFoundError(voice_id)
        if session.state != "recording":
            raise VoiceStateError(f"the utterance is {session.state}, not recording")
        return session

    async def _fail(self, voice_id: str) -> None:
        """Settle a broken utterance and let the backend drop its buffer."""
        self._sessions.pop(voice_id, None)
        if self._backend is not None:
            try:
                await self._backend.cancel(voice_id)
            except Exception as exc:
                # Already failing — a cancel that also throws must not mask the
                # failure that got us here.
                log.warning("voice.cancel_failed", voice_id=voice_id, error=str(exc))

    def _reap(self) -> None:
        """Drop utterances nobody is going to release.

        The backend is not told: this runs under the lock on the start path, and
        awaiting a backend there would let a second start slip past the cap. The
        backend's own buffer for a dead id is bounded by the same ceiling and is
        dropped when it next sees the id, or when the process ends.
        """
        cutoff = time.time() - (MAX_UTTERANCE_S + ABANDON_GRACE_S)
        for voice_id, session in list(self._sessions.items()):
            if session.started_at < cutoff:
                log.warning("voice.abandoned", voice_id=voice_id)
                del self._sessions[voice_id]
