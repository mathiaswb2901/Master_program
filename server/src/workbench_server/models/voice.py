"""Voice input schemas: push-to-talk dictation into the agent composer.

**The privacy posture is in the types, not in a comment somewhere.** Voice here
is *local transcription only* — audio is turned into text on this machine and
never leaves it. The browser's ``SpeechRecognition`` API was rejected as the
default (M7 plan §3) precisely because Chrome/WebView2 streams the microphone to
a cloud service, which is the one thing the README's local-first claim says never
happens. So :attr:`VoiceCapabilities.local_only` is a field on the wire rather
than a promise in prose: the UI renders it, and a day when some other path is
offered is a day this flag goes false and the user is told.

**What ships here is the seam, not the voice.** The lifecycle below — start,
ingest audio chunks, read interim text, stop for a final transcript — is walked
end to end in CI by ``FakeVoiceBackend`` (``WORKBENCH_VOICE_FAKE=1``) with no
microphone, no model and no audio hardware, exactly the way the Office host's
whole state machine is reachable with no Microsoft Office installed. The real
local-whisper backend plugs in behind
:class:`~workbench_server.services.voice.VoiceBackend`; it is owner-gated (a new
runtime dependency and a large model download) and nothing here assumes it.

Everything on the wire is a model in this module, and every one of them is
mirrored in ``ui/src/types.ts``.
"""

from typing import Annotated, Literal

from pydantic import Base64Bytes, BaseModel, Field

#: Which implementation is answering.
#:
#: * ``none``          — nothing is configured; voice is unavailable.
#: * ``fake``          — the scripted stand-in (``WORKBENCH_VOICE_FAKE=1``). It
#:   walks the whole lifecycle and transcribes nothing, so a UI that looks like
#:   it is listening must say so — see :attr:`VoiceCapabilities.fake_backend`.
#: * ``local_whisper`` — the owner-gated real one: a local model, on this
#:   machine. Reserved here so the wire vocabulary does not change the day it
#:   lands; no implementation in this repo registers it yet.
VoiceBackendKind = Literal["none", "fake", "local_whisper"]

#: Why voice is not available right now. ``None`` when it is.
#:
#: * ``no_backend``    — no voice backend is configured on this server.
#: * ``model_missing`` — a backend is configured but its local model is not on
#:   disk. The package being installed must never imply the model is present:
#:   that would be a silent failure at the moment somebody speaks.
#: * ``disabled``      — turned off by policy (``WORKBENCH_VOICE=off``).
VoiceUnavailableReason = Literal["no_backend", "model_missing", "disabled"]

#: ``WORKBENCH_VOICE``. ``auto`` uses whatever backend is actually available and
#: reports honestly when there is none; ``off`` refuses whatever is configured.
VoiceMode = Literal["auto", "off"]

#: The lifecycle of one push-to-talk utterance.
#:
#: * ``recording``    — the key/button is held; chunks are arriving.
#: * ``transcribing`` — released; the backend is finishing the utterance.
#: * ``final``        — terminal: a transcript was produced.
#: * ``cancelled``    — terminal: the user abandoned it; the audio is discarded
#:   and no transcript is ever produced. Escape while recording lands here.
#: * ``failed``       — terminal: the backend refused or gave up.
VoiceState = Literal["recording", "transcribing", "final", "cancelled", "failed"]

#: What the capture side is expected to send: 16-bit little-endian PCM, mono.
#: One format, stated once, because "whatever the browser produced" is how a
#: transcriber ends up guessing at a sample rate.
DEFAULT_SAMPLE_RATE_HZ = 16_000

#: Bytes per audio frame at :data:`DEFAULT_SAMPLE_RATE_HZ` — mono, 16-bit.
BYTES_PER_FRAME = 2

#: The rates a speech model is worth pointing at. Below 8 kHz there is nothing
#: to transcribe; above 48 kHz is a browser bug rather than a choice. Named
#: because :class:`StartVoiceRequest` bounds the session by them *and*
#: :data:`MAX_CHUNK_BYTES` is derived from the top of the range.
MIN_SAMPLE_RATE_HZ = 8_000
MAX_SAMPLE_RATE_HZ = 48_000

#: How long one push-to-talk utterance may be. A ceiling rather than a
#: preference: a held key that is never released must not grow a buffer without
#: bound, and a user who really wants to dictate for three minutes is better
#: served by two utterances they can each read before sending.
MAX_UTTERANCE_S = 120.0

#: How much audio one :class:`VoiceChunk` may carry, as a duration.
#:
#: The shipped capture sends 100 ms slices (``ui/src/voiceCapture.ts``), so a
#: whole second is ten times what any client this repo ships would send, and
#: still leaves room for one that batches a handful of slices per request.
MAX_CHUNK_S = 1.0

#: The same ceiling in **decoded** bytes, at the highest rate a session may be
#: opened at: 96,000, against the 3,200 one real chunk carries at the default
#: rate. Enforced by :attr:`VoiceChunk.audio` itself, which is the point — the
#: per-utterance budget (:data:`MAX_UTTERANCE_S`) lives in the service, several
#: layers past the parser, so without a bound here the only thing standing
#: between one absurd body and the service is the machine's memory. What this
#: does *not* claim: an ASGI server buffers a request body before any model is
#: constructed, so this is the application's cap on what it will accept, not a
#: transport-level one on what can be sent.
MAX_CHUNK_BYTES = int(MAX_SAMPLE_RATE_HZ * MAX_CHUNK_S) * BYTES_PER_FRAME


