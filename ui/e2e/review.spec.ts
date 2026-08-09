/**
 * Journey — validation surfaces in the app (M6 PR3).
 *
 * The order is the feature. First the **quiet** bar: with nothing validated, the
 * status reading is absent (§6.7 — a quiet bar means nothing needs you). Then a
 * `ValidationResult` is driven into existence the way the server really mints
 * one — `POST /api/validation/run`. The reconciliation gate (#85) is now the one
 * registered check, so a run that names no check runs it; handed no spec, it
 * judges what it can and returns a `fail` line — a **high**-risk result, which is
 * a real end-to-end result the panel must surface. (The empty-evidence *blocked*
 * rendering is covered where it is deterministic — the unit suite's
 * `EvidenceGallery` case — since every registered check now emits evidence.)
 *
 * The Review panel then shows that result: the risk badge, and an evidence
 * gallery with the check's finding rather than blankness. A `high` result is
 * medium-or-worse, so it is **awaiting approval** — the one mandatory human
 * decision — and Approve records it and reflects the recorded
 * `ValidationApproval`.
 *
 * The POST runs inside the page so it rides the same-origin `/api` proxy and
 * carries the launch token (fetched from the token endpoint, which is exempt);
 * enforcement is on, so a tokenless POST would 401.
 */

import { writeFile } from "node:fs/promises";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { openApp } from "./app";

interface ValidationSubject {
  kind: "session_output" | "file" | "objective";
  ref: string;
  label: string;
}

const NO_LAYOUT = { current: null, current_name: null, saved: [] };

/** Pane ids as they are on disk — the strings the next launch will trust. */
async function persistedPaneIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).sort();
}

/** Wait until the debounced autosave has written a pane we just opened. */
async function persisted(page: Page, paneId: string): Promise<void> {
  await expect.poll(() => persistedPaneIds(page), { timeout: 10_000 }).toContain(paneId);
}

// Each test starts from the default arrangement: the two-pane test opens instance
// panes that would otherwise be scenery in the single-result test, and the shared
// server holds both runs' validations in memory across tests.
test.beforeEach(async ({ page }) => {
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});

// And on the way out, whether or not the journey passed. The layout is
// server-side state on a server every spec file shares, so a test that dies
// holding two agent panes open leaves them open for whatever file runs next —
// which is how one red test here also turned `status.spec.ts` red, with a
// `.wb-chat-input textarea` that resolved to two elements and a failure message
// pointing at neither cause. A cleanup that only runs on success is not one.
test.afterEach(async ({ page }) => {
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});

/** Mint a real result through the server, from inside the page so the token
 * rides along. Returns its `validation_id`. */
async function seedValidation(
  page: Page,
  subject: ValidationSubject,
  checks: string[] = [],
): Promise<string> {
  return page.evaluate(
    async ({ subject, checks }) => {
      const tokenRes = await fetch("/api/auth/token");
      const { token } = (await tokenRes.json()) as { token: string };
      const res = await fetch("/api/validation/run", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Workbench-Token": token },
        body: JSON.stringify({ subject, checks, params: {} }),
      });
      if (!res.ok) throw new Error(`run failed: ${String(res.status)}`);
      const result = (await res.json()) as { validation_id: string; risk: string };
      return result.validation_id;
    },
    { subject, checks },
  );
}

/** Open the Review panel the way a user reaches a tool not on screen: the
 * QuickBar, through the registry (never a hardcoded panel button). */
async function openReviewPanel(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">Review validation");
  await quickbar.locator(".wb-qb-row", { hasText: "Review validation" }).first().click();
  await expect(page.locator(".wb-review")).toBeVisible();
}

