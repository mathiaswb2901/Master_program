/**
 * Voice input in the agent composer: push-to-talk, transcribed on this machine.
 *
 * **What this is.** The UI half of the M7 §3 seam. It owns one gesture — press,
 * speak, release — and turns it into the server's voice lifecycle, streaming the
 * interim transcript into the composer as it arrives and leaving the final text
 * there **as editable text that is never sent**. A transcriber that is right 95%
 * of the time and presses Enter for you is a transcriber that publishes its own
 * mistakes.
 *
 * **Why it is not a panel.** Voice is a card-on-input, not a place: it fills the
 * composer you are already talking to (the annotate-mode precedent, M5 item 3).
 * So it registers no tool and takes no line in `tools.ts`; the composer renders
 * {@link VoiceButton}, and the Agent tool contributes the command and the chord
 * because *the agent session* is what voice writes into.
 *
 * **Its state is its own** (CLAUDE.md's zustand rule): the store below is a
 * second `create()` instance living in the module that owns it, and nothing
 * outside this file reads it. What leaves is a component and two functions.
 *
 * **Two ways to work it, on purpose (§7).** The pointer *holds* — press, talk,
 * release, which is what push-to-talk means. The keyboard **toggles**, because
 * holding a key down is an accessibility trap: it fights key repeat, it is
 * impossible for anyone using a stick or switch access, and it is unreachable
 * from a screen reader's own key handling. Same control, same state, two
 * gestures, and the button says both in its accessible name.
 *
 * **One microphone, one utterance.** {@link VoiceStore.active} is a single
 * nullable field in a codebase whose rule is that nothing assumes it is the only
 * one of itself — deliberately, and this is the comment that rule asks for:
 * there really is one microphone on the machine. Two composers can each *offer*
 * to dictate (and the tests prove they are independent), but only one can hold
 * it, so starting here cancels there rather than mixing two voices into one
 * transcript. Which composer holds it is `active.target`, never an assumption.
 */

