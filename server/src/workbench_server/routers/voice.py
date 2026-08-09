"""Voice input endpoints. Thin: the service owns the lifecycle and the bounds.

Four calls and no WebSocket, which is a decision rather than an omission. Interim
transcripts come back on the **chunk response** instead of riding ``/ws/events``
like host and session state do, because a half-spoken sentence is not app-wide
state: it belongs to the one composer whose microphone is open. Broadcasting it
would put a partial utterance in every window attached to this server — the
opposite of the privacy posture the feature exists to keep — and would buy
nothing, since the only reader is the pane that started it.

``GET /capabilities`` is the honest-degradation surface, mirroring
``GET /api/office/capabilities``: the UI asks once whether a microphone can be
offered at all, and shows *why* not when the answer is no.
"""

import structlog
from fastapi import APIRouter, HTTPException, Request

from workbench_server.models.voice import (
    StartVoiceRequest,
    VoiceCapabilities,
    VoiceChunk,
    VoiceSession,
    VoiceTranscript,
)
from workbench_server.services.voice import (
    VoiceBackendError,
    VoiceBackendTimeoutError,
    VoiceNotFoundError,
    VoiceService,
    VoiceStateError,
    VoiceTooLongError,
    VoiceUnavailableError,
)

log = structlog.get_logger()

router = APIRouter(prefix="/api/voice", tags=["voice"])


def _voice(request: Request) -> VoiceService:
    service: VoiceService = request.app.state.voice
    return service


@router.get("/capabilities")
def capabilities(request: Request) -> VoiceCapabilities:
    """Can this machine dictate right now, and why not when it cannot. The
    composer offers a microphone from this answer, never from a guess."""
    return _voice(request).capabilities()


@router.post("/start")
async def start(request: Request, body: StartVoiceRequest) -> VoiceSession:
    """Press: begin one push-to-talk utterance."""
    try:
        return await _voice(request).start(body.sample_rate_hz)
    except VoiceUnavailableError as e:
        # A policy answer, not a crash — 503 says "not here", and the detail
        # says which of the three reasons it is.
        raise HTTPException(503, e.detail) from e
    except VoiceStateError as e:
        # The concurrency cap. 429 rather than 409: nothing about *this* request
        # is wrong, there is simply no slot, and the message names the ceiling.
        raise HTTPException(429, str(e)) from e


@router.post("/{voice_id}/chunk")
async def chunk(request: Request, voice_id: str, body: VoiceChunk) -> VoiceSession:
    """Speaking: one slice of audio in, the utterance's interim text out."""
    try:
        return await _voice(request).feed(voice_id, body.sequence, bytes(body.audio))
    except VoiceNotFoundError as e:
        raise HTTPException(404, "no such utterance") from e
    except VoiceTooLongError as e:
        raise HTTPException(413, str(e)) from e
    except VoiceStateError as e:
        raise HTTPException(409, str(e)) from e
    except VoiceBackendTimeoutError as e:
        raise HTTPException(504, str(e)) from e
    except VoiceBackendError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/{voice_id}/stop")
async def stop(request: Request, voice_id: str) -> VoiceTranscript:
    """Release: the final transcript, for the composer to show and the human to
    edit. Nothing is sent anywhere on this path."""
    try:
        return await _voice(request).stop(voice_id)
    except VoiceNotFoundError as e:
        raise HTTPException(404, "no such utterance") from e
    except VoiceStateError as e:
        raise HTTPException(409, str(e)) from e
    except VoiceBackendTimeoutError as e:
        raise HTTPException(504, str(e)) from e
    except VoiceBackendError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/{voice_id}/cancel")
async def cancel(request: Request, voice_id: str) -> VoiceSession:
    """Escape: throw the audio away. Idempotent from the caller's side only in
    the sense that a second call 404s — there is nothing left to discard."""
    try:
        return await _voice(request).cancel(voice_id)
    except VoiceNotFoundError as e:
        raise HTTPException(404, "no such utterance") from e
