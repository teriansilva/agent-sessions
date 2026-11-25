/** The mission shelf in a real browser (#1740 P1a), desktop and phone, against the server's own
 *  manifest shape with the bytes served from real media fixtures.
 *
 *  What only a browser can show: every kind opens in its typed viewer (an image decodes, video and
 *  audio actually play, a text document is text, a PDF is download-only, a link is an inert new-tab
 *  anchor, a text item is shown whole); changed / missing / inaccessible files stay on the shelf and
 *  never ask for bytes; a verdict set in the lightbox lands on the tile; focus moves into the
 *  lightbox and back to its tile; every control is ≥44 px at ≤800 px, measured at runtime; the phone
 *  page never scrolls sideways (the kind chips scroll in their own strip); and THREAD → SHELF →
 *  THREAD keeps a typed draft, shows what arrived meanwhile, and leaves the composer usable — the
 *  thread is hidden, never unmounted (#948). The real routes are `appmode-mission-shelf.spec.ts`.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";

import { missionLink } from "../src/lib/missionLink";
import { MISSION, EMPTY_SHELF, missionList, missionRow, mockMissions } from "./mission-console";
import { MISSION_STREAM, fulfillTurn } from "./missionStream";
import { TOUCH_FLOOR } from "./touchTarget";

const T = 1_700_000_000;
const MID = "msn_" + "5".repeat(32);
const S1 = "claude:aaaaaaaa-0000-4000-8000-000000000001";
const S2 = "codex:bbbbbbbb-0000-4000-8000-000000000002";
const MEDIA = join(process.cwd(), "e2e", "fixtures", "media");
const PNG = readFileSync(join(MEDIA, "dot.png"));
const WEBM = readFileSync(join(MEDIA, "clip.webm"));
const OGG = readFileSync(join(MEDIA, "tone.ogg"));
const PDF = Buffer.from("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n", "latin1");
const MD = "# Changelog\n\n<script>alert('nope')</script>\n- the hero video\n";

const id = (n: number) => "shf_" + String(n).padStart(32, "0");
type Item = Record<string, unknown> & { id: string; verdict: string | null; note: string };

function items(): Item[] {
  const base = (n: number, over: Record<string, unknown>): Item => ({
    id: id(n),
    seq: n,
    source: "delivered",
    title: `Item ${n}`,
    why: "",
    session_key: n <= 4 ? S1 : S2,
    size: 100,
    sha256: null,
    created_at: T + n * 60,
    verdict: null,
    note: "",
    verdict_at: null,
    ...over,
  });
  const file = (name: string, mime: string | null, extra: Record<string, unknown> = {}) => ({
    ref_type: "path",
    name,
    path: `/repo/out/${name}`,
    mime,
    viewable: mime !== null,
    sha256: "9f2c" + "0".repeat(56) + "41a0",
    file: { state: "ok" },
    ...extra,
  });
  return [
    base(1, { kind: "image", title: "Poster frame", why: "for the release page", ...file("poster.png", "image/png"), size: PNG.length }),
    base(2, { kind: "video", title: "Hero video", why: "the 30-second cut", ...file("hero.webm", "video/webm"), size: WEBM.length }),
    base(3, { kind: "audio", title: "Voice-over, take 3", ...file("vo.ogg", "audio/ogg"), size: OGG.length }),
    base(4, { kind: "document", title: "Changelog", ...file("CHANGELOG.md", "text/plain; charset=utf-8"), size: MD.length }),
    base(5, { kind: "document", title: "Release notes PDF", ...file("notes.pdf", null), size: PDF.length }),
    base(6, { kind: "link", title: "Staging preview", ref_type: "url", url: "https://staging.example.org/releases/v0-21", host: "staging.example.org", url_path: "/releases/v0-21", size: null }),
    base(7, { kind: "text", title: "Finding: old badge", ref_type: "text", text: "The BETA badge is the old style.\n<b>Re-shoot</b> from staging.", size: 60 }),
    base(8, { kind: "image", title: "Shelf page, dark", ...file("shelf-dark.png", "image/png", { file: { state: "missing" } }) }),
    base(9, { kind: "image", title: "Private shot", ...file("private.png", "image/png", { file: { state: "inaccessible" } }) }),
    base(10, {
      kind: "image",
      title: "Map view, dark",
      ...file("map.png", "image/png", {
        file: { state: "changed", disk_sha256: "77ab" + "1".repeat(56) + "0e93", disk_mtime: T + 900 },
      }),
      size: PNG.length,
    }),
  ];
}

function manifest(list: Item[], revision: number) {
  const by_kind = { ...EMPTY_SHELF.counts.by_kind } as Record<string, number>;
  for (const i of list) by_kind[i.kind as string] += 1;
  return {
    ...EMPTY_SHELF,
    mission_id: MID,
    revision,
    counts: {
      total: list.length,
      by_kind,
      by_source: { delivered: list.length, found: 0 },
      unreviewed: list.filter((i) => i.verdict === null).length,
      file_checks: { changed: 1, missing: 1, inaccessible: 1 },
    },
    items: list,
    next_cursor: null,
  };
}

const BYTES: Record<string, [Buffer, string]> = {
  [id(1)]: [PNG, "image/png"],
  [id(2)]: [WEBM, "video/webm"],
  [id(3)]: [OGG, "audio/ogg"],
  [id(4)]: [Buffer.from(MD), "text/plain; charset=utf-8"],
  [id(5)]: [PDF, "application/octet-stream"],
  [id(10)]: [PNG, "image/png"],
};

interface Shelf {
  list: Item[];
  revision: number;
  verdicts: Record<string, unknown>[];
  raw: string[];
  events: unknown[];
  /** The mission's state the NEXT detail read answers with. */
  state: string;
  /** Detail reads answered with `review`. */
  reviewReads: number;
  /** Sessions whose found-in-git items are still to come. */
  pending: string[];
  /** The records revision the stored debrief summary was written from (#1740 P2). */
  summaryRev: string;
  /** Debrief reads and regenerations, in order. */
  debriefCalls: string[];
}

