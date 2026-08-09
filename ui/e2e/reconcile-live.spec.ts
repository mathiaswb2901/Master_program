/**
 * Journey — the reconciliation gate may never pass against a stale file.
 *
 * This is a **reproduction first**. On master, a workbook docked in a panel with
 * unsaved edits in it is reconciled against the `.xlsx` on disk — so the gate
 * reports `pass` about numbers it did not read. Both tests below fail there: the
 * first because the run comes back green, the second because a `blocked` outcome
 * did not exist to come back at all.
 *
 * Everything runs under `WORKBENCH_OFFICE_FAKE=1` (see `playwright.config.ts`),
 * so there is no Office, no Rust and no window anywhere near it. The fake
 * document bridge scripts the live-workbook states from the *document's name* —
 * the `FAILURE_TRIGGERS` precedent — which is what makes the whole decision table
 * reachable in CI:
 *
 * - `…dirty…`  -> `Workbook.Saved = False`; the live instance holds edits the
 *   file does not.
 * - `…nolive…` -> the bulk value read refuses while the cheap status read still
 *   answers, which is the one combination with no honest verdict.
 *
 * The two workbooks are written here, as **real** `.xlsx` bytes (a stored zip of
 * five OOXML parts — see `writeWorkbook`), because the point of the journey is
 * that the file on disk is perfectly readable and perfectly agrees with the
 * expectation. A gate that reconciled it would go green, which is exactly the
 * silent pass this refuses.
 */

import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { openApp } from "./app";
import { E2E_WORKSPACE } from "./workspace";

/**
 * A workbook the fake host reports **dirty** and can still be read live.
 *
 * Named to sort *after* `notes.md`: the fake agent's scripted `Read` targets the
 * first file in the workspace by name and journey 4 asserts which one that is,
 * so a fixture called `budget-…` would quietly retarget it (the rule
 * `e2e/workspace.ts` states for its own fixtures, and it binds here too).
 */
const DIRTY = "reconcile-dirty.xlsx";

/** …and one that is dirty *and* whose live values cannot be read: the blocked row. */
const DIRTY_NO_LIVE = "reconcile-dirty-nolive.xlsx";

/**
 * The fake bridge mints `Budget!B3` as 2000 for any workbook. The file on disk is
 * written with a *different* number, and the expectation matches the **file** —
 * so "read the file" is the answer that passes and "read the live workbook" is
 * the answer that is true.
 */
const DISK_B3 = 9999;

interface ValidationRun {
  validation_id: string;
  risk: string;
  evidence: { outcome: string; detail: string }[];
}

// ---- a minimal, real .xlsx --------------------------------------------------
// Deliberately hand-rolled rather than pulled from a library: an OOXML workbook
// with numeric cells is five small XML parts in a stored zip, and the E2E harness
// adding a spreadsheet dependency to write one fixture would be a poor trade.
// The same reasoning (and the same five parts) as
// `scripts/dev/build_document_templates.py`, which builds the blank templates.

const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = (c & 1) === 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

function crc32(buf: Buffer): number {
  let c = -1;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

/** A zip with every member **stored** (no compression), so the writer stays a
 * page of buffer arithmetic and needs nothing from `zlib`. */
function storedZip(entries: [string, string][]): Buffer {
  const parts: Buffer[] = [];
  const directory: Buffer[] = [];
  let offset = 0;
  for (const [name, text] of entries) {
    const data = Buffer.from(text, "utf-8");
    const nameBytes = Buffer.from(name, "utf-8");
    const sum = crc32(data);
    const local = Buffer.alloc(30 + nameBytes.length);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 8); // stored
    local.writeUInt16LE(33, 12); // a fixed 1980-01-01, so bytes are stable
    local.writeUInt32LE(sum, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    nameBytes.copy(local, 30);
    parts.push(local, data);

    const entry = Buffer.alloc(46 + nameBytes.length);
    entry.writeUInt32LE(0x02014b50, 0);
    entry.writeUInt16LE(20, 4);
    entry.writeUInt16LE(20, 6);
    entry.writeUInt16LE(33, 14);
    entry.writeUInt32LE(sum, 16);
    entry.writeUInt32LE(data.length, 20);
    entry.writeUInt32LE(data.length, 24);
    entry.writeUInt16LE(nameBytes.length, 28);
    entry.writeUInt32LE(offset, 42);
    nameBytes.copy(entry, 46);
    directory.push(entry);
    offset += local.length + data.length;
  }
  const dir = Buffer.concat(directory);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(directory.length, 8);
  end.writeUInt16LE(directory.length, 10);
  end.writeUInt32LE(dir.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...parts, dir, end]);
}

