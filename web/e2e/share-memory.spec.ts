import { expect, test, type Locator, type Page } from "@playwright/test";
import { MEMORY_PATH } from "../src/lib/routes";
import { commonMocks } from "./mission-directions";
import { mockRoster } from "./roster";

/** Share bundles carry project memory (#1714 P4, boards A–E in comment 123445): Library → Memory
 *  exports the open project's notes (counts only for what stays home) and imports a file's notes
 *  into that project, each note judged in the preview and given its own outcome — desktop and a
 *  320 px phone. With memory off the notes wait and are never sent. The backend is mocked; every
 *  share request the SPA sends is asserted. */

const PID = "p-1a2b3c4d";
const NOW = Date.now() / 1000;
const file = (path: string, text: string) => ({ path, size: text.length, text });

function view(enabled = true) {
  return {
    project: { id: PID, name: "Alpha", color: "" },
    enabled,
    notes: [
      {
        slug: "deploy",
        name: "Deploy",
        description: "Our own rule",
        type: "decision",
        body: "Ours.",
        size: 5,
        created: NOW,
        updated: NOW,
        session: { key: "", engine: "", kind: "share", resolved: false },
        links: [],
      },
    ],
    pending: [],
    refused: [],
    quarantine: [],
    withheld: { notes: 0, reason: "" },
    instructions: "Read $BATTLELAB_MEMORY/MEMORY.md.",
    limits: { note_bytes: 4096, notes: 1000, inbox_files: 20 },
    specialist: null,
    notebooks: [],
  };
}

const CATALOG = {
  imports: {},
  items: [
    {
      kind: "memory",
      id: PID,
      title: "Alpha",
      depends_on: [],
      exportable: true,
      reason: null,
      source: "local",
      project: { id: PID, name: "Alpha" },
      memory: { notes: 15, withheld: 2, quarantined: 1 },
    },
    {
      kind: "memory",
      id: "p-99999999",
      title: "Billing",
      depends_on: [],
      exportable: true,
      reason: null,
      source: "local",
      project: { id: "p-99999999", name: "Billing" },
      memory: { notes: 3, withheld: 0, quarantined: 0 },
    },
  ],
};

const NOTES = Array.from({ length: 8 }, (_, n) => ({
  file: `notes/${String(n + 3).padStart(4, "0")}.json`,
  verdict: "new",
  reason: null,
  name: `Note ${n + 3}`,
  type: "finding",
  description: `Finding number ${n + 3}`,
}));

const PREVIEW = {
  token: "d".repeat(32),
  expires_at: "2026-11-09T10:27:00Z",
  bundle: {
    digest: "5b1d" + "0".repeat(56) + "c20e",
    size: 9216,
    format: 1,
    created_at: "2026-11-09T09:12:00Z",
    app_version: "0.29.0",
    source: null,
    items: 1,
    files: 10,
  },
  items: [
    {
      key: "memory:project",
      kind: "memory",
      id: "project",
      title: "Project memory",
      dest: null,
      status: "new",
      reason: null,
      depends_on: [],
      uses: [],
      files: [
        file("notes/0001.json", '{"format": 1, "name": "Deploy", "body": "Staging first."}\n'),
        { path: "notes/0002.json", size: 120, withheld: true },
        ...NOTES.map((n) => file(n.file, `{"format": 1, "name": "${n.name}"}\n`)),
      ],
      memory: {
        notes: [
          { file: "notes/0001.json", verdict: "new", reason: null, name: "Deploy", type: "decision", description: "Staging first." },
          { file: "notes/0002.json", verdict: "held", reason: "looks like a credential" },
          ...NOTES,
        ],
        counts: { new: 9, held: 1, refused: 0 },
      },
    },
  ],
};

type Write = { path: string; body: unknown };