/** The debrief the server would answer for this shelf: the facts read now (their revision follows
 *  the shelf's), and the stored summary with the revision it was written from. */
function debriefBody(shelf: Shelf) {
  const counts = manifest(shelf.list, shelf.revision).counts;
  const rev = `r${shelf.revision}`;
  const needs = shelf.list.filter((i) => i.verdict === "needs_work");
  return {
    mission_id: MID,
    state: shelf.state,
    configured: true,
    facts: {
      mission_id: MID,
      title: "Launch video for v0.21",
      state: shelf.state,
      asked: "Produce the launch video package for v0.21",
      sessions: [
        { key: S1, role: "primary", added_at: T, removed_at: T + 6000 },
        { key: S2, role: "sub", added_at: T, removed_at: T + 9000 },
      ],
      objectives: [
        { key: "o1", title: "A 30-second hero video is rendered at 1080p", state: "met", met: true,
          judged: { met: true, confidence: 0.96, checked_at: T, evidence: [{ source: `transcript:${S1}`, quote: "Rendered out/hero-v021.webm — 1920×1080, 00:31, 24 fps." }] } },
        { key: "o2", title: "Voice-over matches the approved script", state: "pending", met: false,
          judged: { met: false, confidence: 0.64, checked_at: T, evidence: [] } },
      ],
      shelf: { counts, groups: [{ session_key: S1, total: 4, by_kind: {} }, { session_key: S2, total: shelf.list.length - 4, by_kind: {} }] },
      open: {
        objectives: [{ key: "o2", title: "Voice-over matches the approved script", state: "pending", judged: null }],
        objectives_total: 1,
        needs_work: needs.map((i) => ({ id: i.id, kind: i.kind, source: i.source, title: i.title, session_key: i.session_key, note: i.note })),
        needs_work_total: needs.length,
        found_not_delivered: [],
        found_not_delivered_total: 0,
      },
      integrity: { changed: [], missing: [] },
      revision: rev,
    },
    summary: { text: "Claude rendered the hero video and recorded a voice-over; Codex captured the screenshots.", revision: shelf.summaryRev, written_at: T + 9100 },
    fresh: shelf.summaryRev === rev,
    generating: false,
    error: null,
  };
}