const XMLNS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
const PKG = "http://schemas.openxmlformats.org/package/2006/relationships";
const OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";

/** Write `name` into the E2E workspace as a real workbook with one sheet and the
 * given numeric cells, cached as literal `<v>` values — which is what
 * `openpyxl(data_only=True)` reads, and therefore what a disk reconciliation
 * would judge. */
function writeWorkbook(name: string, sheet: string, cells: Record<string, number>): void {
  const rows = new Map<number, string[]>();
  for (const [ref, value] of Object.entries(cells)) {
    const row = Number.parseInt(ref.replace(/^[A-Z]+/, ""), 10);
    const existing = rows.get(row) ?? [];
    existing.push(`<c r="${ref}"><v>${String(value)}</v></c>`);
    rows.set(row, existing);
  }
  const body = [...rows.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([row, cs]) => `<row r="${String(row)}">${cs.join("")}</row>`)
    .join("");
  const head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
  const bytes = storedZip([
    [
      "[Content_Types].xml",
      `${head}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
        '<Default Extension="xml" ContentType="application/xml"/>' +
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
        "</Types>",
    ],
    [
      "_rels/.rels",
      `${head}<Relationships xmlns="${PKG}"><Relationship Id="rId1" Type="${OD}/officeDocument" Target="xl/workbook.xml"/></Relationships>`,
    ],
    [
      "xl/workbook.xml",
      `${head}<workbook xmlns="${XMLNS}" xmlns:r="${OD}"><sheets><sheet name="${sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>`,
    ],
    [
      "xl/_rels/workbook.xml.rels",
      `${head}<Relationships xmlns="${PKG}"><Relationship Id="rId1" Type="${OD}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>`,
    ],
    ["xl/worksheets/sheet1.xml", `${head}<worksheet xmlns="${XMLNS}"><sheetData>${body}</sheetData></worksheet>`],
  ]);
  writeFileSync(join(E2E_WORKSPACE, name), bytes);
}

// ---- driving the server ------------------------------------------------------

/** Dock the workbook the way the panel does — `POST /api/office/host`. Under the
 * fake backend this walks the real launching → embedding → embedded lifecycle
 * and ends with a host the reconciliation gate can find. */
async function dock(page: Page, path: string): Promise<void> {
  const response = await page.request.post("/api/office/host", {
    data: { path, rect: { x: 0, y: 0, width: 800, height: 600 } },
  });
  expect(response.ok(), `docking ${path} failed: ${String(response.status())}`).toBe(true);
  expect((await response.json()) as { state: string }).toMatchObject({ state: "embedded" });
}

/** Run the reconciliation gate against `workbook`, expecting `expected` in
 * `Budget!B3` — the number the **file** holds. From inside the page, so the
 * launch token rides along. */
async function reconcile(page: Page, workbook: string, expected: number): Promise<ValidationRun> {
  return page.evaluate(
    async ({ workbook, expected }) => {
      const { token } = (await (await fetch("/api/auth/token")).json()) as { token: string };
      const res = await fetch("/api/validation/run", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Workbench-Token": token },
        body: JSON.stringify({
          subject: { kind: "file", ref: workbook, label: workbook },
          checks: ["reconciliation"],
          params: {
            workbook,
            default_tolerance: { rel: 0.0001 },
            expectations: [{ cell: "Budget!B3", expected, unit: "MWh" }],
          },
        }),
      });
      if (!res.ok) throw new Error(`run failed: ${String(res.status)}`);
      return (await res.json()) as {
        validation_id: string;
        risk: string;
        evidence: { outcome: string; detail: string }[];
      };
    },
    { workbook, expected },
  );
}

/**
 * Settle a result, so this file leaves the shared server as it found it.
 *
 * Not housekeeping — a correctness requirement of the suite. One backend serves
 * every spec file, and `review.spec.ts` opens by asserting the status bar is
 * *quiet* (DESIGN §6.7: nothing needs you). A `high` or `blocked` result is
 * medium-or-worse and therefore awaiting a human, so leaving two of them behind
 * turns that file red for a reason that has nothing to do with it — which is
 * exactly the cross-file failure the layout reset at the bottom of `review.spec`
 * exists to prevent, arriving from the other direction.
 */
async function approve(page: Page, validationId: string): Promise<void> {
  const response = await page.request.post(`/api/validation/${validationId}/approve`, {
    data: { approver: "e2e", note: "reconcile-live journey" },
  });
  expect(response.ok(), `approving ${validationId} failed`).toBe(true);
}