import {
  useEffect,
  useRef,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { create } from "zustand";

import * as api from "./api";
import { useStore } from "./store";
import type { VoiceCapabilities, VoiceSession } from "./types";
import type { VoiceCapture } from "./voiceCapture";

// ---- state -------------------------------------------------------------------

interface Utterance {
  /** Which composer is dictating — the agent session id. */
  target: string;
  /** `null` until the server has answered `start`; the gesture may end first. */
  voiceId: string | null;
  phase: "opening" | "listening" | "finishing";
  /** The transcript so far, as the server reports it. */
  interim: string;
}

interface VoiceStore {
  /** `null` until the probe answers, or when it failed. */
  caps: VoiceCapabilities | null;
  /** Why dictation cannot start here, or `null` when it can. Combines the
   * server's answer ("can anything transcribe this") with the capture side's
   * ("is there anything to capture with") — the composer needs both, and asking
   * before the gesture is what keeps a dead button off the screen. */
  blocker: string | null;
  /** See the module docstring: one microphone, so one utterance. */
  active: Utterance | null;
}

const useVoice = create<VoiceStore>(() => ({ caps: null, blocker: null, active: null }));

/** What dictation needs from a composer: what it holds, how to write it, and how
 * to put the caret back once the words land. */
export interface ComposerHandle {
  draft: string;
  setDraft: (text: string) => void;
  focus: () => void;
}

/** The mounted composers that can be dictated into, by session id. A module-level
 * registry keyed by the resource's own id — the `terminalInput.ts` pattern — so
 * the command and the chord can reach *the composer you are talking to* without
 * the app store growing a field about voice. */
const composers = new Map<string, { current: ComposerHandle }>();

/**
 * Register a composer as a dictation target. Returns its disposer.
 *
 * A ref rather than a value, so a keystroke does not re-register: the gesture
 * reads `.current` when it needs the live draft. Exported because the registry
 * *is* the seam between a composer and this module — {@link VoiceButton} is one
 * caller, and it is what lets the lifecycle be driven without a DOM.
 */
export function registerComposer(target: string, entry: { current: ComposerHandle }): () => void {
  composers.set(target, entry);
  return () => {
    if (composers.get(target) === entry) composers.delete(target);
  };
}

// ---- capabilities ------------------------------------------------------------

/** The probe, run at most once per page: what this machine can do, and whether
 * there is anything to capture with. Lazy — nothing here runs until a composer
 * mounts or the command is invoked, so a launch pays none of it. */
let probe: Promise<void> | null = null;

export function ensureVoice(): Promise<void> {
  probe ??= (async () => {
    const caps = await api.getVoiceCapabilities().catch(() => null);
    if (caps === null) {
      useVoice.setState({ caps: null, blocker: "the voice service did not answer" });
      return;
    }
    if (!caps.available) {
      // The server already wrote the sentence a human reads; do not invent a
      // second one. This is the "why not" the capabilities endpoint exists for.
      useVoice.setState({ caps, blocker: caps.detail });
      return;
    }
    // Only now is the capture module worth fetching. Dynamic: it is not needed
    // to paint, and the real microphone encoder will land inside it.
    const capture = await import("./voiceCapture");
    const kind = caps.fake_backend ? "scripted" : "microphone";
    useVoice.setState({ caps, blocker: capture.captureBlocker(kind) });
  })();
  return probe;
}

/** The one-line reason dictation is unavailable, for a QuickBar row's detail.
 * `null` while the probe has not run or when voice works. */
export function voiceBlocker(): string | null {
  return useVoice.getState().blocker;
}

// ---- the gesture --------------------------------------------------------------

/**
 * One press-to-release, and everything that can go wrong inside it.
 *
 * Held at module level rather than in the store because it is machinery, not
 * something a component renders: the capture handle, the send chain that keeps
 * chunks in order, and the flag that remembers a release which arrived *before*
 * the server finished answering `start` — a real race on a quick tap, and one
 * that would otherwise leave a microphone open with nobody holding it.
 */
interface Gesture {
  target: string;
  /** What the composer held when the microphone opened; spoken text is appended
   * to this, so a cancel restores it exactly. */
  base: string;
  capture: VoiceCapture | null;
  voiceId: string | null;
  sequence: number;
  /** The transcript so far, as the last chunk response reported it. */
  interim: string;
  /** A release that beat the server's answer, remembered until it can be acted on. */
  pendingRelease: "stop" | "cancel" | null;
  /** Serialises the chunk POSTs, so the server sees them in capture order. */
  chain: Promise<void>;
  closed: boolean;
}

let gesture: Gesture | null = null;

/** Append spoken text to what was already typed, without eating a deliberate
 * trailing space or gluing two words together. */
export function joinDraft(base: string, spoken: string): string {
  if (spoken === "") return base;
  if (base.trim() === "") return spoken;
  return `${base.replace(/\s+$/, "")} ${spoken}`;
}

function handleFor(target: string): ComposerHandle | null {
  return composers.get(target)?.current ?? null;
}

function toast(kind: "error" | "warn" | "info", message: string): void {
  useStore.getState().pushToast(kind, message);
}

/** Let go of `mine` if it is still the one holding the microphone. */
function forget(mine: Gesture): void {
  if (gesture !== mine) return;
  gesture = null;
  useVoice.setState({ active: null });
}

/**
 * Press: open the microphone for one composer.
 *
 * **The gesture is claimed synchronously**, before the first `await`. That is
 * the whole shape of this function and it is not an accident: a release can
 * arrive in the same tick as the press (a quick tap, or `Alt+V` twice), and a
 * version that claimed the microphone *after* awaiting the capabilities probe
 * would drop that release on the floor and leave an utterance recording with
 * nobody holding it. Everything asynchronous therefore happens against a
 * gesture object that already exists and can already be told to end.
 */
export function startDictation(target: string): Promise<void> {
  const handle = handleFor(target);
  if (handle === null) {
    toast("warn", "open an agent session to dictate into");
    return Promise.resolve();
  }
  // One microphone: whoever had it loses it, and their audio is discarded
  // rather than silently spliced onto this utterance.
  const previous = gesture;
  const mine: Gesture = {
    target,
    // What the spoken words get appended to. When the microphone is moving
    // *within one composer* — a press while a chord-started utterance is still
    // open — that is what the composer held **before** the abandoned utterance,
    // not the interim words it is one tick away from taking back. Reading the
    // live draft here instead would transcribe the same sentence twice.
    base: previous !== null && previous.target === target ? previous.base : handle.draft,
    capture: null,
    voiceId: null,
    sequence: 0,
    interim: "",
    pendingRelease: null,
    chain: Promise.resolve(),
    closed: false,
  };
  gesture = mine;
  useVoice.setState({ active: { target, voiceId: null, phase: "opening", interim: "" } });
  return openUtterance(mine, previous);
}

async function openUtterance(mine: Gesture, previous: Gesture | null): Promise<void> {
  if (previous !== null) await endGesture(previous, "cancel");
  await ensureVoice();
  // Superseded first: a gesture that already lost the microphone must not also
  // raise the refusal toast its successor is about to raise.
  if (gesture !== mine || mine.closed) return;
  const { caps, blocker } = useVoice.getState();
  if (blocker !== null || caps === null) {
    forget(mine);
    toast("warn", blocker ?? "voice input is not available here");
    return;
  }

  let session: VoiceSession;
  try {
    session = await api.startVoice({ sample_rate_hz: caps.sample_rate_hz });
  } catch (err) {
    forget(mine);
    toast("error", `Could not start dictating: ${errorText(err)}`);
    return;
  }
  if (gesture !== mine || mine.closed) {
    // Superseded while the request was in flight — hand the utterance back
    // rather than leaving one recording on the server with no holder.
    void api.cancelVoice(session.voice_id).catch(() => undefined);
    return;
  }
  mine.voiceId = session.voice_id;
  useVoice.setState({
    active: { target: mine.target, voiceId: session.voice_id, phase: "listening", interim: "" },
  });

  if (mine.pendingRelease !== null) {
    // The user let go before the server answered. Act on it now, with the id.
    const pending = mine.pendingRelease;
    mine.pendingRelease = null;
    await endGesture(mine, pending);
    return;
  }

  const { startCapture, toBase64 } = await import("./voiceCapture");
  if (gesture !== mine || mine.closed) return;
  mine.capture = startCapture(caps.fake_backend ? "scripted" : "microphone", {
    sampleRateHz: caps.sample_rate_hz,
    onChunk: (pcm) => {
      mine.chain = mine.chain.then(() => sendChunk(mine, toBase64(pcm)));
    },
  });
}

async function sendChunk(mine: Gesture, audio: string): Promise<void> {
  if (mine.closed || mine.voiceId === null || gesture !== mine) return;
  const sequence = mine.sequence++;
  try {
    const session = await api.sendVoiceChunk(mine.voiceId, { sequence, audio });
    if (mine.closed || gesture !== mine) return;
    mine.interim = session.interim;
    useVoice.setState({
      active: {
        target: mine.target,
        voiceId: mine.voiceId,
        phase: "listening",
        interim: session.interim,
      },
    });
    // Interim is the utterance *so far*, so the composer replaces rather than
    // stitches — the reason the server sends the whole thing every time.
    handleFor(mine.target)?.setDraft(joinDraft(mine.base, session.interim));
  } catch (err) {
    if (mine.closed || gesture !== mine) return;
    mine.closed = true;
    mine.capture?.stop();
    handleFor(mine.target)?.setDraft(mine.base);
    forget(mine);
    toast("error", `Dictation stopped: ${errorText(err)}`);
  }
}

/**
 * Release. `stop` asks for the final transcript; `cancel` throws the audio away
 * and puts the composer back exactly as it was.
 */
export function releaseDictation(mode: "stop" | "cancel"): Promise<void> {
  const mine = gesture;
  return mine === null ? Promise.resolve() : endGesture(mine, mode);
}

/** End one specific utterance — named rather than "whichever is current",
 * because taking the microphone from a previous composer has to end *that* one
 * while this one already holds it. */
async function endGesture(mine: Gesture, mode: "stop" | "cancel"): Promise<void> {
  if (mine.closed) return;
  mine.capture?.stop();
  if (mine.voiceId === null) {
    // The server has not answered `start` yet; `openUtterance` will finish this
    // the moment it has an id to finish it with.
    mine.pendingRelease = mode;
    return;
  }
  mine.closed = true;
  const voiceId = mine.voiceId;
  const handle = handleFor(mine.target);

  if (mode === "cancel") {
    forget(mine);
    handle?.setDraft(mine.base);
    await api.cancelVoice(voiceId).catch(() => undefined);
    return;
  }

  if (gesture === mine) {
    useVoice.setState({
      active: { target: mine.target, voiceId, phase: "finishing", interim: mine.interim },
    });
  }
  // Let the chunks already in flight land first, so the final transcript is not
  // computed from an utterance the server has not finished hearing.
  await mine.chain.catch(() => undefined);
  try {
    const transcript = await api.stopVoice(voiceId);
    handle?.setDraft(joinDraft(mine.base, transcript.text));
    // Focus the composer, not the microphone: the whole point is that the words
    // land as a draft the human reads before pressing Enter themselves.
    handle?.focus();
  } catch (err) {
    handle?.setDraft(mine.base);
    toast("error", `Could not transcribe: ${errorText(err)}`);
  } finally {
    forget(mine);
  }
}

/** The keyboard gesture: same state, toggled rather than held. */
export function toggleDictation(target: string | null): void {
  if (target === null) {
    toast("warn", "focus an agent session to dictate into it");
    return;
  }
  const active = useVoice.getState().active;
  if (active !== null && active.target === target) void releaseDictation("stop");
  else void startDictation(target);
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ---- the control ---------------------------------------------------------------

/** 14px microphone, `currentColor` — no asset, no dependency. */
function MicIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
      <rect x="6" y="2" width="4" height="7" rx="2" fill="currentColor" />
      <path
        d="M4 7.5a4 4 0 0 0 8 0M8 11.5V14M6 14h4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
      />
    </svg>
  );
}