async function setup(page: Page, initial = "review"): Promise<Shelf> {
  const shelf: Shelf = { list: items(), revision: 10, verdicts: [], raw: [], events: [], state: initial, reviewReads: 0, pending: [], summaryRev: "r10", debriefCalls: [] };
  const state = initial;
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/templates**", (r) => r.fulfill({ json: { templates: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cards: [
          { id: S1, engine: "claude", title: "Render the hero video", mission_id: MID, state: "idle", live: false, project: { kind: "folder", id: "/repo", name: "/repo" } },
          { id: S2, engine: "codex", title: "Screenshots and changelog", mission_id: MID, state: "idle", live: false, project: { kind: "folder", id: "/repo", name: "/repo" } },
        ],
        generated_at: T,
        window_days: 3,
      },
    }),
  );
  const detail = () => {
    if (shelf.state === "review") shelf.reviewReads += 1;
    return detailBody();
  };
  const detailBody = () => ({
    ...MISSION,
    id: MID,
    title: "Launch video for v0.21",
    state: shelf.state,
    cwd: "/repo",
    sessions: [
      { mission_id: MID, session_key: S1, role: "primary", added_at: T, removed_at: null },
      { mission_id: MID, session_key: S2, role: "sub", added_at: T, removed_at: null },
    ],
    events: shelf.events,
    events_next_seq: null,
  });
  await mockMissions(page, {
    missions: missionList([missionRow({ id: MID, title: "Launch video for v0.21", state })]),
    mission: detail(),
  });
  await page.route(new RegExp(`/api/missions/${MID}(\\?.*)?$`), (r) => r.fulfill({ json: detail() }));
  await page.route(`**/api/missions/${MID}/debrief`, (r) => {
    const post = r.request().method() === "POST";
    shelf.debriefCalls.push(post ? "regenerate" : "read");
    if (post) shelf.summaryRev = `r${shelf.revision}`;
    return r.fulfill({ json: debriefBody(shelf) });
  });
  await page.route(`**/api/missions/${MID}/shelf`, (r) =>
    r.fulfill({ json: { ...manifest(shelf.list, shelf.revision), found_pending: shelf.pending } }),
  );
  await page.route(`**/api/missions/${MID}/shelf/*/diff`, (r) =>
    r.fulfill({
      json: {
        id: id(11),
        files_more: 0,
        files: [
          { path: "src/timeline.ts", status: "A", add: 2, del: 0, from: "commit", truncated: false, unavailable: false,
            patch: "--- /dev/null\n+++ b/src/timeline.ts\n@@ -0,0 +1,2 @@\n+<script>alert(1)</script>\n+export const FPS = 24;\n" },
          { path: "old.txt", status: "D", add: 0, del: 1, from: "worktree", truncated: false, unavailable: false, patch: "" },
        ],
      },
    }),
  );
  await page.route(`**/api/missions/${MID}/shelf/*`, async (r) => {
    if (r.request().method() !== "PATCH") return r.fallback();
    const itemId = new URL(r.request().url()).pathname.split("/").pop()!;
    const body = r.request().postDataJSON() as Record<string, unknown>;
    shelf.verdicts.push(body);
    if (body.expected_revision !== shelf.revision) {
      return r.fulfill({ status: 409, json: { detail: "moved", revision: shelf.revision } });
    }
    shelf.revision += 1;
    const it = shelf.list.find((i) => i.id === itemId)!;
    it.verdict = body.verdict as string;
    it.note = body.note as string;
    it.verdict_at = T + 2000;
    return r.fulfill({ json: { revision: shelf.revision, item: it } });
  });
  await page.route(`**/api/missions/${MID}/shelf/*/raw**`, async (r) => {
    const u = new URL(r.request().url());
    const itemId = u.pathname.split("/").slice(-2)[0];
    const download = u.searchParams.get("download") === "1";
    // Recorded BEFORE any answer, so a request for a gone file is seen too.
    shelf.raw.push(download ? `${itemId}:download` : itemId);
    const hit = BYTES[itemId];
    if (!hit) return r.fulfill({ status: 404, json: { detail: "the file is no longer on disk" } });
    const [body, type] = hit;
    const range = r.request().headers()["range"];
    const headers: Record<string, string> = {
      "content-type": download ? "application/octet-stream" : type,
      "content-disposition": `${download ? "attachment" : "inline"}; filename*=UTF-8''x`,
      "x-content-type-options": "nosniff",
      "cache-control": "no-store",
      "accept-ranges": "bytes",
    };
    const m = range ? /bytes=(\d+)-(\d*)/.exec(range) : null;
    if (m) {
      const start = Number(m[1]);
      const end = m[2] ? Math.min(Number(m[2]), body.length - 1) : body.length - 1;
      return r.fulfill({
        status: 206,
        headers: { ...headers, "content-range": `bytes ${start}-${end}/${body.length}` },
        body: body.subarray(start, end + 1),
      });
    }
    return r.fulfill({ status: 200, headers, body });
  });
  return shelf;
}