/** The Review panel, reached the way a user reaches a tool that is not on
 * screen: the QuickBar, through the registry. */
async function openReviewPanel(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">Review validation");
  await quickbar.locator(".wb-qb-row", { hasText: "Review validation" }).first().click();
  await expect(page.locator(".wb-review")).toBeVisible();
}

test.beforeAll(() => {
  writeWorkbook(DIRTY, "Budget", { B3: DISK_B3 });
  writeWorkbook(DIRTY_NO_LIVE, "Budget", { B3: DISK_B3 });
});

test.afterEach(async ({ page }) => {
  await page.request.put("/api/layouts", {
    data: { current: null, current_name: null, saved: [] },
  });
  await undock(page);
});

/**
 * Close every host this file opened.
 *
 * A docked host is server-side state on a backend every spec file shares, and it
 * is not inert: a workspace switch refuses while a document is hosted, and the
 * host map is bounded. Leaving two workbooks docked for the rest of the run is
 * the same class of cross-file failure as leaving two panes open — a red test in
 * a file that never mentions Office, pointing at neither cause.
 */
async function undock(page: Page): Promise<void> {
  const hosts = (await (await page.request.get("/api/office/hosts")).json()) as {
    hosts: { host_id: string; path: string; state: string }[];
  };
  for (const host of hosts.hosts) {
    if (host.path !== DIRTY && host.path !== DIRTY_NO_LIVE) continue;
    if (host.state === "closed" || host.state === "failed") continue;
    await page.request.post(`/api/office/host/${host.host_id}/close`);
  }
}

test("a docked workbook with unsaved edits is judged live, never against the file", async ({
  page,
}) => {
  await openApp(page);
  await dock(page, DIRTY);

  // The expectation matches the number **on disk** exactly. Before this change
  // the gate read that file and reported `pass` — a green badge about a workbook
  // whose live copy holds 2000. This assertion is the reproduction.
  const run = await reconcile(page, DIRTY, DISK_B3);
  expect(run.risk).not.toBe("pass");
  expect(run.risk).toBe("high");

  // …and the evidence says which of the two workbooks it read, in words, without
  // anyone expanding a table.
  const [line] = run.evidence;
  expect(line.detail).toContain("Read live from the docked workbook");
  expect(line.detail).toContain("workbook unsaved");

  await approve(page, run.validation_id);
});

test("dirty and unreadable is BLOCKED, and the panel names the way out", async ({ page }) => {
  await openApp(page);
  await dock(page, DIRTY_NO_LIVE);

  const run = await reconcile(page, DIRTY_NO_LIVE, DISK_B3);

  await test.step("the run refuses rather than passing against cached disk", () => {
    expect(run.risk).toBe("blocked");
    expect(run.evidence.map((item) => item.outcome)).toEqual(["blocked"]);
    // A refusal that does not name the way out is not worth having (AXI 3).
    expect(run.evidence[0].detail).toContain("unsaved changes");
    expect(run.evidence[0].detail).toContain("Save the workbook");
    expect(run.evidence[0].detail).toContain("desktop shell");
  });

  await test.step("the Review panel renders it as Blocked, not as a red fail", async () => {
    await openReviewPanel(page);
    await page.locator(".wb-review-row", { hasText: DIRTY_NO_LIVE }).first().click();
    const body = page.locator(`.wb-review-body[data-validation="${run.validation_id}"]`);
    await expect(body).toBeVisible();
    // The grey "could not judge" badge (DESIGN.md §2.6), on the header — and the
    // *word*, because colour is never the only signal (§7).
    await expect(body.locator(".wb-review-head .wb-pill")).toContainText("Blocked");
    await expect(body.locator(".wb-evidence .wb-pill").first()).toContainText("Blocked");
    // …and the remedy, on screen, where the person who has to act can read it.
    await expect(body).toContainText("Save the workbook");
  });

  await test.step("a blocked result awaits a human, and Approve settles it", async () => {
    // `blocked` is medium-or-worse, so the one mandatory decision applies to it
    // exactly as it does to a `high` — a refusal nobody has to acknowledge would
    // be a refusal that quietly ages out of the bar.
    const body = page.locator(`.wb-review-body[data-validation="${run.validation_id}"]`);
    await expect(body.locator(".wb-review-awaiting")).toContainText("Awaiting approval");
    await body.locator(".wb-review-note").fill("acknowledged: will save and re-run");
    await body.locator(".wb-review-approve").click();
    await expect(body.locator(".wb-review-approved")).toContainText("Approved by you");
  });
});