class VoiceCapabilities(BaseModel):
    """GET /api/voice/capabilities — what this machine can actually do, and why
    not when it cannot.

    The same honesty contract as
    :class:`~workbench_server.models.office_host.OfficeCapabilities`: the UI
    degrades from this and never from a guess, :attr:`available` is the only
    field that answers "can I dictate right now", and :attr:`fake_backend` says
    when that answer is being given by a stand-in that transcribes nothing.
    """

    #: Can a push-to-talk session start right now (policy AND a usable backend).
    available: bool
    #: Which implementation is answering.
    backend: VoiceBackendKind
    #: The configured policy, before resolution.
    mode: VoiceMode
    #: The scripted stand-in is answering (``WORKBENCH_VOICE_FAKE=1``). A UI that
    #: shows a listening indicator over this must say the words are canned.
    fake_backend: bool
    #: A local model is present on this machine. False under the fake — there is
    #: no model, and claiming one would be the silent failure this field exists
    #: to prevent.
    model_present: bool
    #: **Audio is transcribed on this machine and never leaves it.** True for
    #: every backend this repo ships. It is on the wire so the UI can say so
    #: from a fact, and so that offering any non-local path some day is a
    #: visible change here rather than a quiet one.
    local_only: bool = True
    #: Why :attr:`available` is False. None when it is True.
    reason: VoiceUnavailableReason | None = None
    #: One line naming the reason for the verdict, for the UI and the logs.
    detail: str
    #: What the capture side must send.
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ
    #: The ceiling on one utterance, in seconds.
    max_utterance_s: float = MAX_UTTERANCE_S


class VoiceSession(BaseModel):
    """One push-to-talk utterance, as the UI sees it.

    Also the object handed to :meth:`VoiceBackend.start
    <workbench_server.services.voice.VoiceBackend.start>`, which is why the
    counters live here rather than in a private structure: a backend that is
    handed the session it is transcribing can log and bound its own work without
    a second bookkeeping layer above it.
    """

    voice_id: str
    state: VoiceState
    #: Unix seconds at which the utterance started.
    started_at: float
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ
    #: Audio ingested so far. The service counts bytes and keeps none of them —
    #: holding the audio is the backend's business, and the one that ships holds
    #: nothing either.
    audio_bytes: int = 0
    #: Chunks ingested so far.
    chunks: int = 0
    #: The transcript so far, as a plain string — ``""`` until enough audio has
    #: arrived to say anything. Interim text is deliberately *not* a
    #: :class:`VoiceTranscript`: it carries no confidence and no duration
    #: because neither is knowable mid-utterance, and inventing them is how a UI
    #: ends up rendering a number that means nothing.
    interim: str = ""


class VoiceTranscript(BaseModel):
    """What was heard. Returned by ``POST /api/voice/{voice_id}/stop``.

    Never auto-sent anywhere: it lands in the composer as editable text, because
    a transcriber that is right 95% of the time and sends anyway is a
    transcriber that publishes its own mistakes.
    """

    text: str
    #: 0..1, the backend's own confidence. A backend that cannot say reports 0.0
    #: rather than a flattering guess.
    confidence: float = Field(ge=0.0, le=1.0)
    #: How much audio this transcript came from.
    duration_s: float = Field(ge=0.0)
    #: Always True on the stop path — the field exists so an interim transcript
    #: can never be mistaken for a final one if a backend ever returns this type
    #: mid-utterance.
    final: bool = True


class StartVoiceRequest(BaseModel):
    """POST /api/voice/start."""

    #: The rate the capture side will actually send at, bounded to the range a
    #: speech model is worth pointing at (:data:`MIN_SAMPLE_RATE_HZ` ..
    #: :data:`MAX_SAMPLE_RATE_HZ`).
    sample_rate_hz: int = Field(
        default=DEFAULT_SAMPLE_RATE_HZ, ge=MIN_SAMPLE_RATE_HZ, le=MAX_SAMPLE_RATE_HZ
    )


class VoiceChunk(BaseModel):
    """POST /api/voice/{voice_id}/chunk — one slice of captured audio.

    Base64 because every payload here is a JSON model (house rule) and audio is
    bytes; the encoding costs a third more on the wire and buys one wire format
    for the whole API. It is loopback traffic to a server on the same machine,
    which is the only reason that trade is free.
    """

    #: 0-based, in capture order, and **enforced**: the service refuses a chunk
    #: whose sequence does not advance past the audio already ingested, so a
    #: duplicate or a slice that arrived behind one already fed is a 409 rather
    #: than audio quietly spliced into the wrong place. It is also handed to the
    #: backend, which is what lets a transcriber tell a *gap* (a slice the client
    #: dropped) from a contiguous stream and insert silence rather than splice.
    sequence: int = Field(ge=0)
    #: 16-bit little-endian PCM, mono, at the session's sample rate, bounded at
    #: :data:`MAX_CHUNK_BYTES` **decoded** — a body no capture could have
    #: produced is refused by the schema, naming the cap, instead of travelling
    #: as far as the service's per-utterance budget.
    audio: Annotated[Base64Bytes, Field(max_length=MAX_CHUNK_BYTES)]
