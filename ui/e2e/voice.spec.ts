/**
 * Journey — push-to-talk dictation against the fake voice backend (M7 §3).
 *
 * The claim this file exists to prove is that **the voice wiring is real**: a
 * press opens an utterance on the server, audio chunks flow, interim text
 * streams back into the composer as it is heard, and a release leaves a final
 * transcript *as an editable draft that was not sent anywhere*. It runs on a
 * headless runner with no microphone because both ends have a stand-in —
 * `WORKBENCH_VOICE_FAKE=1` server-side (scripted words, nothing heard) and the
 * scripted capture in `ui/src/voiceCapture.ts` browser-side (silence on a
 * timer). Neither invents the *lifecycle*; only the audio and the words.
 *
 * Asserts:
 *  - the microphone is offered, and the capabilities endpoint says honestly
 *    that a fake is answering, that no model is present, and that audio never
 *    leaves the machine;
 *  - hold-to-talk with a real pointer: the control enters an unmistakable
 *    recording state and interim words appear in the composer while it is held;
 *  - release leaves the final text in the composer and **sends nothing** — the
 *    human edits it and presses Enter themselves;
 *  - the keyboard gesture *toggles* (`Alt+V`), because holding a key is an
 *    accessibility trap, and Escape abandons an utterance and restores the
 *    draft exactly;
 *  - two agent panes are independent, and the one microphone moves between them
 *    rather than being shared: taking it back restores the composer it left.
 *
 * Every locator is scoped to the pane it is about. Several composers are on
 * screen at once by the end of this file, and an unscoped locator on a
 * pane-internal class is a singleton assumption wearing a selector.
 */

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import { newSession, openApp } from "./app";

/** What the fake backend always transcribes — `FAKE_SCRIPT` in services/voice.py. */
const FINAL_TEXT = "summarise the day-ahead spread for tomorrow";

/** The default Agent panel (the session browser plus the focused chat). */
const agentPane = (page: Page): Locator => page.locator(".wb-agent");

/** A pane bound to one session, found by the title its first message gave it. */
const boundPane = (page: Page, title: string): Locator =>
  page.locator(".dv-groupview", { has: page.locator(".wb-panel-tab", { hasText: title }) });

const composer = (pane: Locator): Locator => pane.locator(".wb-chat-input textarea");
const mic = (pane: Locator): Locator => pane.locator(".wb-voice-btn");

/** Hold the microphone down with a real pointer, as a hand would. */
async function press(page: Page, button: Locator): Promise<void> {
  await button.hover();
  await page.mouse.down();
}

const release = async (page: Page): Promise<void> => page.mouse.up();

/** Split the focused pane and pick a row — the gesture `panes.spec.ts` documents. */
async function split(page: Page, row: string): Promise<void> {
  await page.keyboard.press("Alt+S");
  const dialog = page.getByRole("dialog", { name: "Split this pane to the right" });
  await expect(dialog).toBeVisible();
  await dialog.locator(".wb-qb-row", { hasText: row }).first().click();
  await expect(dialog).toBeHidden();
}

/**
 * Leave the window arranged the way every other journey expects. This file
 * splits panes and persists the arrangement, and the journeys after it must not
 * inherit three agent panes because a step here failed.
 */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

test("dictate into the composer, edit it, then send it yourself", async ({ page }) => {
  await openApp(page);
  await newSession(page);
  const pane = agentPane(page);
  const box = composer(pane);

  await test.step("the server is honest about what is answering", async () => {
    const caps = await page.request.get("/api/voice/capabilities").then((r) => r.json());
    expect(caps.available).toBe(true);
    expect(caps.fake_backend).toBe(true);
    // No model, and it says so — a package installed is not a model downloaded.
    expect(caps.model_present).toBe(false);
    // The posture the whole design is built around, on the wire.
    expect(caps.local_only).toBe(true);
    expect(caps.reason).toBeNull();
  });

  await test.step("the microphone is offered, and says where the audio goes", async () => {
    await expect(mic(pane)).toBeVisible();
    await expect(mic(pane)).toHaveAttribute("aria-pressed", "false");
    await expect(mic(pane)).toHaveAttribute(
      "title",
      /Audio is transcribed on this machine and never leaves it/,
    );
  });

  await test.step("it shares the action row without crowding Send", async () => {
    // Read from the laid-out window, because this is a *geometry* claim and the
    // one way it fails is invisible to a stylesheet check: `.wb-btn` is
    // `white-space: nowrap`, so a control that cannot shrink rides over the
    // button beside it in a narrow pane. Also the WCAG 2.2 hit target.
    const control = await mic(pane).boundingBox();
    const send = await pane.getByRole("button", { name: "Send" }).boundingBox();
    expect(control && send, "both controls are laid out").toBeTruthy();
    expect(control!.x + control!.width).toBeLessThanOrEqual(send!.x);
    expect(control!.height).toBeGreaterThanOrEqual(24);
  });

  await test.step("holding it is unmistakable, and the words arrive while it is held", async () => {
    await box.fill("check ");
    const sendBefore = (await pane.getByRole("button", { name: "Send" }).boundingBox())?.x;
    await press(page, mic(pane));
    await expect(mic(pane)).toHaveClass(/is-recording/);
    await expect(mic(pane)).toHaveAttribute("aria-pressed", "true");
    // The running commentary appears beside the control, and *Send* does not
    // move for it (§1.9): a button that shifts when you start talking is a
    // button you can miss when you stop.
    expect((await pane.getByRole("button", { name: "Send" }).boundingBox())?.x).toBe(sendBefore);
    // The state is said in words, not only in hue and motion (§7) — and the
    // words admit the transcript is scripted, rather than looking like it works.
    await expect(pane.locator(".wb-voice-note")).toContainText("Listening");
    await expect(pane.locator(".wb-voice-note")).toContainText("no microphone is open");
    // Interim text, appended to what was already typed, growing as it is heard.
    await expect(box).toHaveValue(/^check summarise the day-ahead/);
  });

  await test.step("releasing leaves an editable draft and sends nothing", async () => {
    await release(page);
    await expect(box).toHaveValue(`check ${FINAL_TEXT}`);
    await expect(mic(pane)).not.toHaveClass(/is-recording/);
    // Nothing was sent. This is the whole point: a transcriber that presses
    // Enter for you publishes its own mistakes.
    await expect(pane.locator(".wb-msg-user", { hasText: "day-ahead" })).toHaveCount(0);
  });

  await test.step("the human edits it and sends it", async () => {
    await box.press("End");
    await box.pressSequentially(" for SE3");
    await box.press("Enter");
    await expect(
      pane.locator(".wb-msg-user", { hasText: `check ${FINAL_TEXT} for SE3` }),
    ).toBeVisible();
  });
});