test("validation surfaces: the quiet bar, the risk badge, the approval gate", async ({
  page,
}) => {
  await openApp(page);

  await test.step("with nothing validated, the status reading is absent (§6.7)", async () => {
    await expect(page.locator(".wb-review-status")).toHaveCount(0);
  });

  const subject: ValidationSubject = {
    kind: "session_output",
    ref: "sess-e2e",
    label: "Åsen 2 dispatch output",
  };

  await test.step("a run mints a result the registered check judged", async () => {
    const id = await seedValidation(page, subject);
    expect(id).toMatch(/^val_/);
  });

  await test.step("the status bar now carries a reading — one subject to review", async () => {
    const reading = page.locator(".wb-review-status");
    await expect(reading).toHaveCount(1);
    await expect(reading).toContainText("to review");
  });

  await test.step("the Review panel indexes the result and opens it", async () => {
    await openReviewPanel(page);
    // The bare pane is an index; the seeded result is a row, opened by click.
    const row = page.locator(".wb-review-row", { hasText: "Åsen 2 dispatch output" });
    await expect(row).toBeVisible();
    await row.click();
  });

  await test.step("the risk badge, and an evidence gallery carrying the check's finding", async () => {
    const body = page.locator(".wb-review-body");
    await expect(body).toBeVisible();
    // The header badge is the result's risk — high, from the check's fail. It is
    // scoped to the header so it is not confused with an evidence-row pill.
    await expect(body.locator(".wb-review-head .wb-pill")).toContainText("High risk");
    // The gallery is not blank: the check produced at least one evidence row, and
    // its outcome renders as a Fail pill (the finding, surfaced — not a silent
    // green). We assert the UI shape, not the check's own wording.
    await expect(body.locator(".wb-evidence")).not.toHaveCount(0);
    await expect(body.locator(".wb-evidence .wb-pill").first()).toContainText("Fail");
  });

  await test.step("a high-risk result awaits a human, and Approve records the decision", async () => {
    const body = page.locator(".wb-review-body");
    await expect(body.locator(".wb-review-awaiting")).toContainText("Awaiting approval");
    await body.locator(".wb-review-note").fill("acknowledged: reviewed the finding");
    await body.locator(".wb-review-approve").click();
    // The recorded ValidationApproval, reflected: who and the note.
    await expect(body.locator(".wb-review-approved")).toContainText("Approved by you");
    await expect(body.locator(".wb-review-approved")).toContainText("acknowledged");
    // And no button remains — the decision is settled.
    await expect(body.locator(".wb-review-approve")).toHaveCount(0);
  });

  await test.step("with the one result approved, the bar goes quiet again", async () => {
    await expect(page.locator(".wb-review-status")).toHaveCount(0);
  });
});

/**
 * The plural-tool contract (CLAUDE.md product principle 4): two Review panes are
 * two independent reviews — bound to two different `validation_id`s — and they
 * stay that way **through a reload**, which is what proves the binding lives in
 * the pane id on disk rather than in memory. A reload is the real cold launch:
 * the layout restores two `review#<id>` panes before `GET /api/validation`
 * resolves, so it also exercises the hydration gate (a still-held result must
 * come back live, never as a flashed tombstone).
 */
test("two Review panes are independent, through a reload", async ({ page }) => {
  await openApp(page);

  const first: ValidationSubject = { kind: "session_output", ref: "sess-two-a", label: "First subject A" };
  const second: ValidationSubject = { kind: "file", ref: "wb-two-b.xlsx", label: "Second subject B" };

  const idA = await seedValidation(page, first);
  const idB = await seedValidation(page, second);
  expect(idA).not.toBe(idB);

  await test.step("the index lists both; opening each row opens its own instance pane", async () => {
    await openReviewPanel(page);
    await page.locator(".wb-review-row", { hasText: "First subject A" }).click();
    // Back to the index pane to open the second — the first row opened a pane to
    // the side, leaving the index visible.
    await page.locator(".wb-review-row", { hasText: "Second subject B" }).click();
  });

  await test.step("two bodies, each its own subject — nothing bleeds across", async () => {
    const bodyA = page.locator(".wb-review-body", { hasText: "First subject A" });
    const bodyB = page.locator(".wb-review-body", { hasText: "Second subject B" });
    await expect(bodyA).toBeVisible();
    await expect(bodyB).toBeVisible();
    // Each pane resolved *its* id: A's body does not carry B's subject.
    await expect(bodyA).not.toContainText("Second subject B");
    await expect(bodyB).not.toContainText("First subject A");
    // Each pane is bound to its own validation_id on the DOM.
    await expect(page.locator(`.wb-review-body[data-validation="${idA}"]`)).toHaveCount(1);
    await expect(page.locator(`.wb-review-body[data-validation="${idB}"]`)).toHaveCount(1);
  });

  await test.step("a reload brings both back live — the id on disk, no tombstone flash", async () => {
    // Only really persisted if these exact strings are on disk (`ui/src/panes.ts`).
    await persisted(page, `review#${idA}`);
    await persisted(page, `review#${idB}`);
    await page.reload();
    // Both restored panes resolve their still-held results — live bodies, and the
    // tombstone's "no longer loaded" never appears for a result the server holds.
    await expect(page.locator(`.wb-review-body[data-validation="${idA}"]`)).toBeVisible();
    await expect(page.locator(`.wb-review-body[data-validation="${idB}"]`)).toBeVisible();
    await expect(page.locator(".wb-review-tombstone")).toHaveCount(0);
  });

  // Put the arrangement back for whatever journey shares this workspace next.
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});