const tile = (page: Page, n: number) => page.locator(`[data-shelf-tile="${id(n)}"]`);
const lightbox = (page: Page) => page.getByRole("dialog");

async function openShelf(page: Page) {
  await page.goto(missionLink(MID));
  await expect(page.getByRole("tab", { name: /Shelf/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("mission-shelf")).toBeVisible();
}

test("a mission in review opens on its shelf: every kind, grouped by session", async ({ page }) => {
  await setup(page);
  await openShelf(page);
  await expect(page.getByRole("tab", { name: /Shelf/ })).toContainText("10");
  await expect(page.getByRole("tab", { name: /Shelf/ })).toContainText("10 to review");
  await expect(page.locator("[data-shelf-tile]")).toHaveCount(10);
  await expect(page.getByRole("region", { name: "Render the hero video" }).locator("[data-shelf-tile]")).toHaveCount(4);
  await expect(page.getByRole("region", { name: "Screenshots and changelog" }).locator("[data-shelf-tile]")).toHaveCount(6);
  // The image tile decodes the real bytes as its thumbnail.
  const thumb = tile(page, 1).locator("[data-shelf-thumb]");
  await expect.poll(() => thumb.evaluate((el: HTMLImageElement) => el.naturalWidth), { timeout: 20_000 }).toBe(64);
  await expect(tile(page, 8)).toContainText("File not found");
  await expect(tile(page, 8)).toContainText("Missing");
  await expect(tile(page, 9)).toContainText("Not accessible");
  await expect(tile(page, 10)).toContainText(/Changed/);
  // Text stays text: the agent's markup is shown, never made.
  await expect(tile(page, 7)).toContainText("<b>Re-shoot</b>");
  await expect(tile(page, 7).locator("b")).toHaveCount(0);
});

test("each kind opens in its own viewer", async ({ page }) => {
  const shelf = await setup(page);
  await openShelf(page);

  await tile(page, 1).click();
  const img = lightbox(page).locator("[data-media-el='image']");
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth), { timeout: 20_000 }).toBe(64);
  await expect(lightbox(page)).toContainText("Matches delivery");

  await page.keyboard.press("ArrowRight");
  await expect(lightbox(page)).toHaveAccessibleName("Hero video");
  const video = lightbox(page).locator("[data-media-el='video']");
  await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.readyState), { timeout: 20_000 }).toBeGreaterThan(0);
  await video.evaluate((el: HTMLVideoElement) => {
    el.muted = true;
    return el.play();
  });
  await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.currentTime), { timeout: 20_000 }).toBeGreaterThan(0);

  await page.keyboard.press("ArrowRight");
  await expect(lightbox(page)).toHaveAccessibleName("Voice-over, take 3");
  const audio = lightbox(page).locator("[data-media-el='audio']");
  await expect.poll(() => audio.evaluate((el: HTMLAudioElement) => el.readyState), { timeout: 20_000 }).toBeGreaterThan(0);
  await audio.evaluate((el: HTMLAudioElement) => {
    el.muted = true;
    return el.play();
  });
  await expect.poll(() => audio.evaluate((el: HTMLAudioElement) => el.currentTime), { timeout: 20_000 }).toBeGreaterThan(0);

  await lightbox(page).getByRole("button", { name: "Close" }).click();
  await tile(page, 4).click();
  const doc = lightbox(page).locator("[data-shelf-text-doc]");
  await expect(doc).toContainText("<script>alert('nope')</script>");
  await expect(doc.locator("script")).toHaveCount(0);

  await page.keyboard.press("ArrowRight");
  await expect(lightbox(page)).toHaveAccessibleName("Release notes PDF");
  await expect(lightbox(page).locator("[data-shelf-download-only]")).toBeVisible();
  const download = lightbox(page).locator("[data-shelf-download-only] [data-shelf-download]");
  await expect(download).toHaveAttribute("href", `/api/missions/${MID}/shelf/${id(5)}/raw?download=1`);
  const [file] = await Promise.all([page.waitForEvent("download"), download.click()]);
  expect(await file.suggestedFilename()).toBe("notes.pdf");
  // Nothing ever asked for the PDF inline.
  expect(shelf.raw).not.toContain(id(5));

  await page.keyboard.press("ArrowRight");
  await expect(lightbox(page)).toHaveAccessibleName("Staging preview");
  const link = lightbox(page).locator("[data-shelf-link]");
  await expect(link).toHaveAttribute("href", "https://staging.example.org/releases/v0-21");
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");

  await page.keyboard.press("ArrowRight");
  await expect(lightbox(page)).toHaveAccessibleName("Finding: old badge");
  await expect(lightbox(page).locator("[data-shelf-text-item]")).toHaveText(
    "The BETA badge is the old style.\n<b>Re-shoot</b> from staging.",
  );
});