test("the keyboard toggles instead of holding, and Escape abandons the utterance", async ({
  page,
}) => {
  await openApp(page);
  await newSession(page);
  const pane = agentPane(page);
  const box = composer(pane);

  await test.step("Alt+V starts and Alt+V finishes — no key is ever held down", async () => {
    await box.fill("");
    await page.keyboard.press("Alt+V");
    await expect(mic(pane)).toHaveClass(/is-recording/);
    // Still recording several frames later: a chord that behaved like a hold
    // would have ended the moment the key came up.
    await expect(box).toHaveValue(/summarise the/);
    await expect(mic(pane)).toHaveClass(/is-recording/);

    await page.keyboard.press("Alt+V");
    await expect(mic(pane)).not.toHaveClass(/is-recording/);
    await expect(box).toHaveValue(FINAL_TEXT);
  });

  await test.step("Escape throws the audio away and puts the draft back exactly", async () => {
    await box.fill("keep exactly this");
    await page.keyboard.press("Alt+V");
    await expect(box).toHaveValue(/^keep exactly this summarise/);

    await page.keyboard.press("Escape");
    await expect(mic(pane)).not.toHaveClass(/is-recording/);
    await expect(box).toHaveValue("keep exactly this");
    await expect(pane.locator(".wb-msg-user", { hasText: "summarise" })).toHaveCount(0);
  });
});

/** Split the focused pane into a fresh agent session, and name it by sending one
 * message — a named tab is how this journey tells two composers apart. */
async function sessionPane(page: Page, title: string): Promise<Locator> {
  await split(page, "New agent session");
  const fresh = page.locator(".dv-active-group");
  await composer(fresh).fill(title);
  await composer(fresh).press("Enter");
  await expect(page.locator(".wb-panel-tab", { hasText: title })).toBeVisible();
  return boundPane(page, title);
}

test("two composers are independent, and the one microphone moves between them", async ({
  page,
}) => {
  await openApp(page);
  // Two panes each bound to *their own* session, so "independent" is a real
  // claim: the default Agent panel follows whichever session has the keyboard,
  // so a bound pane beside it would be two views of one conversation.
  await page.locator(".wb-panel-tab", { hasText: "Agent" }).first().click();
  const beta = await sessionPane(page, "beta pane");
  const gamma = await sessionPane(page, "gamma pane");
  await expect(composer(beta)).toBeVisible();
  await expect(composer(gamma)).toBeVisible();

  await test.step("dictating in one leaves the other untouched", async () => {
    // The chord, so that the *second* utterance below can start while the first
    // is still open — which a single pointer cannot physically do.
    await composer(gamma).click();
    await page.keyboard.press("Alt+V");
    await expect(mic(gamma)).toHaveClass(/is-recording/);
    await expect(composer(gamma)).toHaveValue(/summarise/);
    // The other pane's control never entered a recording state, and its draft
    // never changed. One microphone, and it is not a global one.
    await expect(mic(beta)).not.toHaveClass(/is-recording/);
    await expect(composer(beta)).toHaveValue("");
  });

  await test.step("taking the microphone back restores the composer it left", async () => {
    await composer(beta).click();
    await page.keyboard.press("Alt+V");
    await expect(mic(beta)).toHaveClass(/is-recording/);
    await expect(mic(gamma)).not.toHaveClass(/is-recording/);
    // The abandoned utterance is discarded, not spliced onto the new one: the
    // pane that lost the microphone goes back to exactly what it held.
    await expect(composer(gamma)).toHaveValue("");
    await expect(composer(beta)).toHaveValue(/summarise/);

    await page.keyboard.press("Alt+V");
    await expect(composer(beta)).toHaveValue(FINAL_TEXT);
    await expect(mic(beta)).not.toHaveClass(/is-recording/);
  });
});