/**
 * The toolchain gate (M6 staged review PR1), end to end in a real browser — and
 * the half no unit test can claim: **the captured log opens in the panel**.
 *
 * The gate runs in the checkout the subject session is actually writing in, so
 * this journey has to produce one: it starts an orchestrator, lets it spawn
 * workers through the real service (each really borrows a pool slot), and then
 * validates *a worker's own session id*. `WORKBENCH_GATE_FAKE=1` scripts the
 * exit codes and the output — nothing runs ruff or pytest inside a slot on CI —
 * but everything around them is production: the slot lookup, the git
 * fingerprint before and after, the bounded head+tail capture, the bounded
 * payload store, `GET /api/validation/payload/gate/{ref}`, and the expander.
 *
 * The last two steps matter as much as the first: a `high` result **awaits a
 * human**, and approving it leaves the bar quiet for whatever runs next.
 */
const SPAWN_PROMPT = "spawn workers please";

interface Roster {
  orchestrators: {
    orchestrator_id: string;
    workers: { worker_id: string; slot: string | null; path: string }[];
  }[];
}

/** Open Mission Control the way a user reaches a tool not on screen. */
async function openBoard(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">mission control");
  await quickbar.locator(".wb-qb-row", { hasText: "Show Mission Control" }).first().click();
  await expect(page.locator(".wb-mission")).toBeVisible();
}

/** The session id of every orchestrator the fleet currently knows about.
 *
 * From `GET /api/activity`, and both halves of that choice are forced.
 * Not `/api/orchestrator`: that roster only gains an entry when an orchestrator
 * *spawns* (`_rosters.setdefault`, inside `spawn`), so the orchestrator this
 * journey has only just started is absent from it. Not the DOM either: a
 * "what was already here" snapshot taken from cards races the board's own
 * fetch, and a stale card arriving a frame later would read as the new one.
 * The activity feed is the fleet's per-session identity from the moment a
 * session is created — the very source the board builds its cards from — and
 * `session_id` there is the `data-session` on the card and the
 * `orchestrator_id` the roster will use a moment later. */
async function orchestratorSessionIds(page: Page): Promise<string[]> {
  const feed = (await (await page.request.get("/api/activity")).json()) as {
    sessions: { session_id: string; kind: string }[];
  };
  return feed.sessions.filter((row) => row.kind === "orchestrator").map((row) => row.session_id);
}

/** How many workers *this journey's* orchestrator has — never every crew's. */
async function workerCount(page: Page, orchestratorId: string): Promise<number> {
  const roster = (await (await page.request.get("/api/orchestrator")).json()) as Roster;
  return roster.orchestrators
    .filter((crew) => crew.orchestrator_id === orchestratorId)
    .flatMap((crew) => crew.workers).length;
}

/** Drive one named orchestrator the way a user does from the board: click its
 * card to open (and focus) its chat, then type into the pane that click just
 * focused. Both locators are scoped, and for the same reason. `.dv-active-group`
 * scopes the input — an unscoped `.wb-chat-input textarea` is several elements
 * once other panes are open — and `[data-session]` scopes the *card*, because
 * the board is shared with every journey that ran before this one and a stopped
 * crew keeps its card (CLAUDE.md: nothing assumes it is the only one of itself). */
async function sendToOrchestrator(
  page: Page,
  orchestratorId: string,
  text: string,
): Promise<void> {
  await page
    .locator(`.wb-mission-card[data-kind="orchestrator"][data-session="${orchestratorId}"]`)
    .locator(".wb-mission-title")
    .click();
  const input = page.locator(".dv-active-group .wb-chat-input textarea");
  await input.fill(text);
  await input.press("Enter");
  await expect(
    page.locator(".dv-active-group .wb-msg-user").filter({ hasText: text }),
  ).toBeVisible();
}