test("changed, missing and inaccessible files stay on the shelf and never ask for bytes", async ({ page }) => {
  const shelf = await setup(page);
  await openShelf(page);
  await tile(page, 10).click();
  await expect(lightbox(page).locator("[data-shelf-check='changed']")).toHaveText("Changed since delivered");
  await expect(lightbox(page)).toContainText("9f2c…41a0");
  await expect(lightbox(page)).toContainText("77ab…0e93");
  await lightbox(page).getByRole("button", { name: "Close" }).click();
  for (const n of [8, 9]) {
    await tile(page, n).click();
    await expect(lightbox(page).locator("[data-shelf-gone]")).toBeVisible();
    await expect(lightbox(page).locator("img, video, audio")).toHaveCount(0);
    await lightbox(page).getByRole("button", { name: "Close" }).click();
  }
  expect(shelf.raw).not.toContain(id(8));
  expect(shelf.raw).not.toContain(id(9));
});

test("a verdict is set in the lightbox and lands on its tile with its note", async ({ page }, testInfo) => {
  const shelf = await setup(page);
  await openShelf(page);
  await tile(page, 1).click();
  await lightbox(page).getByLabel("Note · optional").fill("Re-export from the final render");
  await lightbox(page).getByRole("radio", { name: "Needs work" }).click();
  await expect(lightbox(page).getByRole("radio", { name: "Needs work" })).toHaveAttribute("aria-checked", "true");
  await expect(lightbox(page).locator("[data-shelf-saved]")).toBeVisible();
  expect(shelf.verdicts).toEqual([
    { expected_revision: 10, verdict: "needs_work", note: "Re-export from the final render" },
  ]);
  await page.keyboard.press("Escape");
  await expect(tile(page, 1)).toContainText("Needs work");
  await expect(tile(page, 1)).toContainText("Re-export from the final render");
  await expect(page.locator("[data-shelf-unreviewed]")).toHaveText("9");
  // An agent delivering meanwhile moves the revision; the verdict still lands (re-read, re-sent).
  shelf.revision += 1;
  await tile(page, 2).click();
  await lightbox(page).getByRole("radio", { name: "Looks good" }).click();
  await expect(lightbox(page).getByRole("radio", { name: "Looks good" })).toHaveAttribute("aria-checked", "true");
  expect(shelf.verdicts.slice(1).map((v) => v.expected_revision)).toEqual([11, 12]);
  void testInfo;
});

test("focus moves into the lightbox and back to its tile", async ({ page }) => {
  await setup(page);
  await openShelf(page);
  await tile(page, 7).focus();
  await page.keyboard.press("Enter");
  await expect(lightbox(page)).toBeVisible();
  await expect(lightbox(page).getByRole("button", { name: "Close" })).toBeFocused();
  // Tab stays inside the dialog.
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press("Tab");
    expect(await page.evaluate(() => !!document.activeElement?.closest("[role=dialog]"))).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(lightbox(page)).toHaveCount(0);
  await expect(tile(page, 7)).toBeFocused();
  // The scrim closes it too (a desktop frame; on a phone the sheet covers the whole screen).
  if ((page.viewportSize()?.width ?? 0) > 800) {
    await tile(page, 1).click();
    await page.mouse.click(5, 5);
    await expect(lightbox(page)).toHaveCount(0);
    await expect(tile(page, 1)).toBeFocused();
  }
});

