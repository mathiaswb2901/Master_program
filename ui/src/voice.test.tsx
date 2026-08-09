/**
 * The push-to-talk gesture, without a browser and without a microphone.
 *
 * The lifecycle is driven directly here — a composer registers through
 * `registerComposer`, the REST client and the capture module are both stubbed —
 * because these are questions about *sequence and races*, and the ones that
 * matter are the ones a journey cannot make happen on demand: a release that
 * beats the server's answer to `start`, a second composer taking the microphone
 * mid-utterance, a chunk that fails halfway through. The live half (a real
 * server, the fake backend, a real press) is `ui/e2e/voice.spec.ts`.
 *
 * Node environment like the other unit tests: nothing here renders.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { VoiceCapabilities, VoiceSession, VoiceTranscript } from "./types";
import type { ComposerHandle } from "./voice";

// ---- stubs -------------------------------------------------------------------

const toasts: { kind: string; message: string }[] = [];
vi.mock("./store", () => ({
  useStore: {
    getState: () => ({
      pushToast: (kind: string, message: string) => toasts.push({ kind, message }),
    }),
  },
}));

/** What the stubbed capture is told to produce, and the hook the test pulls. */
let emitChunk: (() => void) | null = null;
let captureStops = 0;
vi.mock("./voiceCapture", () => ({
  captureBlocker: (kind: string) => (kind === "scripted" ? null : "microphone capture is not built"),
  startCapture: (_kind: string, request: { onChunk: (pcm: Uint8Array) => void }) => {
    emitChunk = () => request.onChunk(new Uint8Array([0, 0, 0, 0]));
    return {
      stop: () => {
        captureStops += 1;
        emitChunk = null;
      },
    };
  },
  toBase64: () => "AAAA",
}));

const api = {
  getVoiceCapabilities: vi.fn<() => Promise<VoiceCapabilities>>(),
  startVoice: vi.fn<() => Promise<VoiceSession>>(),
  sendVoiceChunk: vi.fn<() => Promise<VoiceSession>>(),
  stopVoice: vi.fn<() => Promise<VoiceTranscript>>(),
  cancelVoice: vi.fn<() => Promise<VoiceSession>>(),
};
vi.mock("./api", () => api);

const CAPS: VoiceCapabilities = {
  available: true,
  backend: "fake",
  mode: "auto",
  fake_backend: true,
  model_present: false,
  local_only: true,
  reason: null,
  detail: "the fake voice backend is active",
  sample_rate_hz: 16_000,
  max_utterance_s: 120,
};

const session = (voiceId: string, interim = ""): VoiceSession => ({
  voice_id: voiceId,
  state: "recording",
  started_at: 0,
  sample_rate_hz: 16_000,
  audio_bytes: 0,
  chunks: 0,
  interim,
});

/** A composer, as the registry sees one: a ref whose `draft` really changes when
 * `setDraft` is called, which is what makes "the composer was put back exactly"
 * a thing a test can assert. */
function composer(initial = "") {
  const focuses = { count: 0 };
  const handle: { current: ComposerHandle } = {
    current: {
      draft: initial,
      setDraft: (text: string) => {
        handle.current = { ...handle.current, draft: text };
      },
      focus: () => {
        focuses.count += 1;
      },
    },
  };
  return {
    handle,
    focuses,
    get draft() {
      return handle.current.draft;
    },
  };
}

/** A fresh copy of the module: the capabilities probe is memoised per page, so a
 * test that wants a different answer needs a different module instance. */
async function loadVoice(caps: VoiceCapabilities | Error = CAPS) {
  vi.resetModules();
  toasts.length = 0;
  captureStops = 0;
  emitChunk = null;
  for (const fn of Object.values(api)) fn.mockReset();
  api.getVoiceCapabilities.mockImplementation(() =>
    caps instanceof Error ? Promise.reject(caps) : Promise.resolve(caps),
  );
  api.startVoice.mockResolvedValue(session("v1"));
  api.sendVoiceChunk.mockResolvedValue(session("v1", "summarise the"));
  api.stopVoice.mockResolvedValue({
    text: "summarise the day-ahead spread for tomorrow",
    confidence: 0.98,
    duration_s: 0.4,
    final: true,
  });
  api.cancelVoice.mockResolvedValue({ ...session("v1"), state: "cancelled" });
  return import("./voice");
}