async function setup(page: Page, { enabled = true, failOne = false } = {}) {
  await commonMocks(page);
  await mockRoster(page);
  const writes: Write[] = [];
  await page.route("**/api/memory**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/memory")
      return route.fulfill({
        json: { projects: [{ id: PID, name: "Alpha", color: "", enabled, notes: 1, pending: 0, quarantined: 0, last_write: NOW }] },
      });
    if (path === `/api/memory/${PID}`) return route.fulfill({ json: view(enabled) });
    if (path === `/api/memory/${PID}/ingest`) return route.fulfill({ json: { notes: 0, quarantined: 0, refused: 0, dropped: 0 } });
    if (path === `/api/memory/${PID}/import`) return route.fulfill({ json: { sources: [], last: null, held: [] } });
    return route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  let confirms = 0;
  await page.route("**/api/share/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET")
      writes.push({ path, body: path.endsWith("/preview") ? (request.postDataBuffer()?.length ?? 0) : request.postDataJSON() });
    if (path.endsWith("/catalog")) return route.fulfill({ json: CATALOG });
    if (path.endsWith("/imports")) return route.fulfill({ json: { imports: {} } });
    if (path.endsWith("/export"))
      return route.fulfill({
        body: "bundle-bytes",
        headers: { "content-type": "application/x-tar", "X-Share-Withheld-Notes": "2", "X-Share-Quarantined-Notes": "1" },
      });
    if (path.endsWith("/import/preview")) return route.fulfill({ json: PREVIEW });
    if (path.endsWith("/import")) {
      confirms += 1;
      const failing = failOne && confirms === 1;
      const outcomes = [
        { file: "notes/0001.json", outcome: "created", slug: "deploy-2", reason: null },
        { file: "notes/0002.json", outcome: "held", slug: null, reason: "looks like a credential" },
        ...NOTES.map((n, i) =>
          failing && i === 0
            ? { file: n.file, outcome: "retry", slug: null, reason: "Nothing was written: a storage error (EIO). Confirm again to retry it." }
            : { file: n.file, outcome: "created", slug: `note-${i + 3}`, reason: null },
        ),
      ];
      const count = (o: string) => outcomes.filter((x) => x.outcome === o).length;
      return route.fulfill({
        json: {
          token: PREVIEW.token,
          bundle: PREVIEW.bundle.digest,
          repreview: null,
          items: [
            {
              key: "memory:project",
              kind: "memory",
              id: "project",
              title: "Project memory",
              status: failing ? "failed" : "created",
              dest: PID,
              reason: failing ? "1 of its notes couldn't be taken in now; nothing else was written for them. Confirm again to retry them." : null,
              retry: failing,
              memory: {
                scope: PID,
                notes: outcomes,
                counts: { created: count("created"), already: 0, held: count("held"), refused: 0, failed: 0, retry: count("retry"), pending: 0 },
              },
            },
          ],
        },
      });
    }
    return route.fulfill({ status: 404, json: { detail: "not found" } });
  });
  return writes;
}

test.beforeEach(async ({ page }, testInfo) => {
  if (testInfo.project.name === "mobile") await page.setViewportSize({ width: 320, height: 720 });
});

async function fits(page: Page, dialog: Locator) {
  const box = await dialog.boundingBox();
  const width = page.viewportSize()!.width;
  expect(box!.x).toBeGreaterThanOrEqual(-1);
  expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
  const overflow = await dialog.evaluate((el) => {
    const body = el.querySelector("[class*='body']") as HTMLElement | null;
    return body ? body.scrollWidth - body.clientWidth : 0;
  });
  expect(overflow).toBeLessThanOrEqual(1);
}

async function tall44(locator: Locator) {
  const box = await locator.boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(43.5);
}

async function openPreview(page: Page) {
  await page.goto(MEMORY_PATH);
  await page.getByRole("button", { name: "Import…" }).click();
  const dialog = page.getByTestId("share-import");
  await dialog.getByTestId("share-file").setInputFiles({
    name: "alpha-notes.battlelab",
    mimeType: "application/x-tar",
    buffer: Buffer.from("bundle-bytes"),
  });
  await expect(dialog.getByText("Not signed — imported from a file.")).toBeVisible();
  return dialog;
}