test("THREAD → SHELF → THREAD keeps the draft, shows what arrived, and the composer still sends", async ({ page }) => {
  const shelf = await setup(page, "running");
  const sent: unknown[] = [];
  await page.route(MISSION_STREAM, async (r) => {
    sent.push(r.request().postDataJSON());
    shelf.events = [
      { seq: 2, mission_id: MID, at: T + 10, kind: "assistant_msg", session_key: null, action_id: null, text: "The hero video is on the shelf.", meta: null, settlement: null },
      { seq: 1, mission_id: MID, at: T + 5, kind: "operator_msg", session_key: null, action_id: null, text: "where is the video?", meta: null, settlement: null },
    ];
    return fulfillTurn(r, { turn_id: "t1", state: "done", answer: "The hero video is on the shelf.", matches: [] });
  });
  await page.goto(missionLink(MID));
  // A running mission opens on THREAD.
  await expect(page.getByRole("tab", { name: "Thread" })).toHaveAttribute("aria-selected", "true");
  const thread = page.getByTestId("pane");
  await expect(thread).toBeVisible();
  // The SAME thread element throughout: hidden under SHELF, never unmounted and remounted.
  const threadNode = await thread.elementHandle();
  const input = page.getByTestId("composer-input");
  const node = await input.elementHandle();
  await input.fill("half a thought");

  await page.getByRole("tab", { name: /Shelf/ }).click();
  await expect(page.getByTestId("mission-shelf")).toBeVisible();
  await expect(thread).toBeHidden();
  // The thread is HIDDEN, not unmounted; the composer is docked under the shelf too.
  expect(await threadNode!.evaluate((el) => el.isConnected)).toBe(true);
  await expect(input).toBeVisible();
  expect(await node!.evaluate((el) => (el as HTMLTextAreaElement).isConnected && (el as HTMLTextAreaElement).value)).toBe(
    "half a thought",
  );

  // Send from under the shelf: the turn's events arrive while the thread is hidden.
  await input.fill("where is the video?");
  await page.getByTestId("composer-send").click();
  await expect.poll(() => sent.length).toBe(1);
  await expect(thread).toBeHidden();
  await expect.poll(() => thread.textContent(), { timeout: 20_000 }).toContain("The hero video is on the shelf.");

  await page.getByRole("tab", { name: "Thread" }).click();
  await expect(thread).toBeVisible();
  expect(await threadNode!.evaluate((el) => el.isConnected)).toBe(true);
  await expect(thread).toContainText("where is the video?");
  await expect(thread).toContainText("The hero video is on the shelf.");
  await expect(page.getByTestId("mission-shelf")).toHaveCount(0);
  await input.fill("and the audio?");
  await expect(input).toHaveValue("and the audio?");
  await expect(page.getByTestId("composer-send")).toBeEnabled();
});