/** Let the queues drain. Macrotasks too: the capture module arrives through a
 * dynamic `import()`, which no number of microtasks will resolve. */
const settle = async (): Promise<void> => {
  for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

beforeEach(() => {
  toasts.length = 0;
});

// ---- the join ------------------------------------------------------------------

describe("joinDraft", () => {
  it("is the spoken text when nothing was typed", async () => {
    const { joinDraft } = await loadVoice();
    expect(joinDraft("", "hello there")).toBe("hello there");
    expect(joinDraft("   ", "hello there")).toBe("hello there");
  });

  it("appends after what was typed, with exactly one space", async () => {
    const { joinDraft } = await loadVoice();
    expect(joinDraft("check", "the spread")).toBe("check the spread");
    // A trailing space the user typed must not become two.
    expect(joinDraft("check ", "the spread")).toBe("check the spread");
  });

  it("leaves the draft untouched while there is nothing to say yet", async () => {
    const { joinDraft } = await loadVoice();
    expect(joinDraft("check ", "")).toBe("check ");
  });
});

// ---- the lifecycle ---------------------------------------------------------------

describe("push to talk", () => {
  it("streams interim text into the composer and leaves the final text unsent", async () => {
    const voice = await loadVoice();
    const box = composer("check ");
    voice.registerComposer("s1", box.handle);

    await voice.startDictation("s1");
    expect(api.startVoice).toHaveBeenCalledWith({ sample_rate_hz: 16_000 });

    emitChunk?.();
    await settle();
    // Interim is the whole utterance so far, appended to what was already typed.
    expect(box.draft).toBe("check summarise the");

    await voice.releaseDictation("stop");
    expect(box.draft).toBe("check summarise the day-ahead spread for tomorrow");
    // The caret goes back to the composer: the words are a draft to read, not a
    // message that was sent.
    expect(box.focuses.count).toBe(1);
    expect(captureStops).toBe(1);
  });

  it("puts the composer back exactly as it was when the utterance is cancelled", async () => {
    const voice = await loadVoice();
    const box = composer("keep this");
    voice.registerComposer("s1", box.handle);

    await voice.startDictation("s1");
    emitChunk?.();
    await settle();
    expect(box.draft).not.toBe("keep this");

    await voice.releaseDictation("cancel");
    expect(box.draft).toBe("keep this");
    expect(api.cancelVoice).toHaveBeenCalledWith("v1");
    expect(api.stopVoice).not.toHaveBeenCalled();
  });

  it("acts on a release that beat the server's answer to start", async () => {
    // The quick-tap race: pointerup lands while `POST /start` is still open. A
    // gesture that forgot this leaves a microphone running with nobody holding it.
    const voice = await loadVoice();
    const box = composer();
    voice.registerComposer("s1", box.handle);

    let answer: (value: VoiceSession) => void = () => undefined;
    api.startVoice.mockImplementation(
      () =>
        new Promise<VoiceSession>((resolve) => {
          answer = resolve;
        }),
    );
    // The gesture is claimed synchronously, so this release is remembered
    // rather than dropped — that is the property under test.
    const started = voice.startDictation("s1");
    await voice.releaseDictation("stop"); // no voice_id yet
    expect(api.stopVoice).not.toHaveBeenCalled();

    await vi.waitFor(() => expect(api.startVoice).toHaveBeenCalled());
    answer(session("v1"));
    await started;
    await settle();
    expect(api.stopVoice).toHaveBeenCalledWith("v1");
    expect(box.draft).toBe("summarise the day-ahead spread for tomorrow");
  });

  it("gives the microphone to the second composer and discards the first's audio", async () => {
    // One microphone (see the module docstring). Two composers are independent —
    // the one that loses it must be restored, not left holding half a sentence.
    const voice = await loadVoice();
    const first = composer("first draft");
    const second = composer("second draft");
    voice.registerComposer("s1", first.handle);
    voice.registerComposer("s2", second.handle);

    await voice.startDictation("s1");
    emitChunk?.();
    await settle();
    expect(first.draft).toBe("first draft summarise the");

    api.startVoice.mockResolvedValue(session("v2"));
    await voice.startDictation("s2");
    await settle();

    expect(api.cancelVoice).toHaveBeenCalledWith("v1");
    expect(first.draft).toBe("first draft");
    // And the second composer's own text is untouched by any of it.
    expect(second.draft).toBe("second draft");
  });

  it("does not transcribe the same sentence twice when restarted in place", async () => {
    // Press the button while a chord-started utterance is still open, on the
    // *same* composer. The abandoned interim words are taken back — so the new
    // utterance must append to what was typed, not to what was heard. Reading
    // the live draft as the base gives "typed summarise the summarise the …".
    const voice = await loadVoice();
    const box = composer("typed ");
    voice.registerComposer("s1", box.handle);

    await voice.startDictation("s1");
    emitChunk?.();
    await settle();
    expect(box.draft).toBe("typed summarise the");

    api.startVoice.mockResolvedValue(session("v2"));
    await voice.startDictation("s1");
    await settle();
    await voice.releaseDictation("stop");
    expect(box.draft).toBe("typed summarise the day-ahead spread for tomorrow");
  });

  it("keeps text the human types by hand while dictating, instead of clobbering it", async () => {
    // The toggle gesture frees the hands to type *while* speaking. Each interim
    // is the whole utterance so far, so a naive rewrite from a base captured once
    // at start discards the manual edit. The base must self-heal against the live
    // draft: keep the human's text, replace only the spoken part.
    const voice = await loadVoice();
    const box = composer("");
    voice.registerComposer("s1", box.handle);

    await voice.startDictation("s1");
    api.sendVoiceChunk.mockResolvedValueOnce(session("v1", "summarise the"));
    emitChunk?.();
    await settle();
    expect(box.draft).toBe("summarise the");

    // The human prepends a note by hand while the microphone is still open.
    box.handle.current.setDraft("NOTE: summarise the");

    api.sendVoiceChunk.mockResolvedValueOnce(session("v1", "summarise the day-ahead"));
    emitChunk?.();
    await settle();
    // The interim still grows, but the hand-typed "NOTE:" is not discarded.
    expect(box.draft).toBe("NOTE: summarise the day-ahead");

    await voice.releaseDictation("stop");
    // And the final transcript lands after the human's text, not over it.
    expect(box.draft).toBe("NOTE: summarise the day-ahead spread for tomorrow");
  });

  it("stops and explains when a chunk is refused mid-utterance", async () => {
    const voice = await loadVoice();
    const box = composer("typed");
    voice.registerComposer("s1", box.handle);
    await voice.startDictation("s1");

    api.sendVoiceChunk.mockRejectedValue(new Error("an utterance may run to 120s"));
    emitChunk?.();
    await settle();

    expect(box.draft).toBe("typed");
    expect(toasts.at(-1)?.kind).toBe("error");
    expect(toasts.at(-1)?.message).toContain("120s");
  });
});

// ---- releasing when the composer goes away ---------------------------------------

describe("the microphone is released when its composer unmounts", () => {
  it("cancels the gesture when the holding composer's pane goes away", async () => {
    // Close the pane (Ctrl+W, detach) while Listening and the mic must not keep
    // recording until the server reaps the utterance ~2.5 min later. VoiceButton
    // registers through `registerComposer`, and its unmount/target-change cleanup
    // is exactly that call's disposer — so tearing the registration down is how a
    // pane closing reaches this module.
    const voice = await loadVoice();
    const box = composer("");
    const dispose = voice.registerComposer("s1", box.handle);

    await voice.startDictation("s1");
    expect(api.startVoice).toHaveBeenCalled();

    dispose(); // the pane unmounts
    await settle();

    // The utterance is handed back with a cancel, not left recording — and not
    // stopped, because there is no composer left to receive a final transcript.
    expect(api.cancelVoice).toHaveBeenCalledWith("v1");
    expect(api.stopVoice).not.toHaveBeenCalled();
    expect(captureStops).toBe(1);
  });

  it("acts on a release that beats the server's answer when the pane unmounts", async () => {
    // The quick-close race: the pane goes away while `POST /start` is still open.
    // The gesture is claimed synchronously, so the cancel is remembered and acted
    // on the moment the id arrives, rather than leaking a server-side utterance.
    const voice = await loadVoice();
    const box = composer("");
    const dispose = voice.registerComposer("s1", box.handle);

    let answer: (value: VoiceSession) => void = () => undefined;
    api.startVoice.mockImplementation(
      () =>
        new Promise<VoiceSession>((resolve) => {
          answer = resolve;
        }),
    );
    const started = voice.startDictation("s1");
    dispose(); // unmount before the server answered start

    await vi.waitFor(() => expect(api.startVoice).toHaveBeenCalled());
    answer(session("v1"));
    await started;
    await settle();
    expect(api.cancelVoice).toHaveBeenCalledWith("v1");
  });

  it("leaves the microphone alone when a different composer unmounts", async () => {
    // Only the holder's teardown releases. A sibling pane closing must not cancel
    // the utterance a different composer is still dictating.
    const voice = await loadVoice();
    const held = composer("");
    const other = composer("");
    voice.registerComposer("s1", held.handle);
    const disposeOther = voice.registerComposer("s2", other.handle);

    await voice.startDictation("s1");
    disposeOther(); // the *other* pane unmounts
    await settle();

    expect(api.cancelVoice).not.toHaveBeenCalled();
    // s1's gesture is still live: releasing it now stops the same utterance,
    // proving the sibling's teardown never touched it.
    await voice.releaseDictation("stop");
    expect(api.stopVoice).toHaveBeenCalledWith("v1");
    expect(held.draft).toBe("summarise the day-ahead spread for tomorrow");
  });
});

// ---- honest refusals -------------------------------------------------------------

describe("when dictation is unavailable", () => {
  it("says the server's own reason rather than inventing a second one", async () => {
    const voice = await loadVoice({
      ...CAPS,
      available: false,
      backend: "none",
      fake_backend: false,
      reason: "no_backend",
      detail: "no voice backend is configured",
    });
    voice.registerComposer("s1", composer().handle);
    await voice.startDictation("s1");
    expect(api.startVoice).not.toHaveBeenCalled();
    expect(voice.voiceBlocker()).toBe("no voice backend is configured");
    expect(toasts.at(-1)?.message).toBe("no voice backend is configured");
  });

  it("refuses before the gesture when there is nothing to capture with", async () => {
    // A registered transcriber with no microphone capture built: the server says
    // yes and the browser cannot, which must be a control that is not offered
    // rather than a button that throws when pressed.
    const voice = await loadVoice({ ...CAPS, backend: "local_whisper", fake_backend: false });
    voice.registerComposer("s1", composer().handle);
    await voice.startDictation("s1");
    expect(api.startVoice).not.toHaveBeenCalled();
    expect(voice.voiceBlocker()).toContain("microphone capture is not built");
  });

  it("degrades when the probe itself fails", async () => {
    const voice = await loadVoice(new Error("offline"));
    voice.registerComposer("s1", composer().handle);
    await voice.startDictation("s1");
    expect(api.startVoice).not.toHaveBeenCalled();
    expect(voice.voiceBlocker()).toBe("the voice service did not answer");
  });

  it("asks the user to focus a session when the chord names none", async () => {
    const voice = await loadVoice();
    voice.toggleDictation(null);
    expect(toasts.at(-1)?.kind).toBe("warn");
    expect(toasts.at(-1)?.message).toContain("focus an agent session");
  });
});

// ---- the toggle ------------------------------------------------------------------

describe("the keyboard gesture toggles rather than holds", () => {
  it("starts on the first press and finishes on the second", async () => {
    const voice = await loadVoice();
    const box = composer();
    voice.registerComposer("s1", box.handle);

    voice.toggleDictation("s1");
    await settle();
    expect(api.startVoice).toHaveBeenCalledTimes(1);

    voice.toggleDictation("s1");
    await settle();
    expect(api.stopVoice).toHaveBeenCalledWith("v1");
    expect(box.draft).toBe("summarise the day-ahead spread for tomorrow");
  });
});