test("a toolchain gate proves a worker's checkout, and its log opens in the panel", async ({
  page,
}) => {
  await openApp(page);

  await test.step("the pool is really there — or this journey tests a refusal", async () => {
    // Without a git repository every spawn is refused and the gate would answer
    // "no slot" for a reason that has nothing to do with the gate.
    const pool = (await (await page.request.get("/api/worktrees")).json()) as {
      problem: string | null;
    };
    expect(pool.problem, `the E2E workspace is not a git repository: ${String(pool.problem)}`).toBe(
      null,
    );
  });

  const orchestratorId = await test.step(
    "an orchestrator spawns a worker, which borrows a slot",
    async () => {
      await openBoard(page);
      // The board is server-side state shared with every journey that ran
      // before this one, and stopping a crew does not remove its card. So this
      // records what was already there and claims only the orchestrator it
      // starts: `toHaveCount(1)` over *every* orchestrator card is the
      // singleton assumption CLAUDE.md forbids, and it is precisely what made
      // this journey red whenever `mission.spec.ts` ran ahead of it.
      const before = new Set(await orchestratorSessionIds(page));
      await page.keyboard.press("Control+Shift+P");
      const quickbar = page.getByRole("dialog", { name: "Quick open" });
      await quickbar.locator(".wb-qb-input").fill(">new orchestrator");
      await quickbar.locator(".wb-qb-row", { hasText: "New orchestrator session" }).first().click();

      let mine = "";
      await expect
        .poll(
          async () => {
            mine = (await orchestratorSessionIds(page)).filter((id) => !before.has(id))[0] ?? "";
            return mine;
          },
          { timeout: 15_000 },
        )
        .not.toBe("");
      // The board really renders a card for the one this journey started —
      // the original claim, made about a named orchestrator instead of as a
      // census of a board every earlier journey also wrote to.
      await expect(
        page.locator(`.wb-mission-card[data-kind="orchestrator"][data-session="${mine}"]`),
      ).toHaveCount(1);

      await sendToOrchestrator(page, mine, SPAWN_PROMPT);
      await expect
        .poll(async () => workerCount(page, mine), { timeout: 30_000 })
        .toBeGreaterThan(0);
      return mine;
    },
  );

  const workerId = await test.step("read the worker the gate will judge", async () => {
    const roster = (await (await page.request.get("/api/orchestrator")).json()) as Roster;
    // This journey's crew, by id. A `flatMap` over the whole roster would judge
    // whichever worker another spec happened to leave behind.
    const crews = roster.orchestrators.filter((crew) => crew.orchestrator_id === orchestratorId);
    expect(crews, "the orchestrator this journey started left the roster").toHaveLength(1);
    const worker = crews[0].workers[0];
    expect(worker, "the orchestrator spawned no worker").toBeDefined();
    // A slot each is the whole reason the pool exists — and the thing the gate
    // resolves rather than being handed.
    expect(worker.slot).not.toBeNull();
    return worker.worker_id;
  });

  const validationId = await test.step("the gate runs in that worker's own checkout", async () => {
    // `checks: ["gates"]` — the toolchain gate alone. An empty `checks` runs
    // *every* registered check, and the reconciliation gate handed no spec would
    // add a `numeric` fail of its own, which is a different feature's evidence
    // in this journey's assertions.
    const id = await seedValidation(
      page,
      { kind: "session_output", ref: workerId, label: "Worker gates" },
      ["gates"],
    );
    expect(id).toMatch(/^val_/);
    return id;
  });

  await test.step("four gate lines, one per gate, and the failing one is a Fail", async () => {
    await openReviewPanel(page);
    await page.locator(".wb-review-row", { hasText: "Worker gates" }).click();
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    await expect(body).toBeVisible();
    // One line per gate, not one grouped line — that is the design decision, and
    // it is visible here rather than only in a unit test.
    await expect(body.locator('.wb-evidence[data-kind="gate"]')).toHaveCount(4);
    await expect(body.locator('.wb-evidence[data-outcome="fail"]')).toHaveCount(1);
    await expect(body.locator(".wb-review-head .wb-pill")).toContainText("High risk");
  });

  await test.step("the payload route works: the captured log opens in the expander", async () => {
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    const failing = body.locator('.wb-evidence[data-outcome="fail"]');
    // Lazily fetched: nothing is loaded until the expander is opened.
    await expect(failing.locator(".wb-evidence-log")).toHaveCount(0);
    await failing.locator("summary").click();
    const log = failing.locator(".wb-evidence-log");
    await expect(log).toBeVisible();
    // The scripted pytest failure, read back through
    // GET /api/validation/payload/gate/{ref} — the frame's dead handle, alive.
    await expect(log).toContainText("1 failed, 118 passed");
    await expect(log).toContainText("test_dispatch.py:118");
    await expect(failing.locator(".wb-evidence-argv")).toContainText("exit 1");
  });

  await test.step("a failing gate awaits a human, and approving leaves the bar quiet", async () => {
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    await expect(body.locator(".wb-review-awaiting")).toContainText("Awaiting approval");
    await body.locator(".wb-review-approve").click();
    await expect(body.locator(".wb-review-approved")).toContainText("Approved by you");
  });

  await test.step("cleanup: the crew is reaped and its slots go back", async () => {
    // Through the REST stop path rather than the board's own button: this is
    // teardown, not a claim — a leaked lease is a checkout no later journey can
    // borrow for an hour, and `mission.spec.ts` is where reaping *from the
    // board* is the assertion. `REAP_PROMPT` is the agent-driven equivalent and
    // is exercised there too.
    const roster = (await (await page.request.get("/api/orchestrator")).json()) as Roster;
    for (const crew of roster.orchestrators) {
      await page.request.post(`/api/orchestrator/sessions/${crew.orchestrator_id}/stop`);
    }
    await expect
      .poll(
        async () => {
          const pool = (await (await page.request.get("/api/worktrees")).json()) as {
            slots: { state: string }[];
          };
          return pool.slots.filter((slot) => slot.state === "leased").length;
        },
        { timeout: 30_000 },
      )
      .toBe(0);
  });
});