test("targets are ≥44 px at ≤800 px and the phone page never scrolls sideways", async ({ page }, testInfo) => {
  await setup(page);
  if (testInfo.project.name === "desktop") await page.setViewportSize({ width: 800, height: 900 });
  await openShelf(page);
  const measure = async (sel: string) =>
    page.locator(sel).evaluateAll((els) =>
      els
        .filter((e) => (e as HTMLElement).offsetParent !== null)
        .map((e) => {
          const r = e.getBoundingClientRect();
          return { what: (e.getAttribute("aria-label") ?? e.textContent ?? "").trim().slice(0, 40), h: r.height, w: r.width };
        }),
    );
  const shelfTargets = await measure(
    "[role=tab], [data-testid=mission-shelf] button, [data-testid=mission-shelf] a[href]",
  );
  expect(shelfTargets.length).toBeGreaterThan(10);
  for (const t of shelfTargets) expect(t.h, t.what).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // The kind chips scroll in their own strip; the page does not.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await tile(page, 1).click();
  const boxTargets = await measure("[role=dialog] button, [role=dialog] a[href], [role=dialog] textarea");
  expect(boxTargets.length).toBeGreaterThan(3);
  for (const t of boxTargets) {
    expect(t.h, t.what).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(t.w, t.what).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  const lbOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(lbOverflow).toBeLessThanOrEqual(0);
});

test("an empty shelf says what will arrive", async ({ page }) => {
  const shelf = await setup(page);
  shelf.list = [];
  shelf.revision = 0;
  await openShelf(page);
  await expect(page.locator("[data-shelf-empty]")).toContainText("Nothing on the shelf yet");
});

test("the opening view is latched, and the hidden thread keeps its scroll position", async ({ page }) => {
  const shelf = await setup(page, "running");
  shelf.events = Array.from({ length: 40 }, (_, i) => ({
    seq: 40 - i, mission_id: MID, at: T + (40 - i) * 60, kind: "operator_msg", session_key: null,
    action_id: null, text: `message ${40 - i}`, meta: null, settlement: null,
  }));
  await page.route(MISSION_STREAM, async (r) => {
    shelf.state = "review"; // the mission moved into review while the operator was on its thread
    return fulfillTurn(r, { turn_id: "t2", state: "done", answer: "ok", matches: [] });
  });
  await page.goto(missionLink(MID));
  const thread = page.getByTestId("pane");
  await expect(thread).toContainText("message 40");
  await page.getByTestId("composer-input").fill("status?");
  await page.getByTestId("composer-send").click();
  await expect.poll(() => shelf.state).toBe("review");
  // The reload after the turn reads `review`; the page stays on THREAD (it opened there).
  await expect.poll(() => shelf.reviewReads).toBeGreaterThan(0);
  await expect(page.getByRole("tab", { name: "Thread" })).toHaveAttribute("aria-selected", "true");
  const max = await thread.evaluate((el) => el.scrollHeight - el.clientHeight);
  expect(max).toBeGreaterThan(200);
  await thread.evaluate((el) => (el.scrollTop = 120));
  await page.getByRole("tab", { name: /Shelf/ }).click();
  await expect(page.getByTestId("mission-shelf")).toBeVisible();
  await page.getByRole("tab", { name: "Thread" }).click();
  await expect.poll(() => thread.evaluate((el) => Math.round(el.scrollTop))).toBe(120);
});

function codeItem(): Item {
  return {
    id: id(11),
    seq: 11,
    source: "found",
    kind: "code",
    title: "render: add the hero timeline",
    why: "",
    session_key: S1,
    sessions: [S1, S2],
    ref_type: "code",
    size: null,
    sha256: null,
    created_at: T + 900,
    verdict: null,
    note: "",
    verdict_at: null,
    meta: {
      repo: "/repo", repo_name: "battlelab-site", branch: "devopsagent/launch", base: "1".repeat(40),
      head: "3e9c1f2" + "0".repeat(33), commits: [{ sha: "3e9c1f2" + "0".repeat(33), subject: "render: add the hero timeline" }],
      commits_more: false,
      files: [
        { path: "src/timeline.ts", status: "A", add: 2, del: 0, from: "commit" },
        { path: "old.txt", status: "D", add: 0, del: 1, from: "worktree" },
      ],
      files_more: 0, add: 2, del: 1,
      pr: { number: 418, state: "open", url: "https://git.example.org/o/r/pulls/418", merged: false },
      late: false, sessions: [S1, S2],
    },
  };
}

test("found in git: the code tile, its diff as text, its PR, and the placeholder while a session runs", async ({ page }) => {
  const shelf = await setup(page);
  shelf.list.push(codeItem());
  shelf.pending = [S2];
  await openShelf(page);
  const placeholder = page.locator(`[data-shelf-found-pending="${S2}"]`);
  await expect(placeholder).toContainText("Collected when this session ends");
  const t = tile(page, 11);
  await expect(t).toContainText("battlelab-site");
  await expect(t).toContainText("+2");
  await expect(t).toContainText("2 files");
  await expect(t).toContainText("Touched by 2 sessions");
  await t.click();
  const box = lightbox(page);
  const diff = box.locator("[data-shelf-diff='src/timeline.ts']");
  await expect(diff).toContainText("+<script>alert(1)</script>");
  await expect(diff.locator("script")).toHaveCount(0);
  const pr = box.locator("[data-shelf-pr]");
  await expect(pr).toHaveAttribute("href", "https://git.example.org/o/r/pulls/418");
  await expect(pr).toHaveAttribute("target", "_blank");
  await expect(pr).toHaveAttribute("rel", "noopener noreferrer");
  await box.locator("[data-shelf-code-file='old.txt']").click();
  await expect(box.locator("[data-shelf-diff='old.txt']")).toContainText("Deleted — nothing to show.");
  // The file list's targets hold the floor on a phone too.
  if ((page.viewportSize()?.width ?? 0) <= 800) {
    const h = await box.locator("[data-shelf-code-file]").first().evaluate((el) => el.getBoundingClientRect().height);
    expect(h).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await box.getByRole("radio", { name: "Looks good" }).click();
  await expect(box.getByRole("radio", { name: "Looks good" })).toHaveAttribute("aria-checked", "true");
});

test("the debrief opens a mission in review, and says so the moment the records move past its summary", async ({ page }) => {
  const shelf = await setup(page);
  await openShelf(page);
  const debrief = page.getByTestId("mission-debrief");
  await expect(debrief).toBeVisible();
  await expect(debrief).toHaveAttribute("data-fresh", "");
  await expect(debrief.locator("[data-debrief-summary]")).toContainText("Claude rendered the hero video");
  await expect(debrief.getByText("“Rendered out/hero-v021.webm — 1920×1080, 00:31, 24 fps.”")).toBeVisible();
  await expect(debrief.locator("[data-debrief-open]")).toHaveCount(3);
  await expect(debrief.locator("[data-debrief-stale]")).toHaveCount(0);
  // The debrief sits ABOVE the shelf's own head, first on the page.
  const [db, head] = await Promise.all([
    debrief.boundingBox(),
    page.locator("[data-shelf-total]").boundingBox(),
  ]);
  expect(db!.y).toBeLessThan(head!.y);

  // A verdict moves the records: the summary is out of date, and the current lists are beside it.
  await tile(page, 3).click();
  await lightbox(page).getByRole("radio", { name: "Needs work" }).first().click();
  await expect.poll(() => shelf.verdicts.length).toBe(1);
  await page.keyboard.press("Escape");
  await expect(debrief.locator("[data-debrief-stale]")).toContainText("records changed after this summary was written");
  await expect(debrief).not.toHaveAttribute("data-fresh", "");
  await expect(debrief.locator('[data-debrief-open="needs_work"] [data-debrief-open-count]')).toHaveText("1");

  // Regenerate writes from the records now: fresh again.
  await debrief.getByRole("button", { name: /Regenerate/ }).click();
  await expect(debrief.locator("[data-debrief-stale]")).toHaveCount(0);
  await expect(debrief).toHaveAttribute("data-fresh", "");
  expect(shelf.debriefCalls).toContain("regenerate");
});

test("the debrief collapses to one line once read, and Review shows the unreviewed items", async ({ page }) => {
  await setup(page);
  await openShelf(page);
  const debrief = page.getByTestId("mission-debrief");
  await debrief.getByRole("button", { name: /Review \d+ items/ }).click();
  await expect(debrief).toHaveAttribute("data-collapsed", "");
  await expect(debrief.locator("[data-debrief-line]")).toContainText("1 of 2 objectives met");
  await expect(page.getByRole("button", { name: "Unreviewed only" })).toHaveAttribute("aria-pressed", "true");
  // Read is remembered: opening the mission again shows it collapsed.
  await page.goto("about:blank");
  await openShelf(page);
  await expect(page.getByTestId("mission-debrief")).toHaveAttribute("data-collapsed", "");
  await page.getByTestId("mission-debrief").getByRole("button", { name: "Expand" }).click();
  await expect(page.getByTestId("mission-debrief").locator("[data-debrief-asked]")).toBeVisible();
});

test("a running mission has no debrief", async ({ page }) => {
  const shelf = await setup(page, "running");
  await page.goto(missionLink(MID));
  await page.getByRole("tab", { name: /Shelf/ }).click();
  await expect(page.getByTestId("mission-shelf")).toBeVisible();
  await expect(page.getByTestId("mission-debrief")).toHaveCount(0);
  expect(shelf.debriefCalls).toEqual([]);
});