export interface VoiceButtonProps {
  /** The composer's session id — which conversation the words land in. */
  target: string;
  /** What the composer holds right now. */
  draft: string;
  setDraft: (text: string) => void;
  /** Put the caret back in the composer once the final text lands. */
  focusComposer: () => void;
}

/**
 * The push-to-talk control, rendered inside the composer's action row.
 *
 * Renders **nothing** when dictation is unavailable. That is the quiet-bar
 * doctrine (§6.7) rather than silence: the reason still exists and is still
 * reachable — the QuickBar's *Dictate* row carries it as its detail, and running
 * the command anyway says it in a toast — but a permanently dead microphone in
 * every composer on every machine with no local model is exactly the standing
 * clutter this app does not ship.
 */
export function VoiceButton({ target, draft, setDraft, focusComposer }: VoiceButtonProps) {
  const caps = useVoice((s) => s.caps);
  const blocker = useVoice((s) => s.blocker);
  const active = useVoice((s) => s.active);
  const held = useRef(false);

  // The registry entry is a ref so the command reaches the live draft without
  // re-registering on every keystroke. Updated in an effect rather than during
  // render: a render React throws away must not be able to leave a value behind.
  const handle = useRef<ComposerHandle>({ draft, setDraft, focus: focusComposer });
  useEffect(() => {
    handle.current = { draft, setDraft, focus: focusComposer };
  });

  useEffect(() => {
    void ensureVoice();
  }, []);

  useEffect(() => registerComposer(target, handle), [target]);

  const recording = active !== null && active.target === target;

  // Escape abandons the utterance — bound only while this composer holds the
  // microphone, so it never competes with anything else for the key.
  useEffect(() => {
    if (!recording) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      void releaseDictation("cancel");
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [recording]);

  // Every hook above runs unconditionally; only the painting is conditional.
  if (caps === null || blocker !== null || !caps.available) return null;

  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>): void => {
    // Capture the pointer so a release outside the button still ends the
    // utterance — dragging off a held control must not leave it recording.
    event.currentTarget.setPointerCapture(event.pointerId);
    held.current = true;
    void startDictation(target);
  };

  const endHold = (): void => {
    if (!held.current) return;
    held.current = false;
    void releaseDictation("stop");
  };

  const onClick = (event: ReactMouseEvent<HTMLButtonElement>): void => {
    // A keyboard activation reports `detail === 0`; a pointer click reports 1+
    // and has already been handled by the hold above. This is what lets one
    // control be hold-to-talk for a pointer and toggle for a keyboard.
    if (event.detail !== 0) return;
    toggleDictation(target);
  };

  const label = recording
    ? "Stop dictating and insert the text"
    : "Dictate — hold to talk, or press to toggle";

  return (
    <div className="wb-voice">
      <button
        type="button"
        className={"wb-btn wb-btn-outline wb-voice-btn" + (recording ? " is-recording" : "")}
        aria-pressed={recording}
        aria-label={label}
        title={`${label}. Audio is transcribed on this machine and never leaves it.`}
        onPointerDown={onPointerDown}
        onPointerUp={endHold}
        onPointerCancel={endHold}
        onClick={onClick}
      >
        <MicIcon />
        <span className="wb-voice-label">{recording ? "Listening" : "Dictate"}</span>
        {/* Pulses only while the microphone is open — transient attention, and
            the app's one keyframe (§5.4, §5.6). */}
        {recording && <span className="wb-voice-live u-agent-pulse" aria-hidden="true" />}
      </button>
      {/* Said in words as well as in motion (§7), and announced once rather than
          on every interim word — `polite` on a container that only changes when
          the state does. */}
      <span className="wb-voice-note" role="status">
        {recording
          ? caps.fake_backend
            ? "Listening (scripted: no microphone is open) — release to insert"
            : "Listening on this machine — release to insert"
          : ""}
      </span>
    </div>
  );
}