/**
 * The adversarial review (M6 staged review PR2), end to end in a real browser.
 *
 * The half no unit test can claim is the same one PR 1's leg claims for the
 * gate: **the findings open in the panel**, redeemed through
 * `GET /api/validation/payload/diff/{ref}` — the payload route, carrying a
 * second shape.
 *
 * Two things make this journey worth its runtime rather than a duplicate of the
 * unit suite:
 *
 * 1. **The diff is built from an untracked file.** The worker's slot is a fresh
 *    checkout, so `git diff` against its lease base is empty; this journey writes
 *    a brand-new file into that slot and nothing else. If `build_diff` did not
 *    read `git ls-files --others`, the review would report "nothing to review"
 *    and this test would fail — which is the silent-green trap the plan named,
 *    caught in the one place it would really happen.
 * 2. **A must_fix review is still awaiting a human.** The reviewer found a
 *    blocking problem and the result is `high` with no approval on it. A check
 *    that could clear its own subject would make this step pass by approving
 *    itself, so asserting the *absence* here is the point.
 *
 * `WORKBENCH_FAKE_AGENT=1` scripts what the reviewer reports; everything around
 * it is production — the spawn seam, the slot lookup, the diff builder, the real
 * `report_findings` handler, the grouped evidence, `derive_risk`, and the panel.
 */