test("export: the open project's notes are chosen, counted, and the download says what stayed home", async ({ page }) => {
  const writes = await setup(page);
  await page.goto(MEMORY_PATH);
  // An imported note reads as such, never as an unknown agent.
  await expect(page.getByText("imported from a file").first()).toBeVisible();
  await page.getByRole("button", { name: "Export…" }).click();
  const dialog = page.getByTestId("share-export");
  const own = dialog.getByTestId(`share-export-row-memory:${PID}`);
  await expect(own).toContainText("15 notes travel · 2 held back by the secret check · 1 quarantined item stays");
  await expect(own.getByRole("checkbox")).toBeChecked();
  await expect(dialog.getByTestId("share-export-row-memory:p-99999999")).toHaveCount(0);
  await expect(dialog.getByText("What memory carries.")).toBeVisible();
  await expect(dialog.getByTestId("share-memory-zip")).toHaveAttribute("href", `/api/memory/${PID}/export`);
  await fits(page, dialog);
  const download = page.waitForEvent("download");
  const button = dialog.getByRole("button", { name: "Download bundle" });
  await tall44(button);
  await button.click();
  await download;
  await expect(dialog.getByRole("status")).toContainText(
    "2 notes were held back by the secret check; 1 quarantined item stayed here.",
  );
  expect(writes).toEqual([{ path: "/api/share/export", body: { items: [{ kind: "memory", id: PID }] } }]);
});

test("import: each note judged, a held note shows no text, the confirm names the project, one failed note is retried alone", async ({
  page,
}) => {
  const writes = await setup(page, { failOne: true });
  const dialog = await openPreview(page);
  await fits(page, dialog);
  await expect(dialog.getByTestId("share-notes-into")).toContainText("Alpha");
  const row = dialog.getByTestId("share-preview-row-memory:project");
  await expect(row).toContainText("9 new · 1 held back → into Alpha");
  const notes = row.getByTestId("share-notes");
  await expect(notes).toContainText("Deploy");
  await expect(notes).toContainText("Name withheld");
  await expect(notes).toContainText("Looks like a credential. It is never stored and never put in a review tray");
  // A long list: the first five, then every note on request.
  await expect(notes.getByRole("listitem")).toHaveCount(5);
  const more = notes.getByRole("button", { name: "Show all 10 notes" });
  await tall44(more);
  await more.click();
  await expect(notes.getByRole("listitem")).toHaveCount(10);
  await expect(dialog.getByTestId("share-blind-ack")).toHaveCount(0); // a held note is never imported
  const importButton = dialog.getByRole("button", { name: "Import 1 item" });
  await importButton.scrollIntoViewIfNeeded();
  await tall44(importButton);
  await importButton.click();
  const result = dialog.getByTestId("share-result-row-memory:project");
  await expect(result.getByTestId("share-notes-counts")).toContainText("8 imported · 1 held back · 1 failed");
  await expect(result).toContainText("Retry failed items tries this note alone.");
  await fits(page, dialog);
  await dialog.getByRole("button", { name: "Retry failed items" }).click();
  await expect(result.getByTestId("share-notes-counts")).toContainText("9 imported · 1 held back");
  const open = result.getByRole("link", { name: /Open/ });
  await tall44(open);
  await expect(open).toHaveAttribute("href", `${MEMORY_PATH}?project=${PID}`);
  const confirms = writes.filter((w) => w.path === "/api/share/import");
  expect(confirms).toHaveLength(2);
  for (const c of confirms)
    expect(c.body).toEqual({ token: PREVIEW.token, selection: ["memory:project"], memory_project: PID });
});

test("import with memory off: the notes wait, say how to turn it on, and nothing is sent", async ({ page }) => {
  const writes = await setup(page, { enabled: false });
  const dialog = await openPreview(page);
  const row = dialog.getByTestId("share-preview-row-memory:project");
  await expect(row).toHaveAttribute("data-state", "refused");
  await expect(row).toContainText("Memory is off for Alpha. Turn it on (the switch on this page)");
  await expect(dialog.getByRole("button", { name: /Import 0 items/ })).toBeDisabled();
  await fits(page, dialog);
  expect(writes.filter((w) => w.path === "/api/share/import")).toEqual([]);
});
