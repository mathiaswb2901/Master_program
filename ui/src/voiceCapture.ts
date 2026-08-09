/**
 * The capture seam: where audio comes from, on the browser side.
 *
 * The server has a `VoiceBackend` seam (what turns audio into text); this is its
 * counterpart (what produces the audio in the first place), and it exists for
 * exactly the same reason: **a headless CI runner has no microphone**, so the
 * push-to-talk journey has to be drivable without one or it can only ever be
 * tested by the owner, by hand, once.
 *
 * Two kinds, and only one of them is built:
 *
 * - **`scripted`** — the one that ships. Emits fixed-size slices of *silence* on
 *   a timer. No microphone is opened, no permission is requested, and nothing
 *   about the machine is read. It is what `WORKBENCH_VOICE_FAKE=1` pairs with,
 *   and the fake server backend counts chunks rather than listening to them, so
 *   the whole lifecycle is deterministic.
 * - **`microphone`** — **NOT BUILT. This is the named UI seam** and it is
 *   owner-gated with the model itself. See {@link startMicrophoneCapture} for
 *   exactly what writing it involves; until it exists, {@link captureBlocker}
 *   refuses *before* the gesture rather than throwing in the middle of one.
 *
 * This module is reached through a dynamic `import()` from `voice.tsx`, so none
 * of it — nor the encoder that will land beside it — is on the launch path.
 */

/** What is producing audio. See the module docstring. */
export type CaptureKind = "scripted" | "microphone";

/** Called with each slice of 16-bit little-endian PCM, mono, in capture order. */
export type ChunkSink = (pcm: Uint8Array) => void;

export interface CaptureRequest {
  sampleRateHz: number;
  onChunk: ChunkSink;
}

/** A capture in progress. Stopping is idempotent: a release that arrives twice
 * (pointer up *and* pointer cancel, which browsers really do send) must not
 * turn into two stops. */
export interface VoiceCapture {
  stop: () => void;
}

/** How much audio one chunk carries. 100 ms is short enough that interim text
 * arrives while you are still speaking, and long enough that a two-minute
 * utterance is 1,200 requests rather than 12,000. */
export const CHUNK_MS = 100;

/** Bytes per frame: mono, 16-bit — the one format the server's `VoiceChunk`
 * documents, stated here too because this is the end that produces it. */
export const BYTES_PER_FRAME = 2;

/**
 * Why a capture cannot start, or `null` when it can.
 *
 * Asked **before** the gesture, so an unbuilt or refused capture is a control
 * that is not offered rather than a button that fails when pressed. The server's
 * `VoiceCapabilities.available` answers the other half ("can anything transcribe
 * it") and the composer needs both.
 */
export function captureBlocker(kind: CaptureKind): string | null {
  if (kind === "scripted") return null;
  return (
    "microphone capture is not built yet — voice ships as a seam, and the real " +
    "local transcriber is owner-gated (see docs/plan/m7-premium.md §3)"
  );
}

/** Start capturing. Throws only if {@link captureBlocker} was ignored. */
export function startCapture(kind: CaptureKind, request: CaptureRequest): VoiceCapture {
  const blocker = captureBlocker(kind);
  if (blocker !== null) throw new Error(blocker);
  return startScriptedCapture(request);
}

/**
 * Silence, on a timer. No microphone, no permission prompt, no device read.
 *
 * The bytes are zeroes on purpose: the fake server backend counts chunks and
 * transcribes nothing, so audio that *sounded* like something would be a
 * pretence with no reader. What this proves is the wiring — that a press
 * produces chunks, that chunks produce interim text, and that a release
 * produces a final transcript — which is the whole claim this PR makes.
 */
function startScriptedCapture({ sampleRateHz, onChunk }: CaptureRequest): VoiceCapture {
  const frames = Math.round((sampleRateHz * CHUNK_MS) / 1000);
  const silence = new Uint8Array(frames * BYTES_PER_FRAME);
  // A fresh copy per chunk: a sink that keeps the buffer (an encoder, a queue)
  // must not find it rewritten under it by the next tick.
  const timer = setInterval(() => onChunk(new Uint8Array(silence)), CHUNK_MS);
  let stopped = false;
  return {
    stop: () => {
      if (stopped) return;
      stopped = true;
      clearInterval(timer);
    },
  };
}

/**
 * **The named UI seam — deliberately unimplemented.**
 *
 * Writing it is: `navigator.mediaDevices.getUserMedia({ audio: … })`, an
 * `AudioWorklet` (not the deprecated `ScriptProcessorNode`) resampling to the
 * session's `sample_rate_hz`, a float32 → 16-bit LE conversion, and the same
 * `onChunk` contract as {@link startScriptedCapture} — plus the permission
 * story: a browser prompt the user must be able to refuse without the composer
 * ending up in a recording state that never resolves.
 *
 * It is owner-gated with the model, and for the same reason: neither can be
 * judged without a real microphone in the owner's room. Nothing calls this.
 */
export function startMicrophoneCapture(_request: CaptureRequest): VoiceCapture {
  throw new Error(captureBlocker("microphone") ?? "microphone capture is not built");
}

/** Base64 for the wire. Chunked so a large slice cannot blow the argument limit
 * of `String.fromCharCode` (~64k arguments on V8). */
export function toBase64(bytes: Uint8Array): string {
  let binary = "";
  const step = 8192;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(binary);
}