test("an adversarial review reads a worker's new file and its findings open in the panel", async ({
  page,
}) => {
  await openApp(page);

  await test.step("the pool is really there — or this journey tests a refusal", async () => {
    const pool = (await (await page.request.get("/api/worktrees")).json()) as {
      problem: string | null;
    };
    expect(pool.problem, `the E2E workspace is not a git repository: ${String(pool.problem)}`).toBe(
      null,
    );
  });

  const orchestratorId = await test.step("an orchestrator spawns a worker", async () => {
    await openBoard(page);
    const before = new Set(await orchestratorSessionIds(page));
    await page.keyboard.press("Control+Shift+P");
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    await quickbar.locator(".wb-qb-input").fill(">new orchestrator");
    await quickbar.locator(".wb-qb-row", { hasText: "New orchestrator session" }).first().click();

    let mine = "";
    await expect
      .poll(
        async () => {
          mine = (await orchestratorSessionIds(page)).filter((id) => !before.has(id))[0] ?? "";
          return mine;
        },
        { timeout: 15_000 },
      )
      .not.toBe("");
    await sendToOrchestrator(page, mine, SPAWN_PROMPT);
    await expect.poll(async () => workerCount(page, mine), { timeout: 30_000 }).toBeGreaterThan(0);
    return mine;
  });

  const workerId = await test.step("the worker writes a brand-new file in its slot", async () => {
    const roster = (await (await page.request.get("/api/orchestrator")).json()) as Roster;
    const crews = roster.orchestrators.filter((crew) => crew.orchestrator_id === orchestratorId);
    expect(crews, "the orchestrator this journey started left the roster").toHaveLength(1);
    const worker = crews[0].workers[0];
    expect(worker, "the orchestrator spawned no worker").toBeDefined();
    expect(worker.slot).not.toBeNull();

    // The file git has never heard of. Written here rather than by the fake
    // agent because it must land in the *worker's own slot*, which is a pooled
    // checkout outside the workspace — and because "a new file is invisible to
    // git diff" is precisely the condition under test.
    await writeFile(
      join(worker.path, "brand_new_module.py"),
      "def settle(mwh: float) -> float:\n    return mwh * PRICE\n",
      "utf-8",
    );
    return worker.worker_id;
  });

  const validationId = await test.step("the review runs over that slot's diff", async () => {
    // `checks: ["review"]` alone: an empty `checks` would also run the
    // reconciliation gate and the toolchain gate, whose evidence belongs to
    // other journeys' assertions.
    const id = await seedValidation(
      page,
      { kind: "session_output", ref: workerId, label: "Worker review" },
      ["review"],
    );
    expect(id).toMatch(/^val_/);
    return id;
  });

  await test.step("one grouped diff line, and it is a Fail", async () => {
    await openReviewPanel(page);
    await page.locator(".wb-review-row", { hasText: "Worker review" }).click();
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    await expect(body).toBeVisible();
    // **One** grouped line, not one per finding — the opposite of the gate's
    // shape, and the design decision made visible rather than only unit-tested.
    await expect(body.locator('.wb-evidence[data-kind="diff"]')).toHaveCount(1);
    const line = body.locator('.wb-evidence[data-kind="diff"]');
    await expect(line).toHaveAttribute("data-outcome", "fail");
    await expect(line.locator(".wb-evidence-detail")).toContainText("3 finding(s)");
    await expect(line.locator(".wb-evidence-detail")).toContainText("1 must_fix");
    await expect(body.locator(".wb-review-head .wb-pill")).toContainText("High risk");
  });

  await test.step("the payload route carries findings, refutation and all", async () => {
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    const line = body.locator('.wb-evidence[data-kind="diff"]');
    // Lazily fetched: nothing loads until the expander is opened.
    await expect(line.locator(".wb-evidence-rows")).toHaveCount(0);
    await line.locator("summary").click();
    const rows = line.locator(".wb-evidence-rows li");
    await expect(rows).toHaveCount(3);
    await expect(rows.first()).toContainText("must fix");
    // The refutation — the reason to believe the claim — is on screen, not
    // hidden behind a second expander.
    await expect(rows.first()).toContainText("Breaks when:");
    await expect(rows.first()).toContainText("DST");
  });

  await test.step("the review never approves: a must_fix result still awaits a human", async () => {
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    // The assertion is the *absence* of an approval. A check that could clear
    // its own subject would quietly become its own merge queue.
    await expect(body.locator(".wb-review-approved")).toHaveCount(0);
    await expect(body.locator(".wb-review-awaiting")).toContainText("Awaiting approval");
    await body.locator(".wb-review-approve").click();
    await expect(body.locator(".wb-review-approved")).toContainText("Approved by you");
  });

  await test.step("cleanup: the crew is reaped and its slots go back", async () => {
    const roster = (await (await page.request.get("/api/orchestrator")).json()) as Roster;
    for (const crew of roster.orchestrators) {
      await page.request.post(`/api/orchestrator/sessions/${crew.orchestrator_id}/stop`);
    }
    await expect
      .poll(
        async () => {
          const pool = (await (await page.request.get("/api/worktrees")).json()) as {
            slots: { state: string }[];
          };
          return pool.slots.filter((slot) => slot.state === "leased").length;
        },
        { timeout: 30_000 },
      )
      .toBe(0);
  });
});
