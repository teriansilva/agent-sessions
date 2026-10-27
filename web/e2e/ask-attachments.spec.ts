/** Shared files in the HQ chat (#1707), in a real browser, desktop and mobile — the red→green
 *  repro for the feature's UI.
 *
 *  What a DOM emulator cannot prove: that the picker's `setInputFiles` really stages a pill whose
 *  bytes go up through the app's fetch seam and come back as an INLINE image (an object URL, never
 *  a native `/api/…` src), that the text card's View opens a scrollable excerpt and its Download
 *  is a real download, that the three card kinds render identically for the just-sent turn and on
 *  a reload (the conversation is durable), that a failed pill blocks Send AND Enter until the
 *  labeled removal, and that a long file name never scrolls a phone sideways. The camera input is
 *  the OS capture picker (`accept="image/*" capture="environment"`) — no getUserMedia prompt.
 *
 *  On `main` this spec is RED: the composer has no Attach/Camera controls and the thread renders
 *  no attachment cards. It goes green with the feature and stays as its guard.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk, pickConversation } from "./askSidebar";
import {
  mockAskConversations,
  turnOf,
  type FakeAsk,
  type FakeConversation,
} from "./askStream";

const CID = "ask_" + "a".repeat(32);
const AID_IMG = "b".repeat(32);
const AID_TXT = "c".repeat(32);
const AID_PDF = "d".repeat(32);
const AID_GONE = "e".repeat(32);

const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64",
);
const LOG = "2026-10-26T09:12:04Z ERROR migration 3/7: relation does not exist\n";

function att(id: string, name: string, kind: string, size: number) {
  return { id, name, kind, size };
}

/** A conversation whose one turn shared an image, a log and a pdf. */
function seeded(): FakeConversation {
  return {
    id: CID,
    created_at: 10,
    updated_at: 30,
    turns: [
      turnOf("Here is the screenshot and the log tail", {
        answer: "The log tail says it: step 3 expects a missing index.",
        attachments: [
          att(AID_IMG, "rack-error.png", "image", PNG.length),
          att(AID_TXT, "deploy.log", "text", LOG.length),
          att(AID_PDF, "release-checklist.pdf", "other", 84_000),
          att(AID_GONE, "gone.pdf", "other", 1_234),
        ],
      }),
    ],
  };
}

async function mockShell(page: Page): Promise<FakeAsk> {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
        configured: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        total: 0,
        next_offset: null,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } },
    }),
  );
  const fake = await mockAskConversations(page, [seeded()]);
  // The three attachment endpoints (#1707): ingest, and the two bytes shapes.
  await page.route("**/api/ask/conversations/*/attachments", async (r) => {
    if (r.request().method() !== "POST") return r.fulfill({ json: {} });
    return r.fulfill({
      status: 201,
      json: {
        attachment: att("9".repeat(32), "shot.png", "image", PNG.length),
      },
    });
  });
  await page.route(/\/api\/ask\/conversations\/[^/]+\/attachments\/[^/?]+(\?|$)/, (r) => {
    const aid = r.request().url().split("/").pop()!.split("?")[0];
    const download = r.request().url().includes("download=1");
    const meta = r.request().url().includes("meta=1");
    if (aid === AID_GONE) return r.fulfill({ status: 404, json: { detail: "that file is gone" } });
    if (meta)
      return r.fulfill({
        json: {
          attachment: att(
            aid,
            aid === AID_IMG ? "rack-error.png" : aid === AID_TXT ? "deploy.log" : "release-checklist.pdf",
            aid === AID_IMG ? "image" : aid === AID_TXT ? "text" : "other",
            aid === AID_PDF ? 84_000 : PNG.length,
          ),
        },
      });
    if (aid === AID_IMG)
      return r.fulfill({
        status: 200,
        contentType: "image/png",
        headers: {
          "content-disposition": download ? "attachment" : "inline",
          "x-content-type-options": "nosniff",
          "cache-control": "no-store",
        },
        body: PNG,
      });
    if (aid === AID_TXT)
      return r.fulfill({
        status: 200,
        contentType: "text/plain",
        headers: { "x-content-type-options": "nosniff" },
        body: LOG,
      });
    return r.fulfill({ status: 404, json: { detail: "that file is gone" } });
  });
  return fake;
}

test("a shared image renders inline, a log is viewable and everything else a Download card", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/");
  const panel = await openAsk(page);
  // HQ opens on an EMPTY conversation; the kept one is a switcher pick (#1443).
  await pickConversation(panel);
  const cards = panel.getByTestId("ask-attachment");
  await expect(cards).toHaveCount(4);

  // the image: inline bytes through the app's fetch seam (an object URL img), never a raw /api src
  const img = panel.getByTestId("ask-attachment-img");
  await expect(img).toBeVisible();
  await expect(img).toHaveAttribute("alt", "rack-error.png");
  const src = (await img.getAttribute("src")) ?? "";
  expect(src.startsWith("blob:")).toBe(true);

  // the text card: collapsed first, View opens a scrollable excerpt, Download is a real download
  const txt = cards.filter({ hasText: "deploy.log" });
  await expect(panel.getByTestId("ask-attachment-download").first()).toBeVisible();
  await expect(panel.getByTestId("ask-attachment-preview")).toHaveCount(0);
  await txt.getByTestId("ask-attachment-view").click();
  const preview = panel.getByTestId("ask-attachment-preview");
  await expect(preview).toBeVisible();
  await expect(preview).toContainText("relation does not exist");
  // the excerpt is bounded but scrollable — the box exists and has a scroll affordance
  expect(await preview.evaluate((el) => getComputedStyle(el).overflowY)).toBe("auto");

  // the pdf: Download only, no preview surface at all
  const pdf = cards.filter({ hasText: "release-checklist.pdf" });
  await expect(pdf.getByTestId("ask-attachment-download")).toBeVisible();
  await expect(pdf.getByTestId("ask-attachment-preview")).toHaveCount(0);
  await expect(pdf.getByTestId("ask-attachment-img")).toHaveCount(0);

  // the gone file (verified 404 from the tiny probe): the name and *gone*, NO Download — for
  // every kind, a download-only one included (#1720 review).
  const gone = cards.filter({ hasText: "gone.pdf" });
  await expect(gone).toContainText("gone");
  await expect(gone.getByTestId("ask-attachment-download")).toHaveCount(0);
});

test("the cards survive a reload — the conversation is durable", async ({ page }) => {
  await mockShell(page);
  await page.goto("/");
  const first = await openAsk(page);
  await pickConversation(first);
  await expect(page.getByTestId("ask-attachment")).toHaveCount(4);
  await page.reload();
  const panel = await openAsk(page);
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-attachment")).toHaveCount(4);
  await expect(panel.getByTestId("ask-attachment-img")).toHaveAttribute(
    "alt",
    "rack-error.png",
  );
});

test("attach stages a pill, Send carries it, and the camera picker is the OS capture picker", async ({
  page,
}) => {
  const fake = await mockShell(page);
  await page.goto("/");
  const panel = await openAsk(page);

  // the camera input is the capture picker — no getUserMedia permission surface to manage
  const cam = panel.getByTestId("ask-camera-input");
  await expect(cam).toHaveAttribute("accept", "image/*");
  await expect(cam).toHaveAttribute("capture", "environment");

  // stage one through the real file input; the pill appears and settles on Ready
  await panel.getByTestId("ask-attach-input").setInputFiles({
    name: "shot.png",
    mimeType: "image/png",
    buffer: PNG,
  });
  const pill = panel.getByTestId("ask-staged-pill");
  await expect(pill).toHaveCount(1);
  await expect(pill).toContainText("shot.png");
  await expect(pill.getByTestId("ask-staged-state")).toHaveText("Ready");

  await panel.getByTestId("composer-input").fill("what does this mean?");
  await panel.getByTestId("composer-send").click();
  await expect(panel.getByTestId("ask-attachment").first()).toBeVisible();
  expect(fake.deleted ?? []).toEqual([]); // nothing else happened on the side
});

test("a named failure blocks Send and Enter until its labeled removal", async ({ page }) => {
  await mockShell(page);
  // AFTER the shell's success route: Playwright matches the last registration first.
  await page.route("**/api/ask/conversations/*/attachments", (r) =>
    r.fulfill({ status: 422, json: { detail: "that file is larger than the limit" } }),
  );
  await page.goto("/");
  const panel = await openAsk(page);
  await panel.getByTestId("ask-attach-input").setInputFiles({
    name: "notes.docx",
    mimeType: "application/msword",
    buffer: Buffer.from("MZ binary"),
  });
  const pill = panel.getByTestId("ask-staged-pill");
  await expect(pill).toContainText("notes.docx");
  await expect(pill.getByTestId("ask-staged-state")).toHaveText(
    "Couldn't read that file",
  );
  await panel.getByTestId("composer-input").fill("send anyway");
  await expect(panel.getByTestId("composer-send")).toBeDisabled();
  await panel.getByTestId("composer-input").press("Enter");
  await expect(panel.getByTestId("ask-staged-pill")).toHaveCount(1); // nothing sent, the pill stays
  await panel.getByTestId("composer-send").click({ force: true });
  await expect(panel.getByTestId("ask-staged-pill")).toHaveCount(1);

  await panel.getByLabel("Remove notes.docx").click();
  await expect(panel.getByTestId("ask-staged-pill")).toHaveCount(0);
  await expect(panel.getByTestId("composer-send")).toBeEnabled();
});

test("a long file name never scrolls the thread sideways on a phone", async ({ page }) => {
  const long =
    "a-very-long-shared-file-name-that-must-not-scroll-the-chat-sideways-" +
    "on-a-phone-even-when-it-has-no-extension-to-break-at.log";
  const conv: FakeConversation = {
    id: CID,
    created_at: 10,
    updated_at: 30,
    turns: [
      turnOf("see attached", {
        answer: "ok",
        attachments: [att(AID_TXT, long, "text", LOG.length)],
      }),
    ],
  };
  await mockAskConversations(page, []);
  await mockShell(page);
  await page.route("**/api/ask/conversations", (r) =>
    r.fulfill({
      json: {
        configured: true,
        conversations: [
          { id: CID, title: "see attached", created_at: 10, updated_at: 30, unread: false },
        ],
      },
    }),
  );
  await page.route("**/api/ask/conversations/ask_*", (r) => {
    if (r.request().url().includes("/message")) return r.fallback();
    return r.fulfill({ json: { ...conv, running: false, updates: [], following: [] } });
  });
  await page.goto("/");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-attachment")).toHaveCount(1);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});


test("two files picked at once in a fresh thread land in ONE conversation", async ({
  page,
}) => {
  await mockShell(page);
  let creates = 0;
  await page.route("**/api/ask/conversations", (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    creates += 1;
    return r.fulfill({
      status: 201,
      json: {
        id: "ask_" + "f".repeat(32),
        title: "",
        turns: [],
        running: false,
        updates: [],
        following: [],
      },
    });
  });
  await page.goto("/");
  const panel = await openAsk(page);
  await panel.getByTestId("ask-attach-input").setInputFiles([
    { name: "one.txt", mimeType: "text/plain", buffer: Buffer.from("one") },
    { name: "two.txt", mimeType: "text/plain", buffer: Buffer.from("two") },
  ]);
  await expect(panel.getByTestId("ask-staged-pill")).toHaveCount(2);
  await expect(panel.getByTestId("ask-staged-state").first()).toHaveText("Ready");
  await expect(panel.getByTestId("ask-staged-state").nth(1)).toHaveText("Ready");
  // single-flight creation: one conversation for both files, never one per file
  expect(creates).toBe(1);
});


test("View opens the FULL text — a long log scrolls to its end, never a silent cut", async ({
  page,
}) => {
  const tail = "THE-VERY-END-OF-THE-LOG-MARKER";
  const long = Array.from({ length: 400 }, (_, i) => `line ${i + 1} of the deploy log`).join("\n") +
    "\n" + tail;
  expect(long.length).toBeGreaterThan(4000);
  const conv: FakeConversation = {
    id: CID,
    created_at: 10,
    updated_at: 30,
    turns: [turnOf("see the log", { answer: "ok", attachments: [att(AID_TXT, "deploy.log", "text", long.length)] })],
  };
  await mockShell(page);
  await page.route("**/api/ask/conversations", (r) =>
    r.fulfill({
      json: {
        configured: true,
        conversations: [
          { id: CID, title: "see the log", created_at: 10, updated_at: 30, unread: false },
        ],
      },
    }),
  );
  await page.route("**/api/ask/conversations/ask_*", (r) => {
    if (r.request().url().includes("/attachment") || r.request().url().includes("/message"))
      return r.fallback();
    return r.fulfill({ json: { ...conv, running: false, updates: [], following: [] } });
  });
  await page.route(/\/api\/ask\/conversations\/[^/]+\/attachments\/[^/?]+(\?|$)/, (r) => {
    const aid = r.request().url().split("/").pop()!.split("?")[0];
    if (aid !== AID_TXT) return r.fallback();
    if (r.request().url().includes("meta=1"))
      return r.fulfill({
        json: { attachment: att(AID_TXT, "deploy.log", "text", long.length) },
      });
    return r.fulfill({ contentType: "text/plain", body: long });
  });
  await page.goto("/");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await panel.getByTestId("ask-attachment-view").click();
  const preview = panel.getByTestId("ask-attachment-preview");
  await expect(preview).toBeVisible();
  // the WHOLE file is in the scrollable box: the tail is reachable by scrolling, not absent
  const reached = await preview.evaluate((el, mark) => {
    el.scrollTop = el.scrollHeight;
    return el.textContent?.includes(mark) ?? false;
  }, tail);
  expect(reached).toBe(true);
  await expect(preview).toContainText(tail);
});


test("New conversation invalidates the upload flight — the next file goes to the NEW thread", async ({
  page,
}) => {
  await mockShell(page);
  let creates = 0;
  await page.route("**/api/ask/conversations", (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    creates += 1;
    return r.fulfill({
      status: 201,
      json: {
        id: "ask_" + String(creates).padStart(32, "f"),
        title: "",
        turns: [],
        running: false,
        updates: [],
        following: [],
      },
    });
  });
  await page.goto("/");
  const panel = await openAsk(page);
  // a real turn first: New conversation is disabled on an empty thread
  await panel.getByTestId("composer-input").fill("first");
  await panel.getByTestId("composer-send").click();
  await expect(panel.getByTestId("ask-turn")).toHaveCount(1);
  expect(creates).toBe(1); // conversation A
  await panel.getByTestId("ask-attach-input").setInputFiles({
    name: "two.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("two"),
  });
  await expect(panel.getByTestId("ask-staged-state")).toHaveText("Ready"); // went to A
  await panel.getByTestId("ask-new").click();
  await expect(panel.getByTestId("ask-staged-pill")).toHaveCount(0); // the switch cleared them
  await panel.getByTestId("ask-attach-input").setInputFiles({
    name: "three.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("three"),
  });
  await expect(panel.getByTestId("ask-staged-state")).toHaveText("Ready");
  // the flight was invalidated by New: a second conversation is created, A is not reused
  expect(creates).toBe(2);
});


test("a first Send and a first Attach share ONE creation, whichever lands first", async ({
  page,
}) => {
  await mockShell(page);
  let creates = 0;
  let release: (() => void) | null = null;
  const gate = new Promise<void>((r) => (release = r));
  await page.route("**/api/ask/conversations", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    creates += 1;
    await gate; // both callers pile onto the same flight while creation is in the air
    return r.fulfill({
      status: 201,
      json: {
        id: "ask_" + String(creates).padStart(32, "f"),
        title: "",
        turns: [],
        running: false,
        updates: [],
        following: [],
      },
    });
  });
  await page.goto("/");
  const panel = await openAsk(page);
  // Send first (its creation is in the air), then a file — the file must ride the same flight
  await panel.getByTestId("composer-input").fill("first question");
  await panel.getByTestId("composer-send").click();
  await panel.getByTestId("ask-attach-input").setInputFiles({
    name: "with-it.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("file"),
  });
  await expect(panel.getByTestId("ask-staged-pill")).toHaveCount(1); // the file queued up
  // both callers are now in the air on ONE creation flight — never two conversations
  expect(creates).toBe(1);
  release!();
  await expect(panel.getByTestId("ask-staged-state")).toHaveText("Ready");
  await expect(panel.getByTestId("ask-turn")).toHaveCount(1);
  expect(creates).toBe(1); // and still one after they settle
});

test("a discard the transport keeps failing stays visible and lands once it can", async ({
  page,
}) => {
  await mockShell(page);
  let deletes = 0;
  await page.route("**/api/ask/conversations/*/attachments/*", (r) => {
    if (r.request().method() !== "DELETE") return r.fallback();
    deletes += 1;
    // the server's tombstone makes the quota free at once; what can still fail here is the
    // transport — and THAT is what the visible pending row and the retry cover
    return r.fulfill(
      deletes <= 3
        ? { status: 500, json: { detail: "try again" } }
        : { json: { discarded: true } },
    );
  });
  await page.goto("/");
  const panel = await openAsk(page);
  await panel.getByTestId("ask-attach-input").setInputFiles({
    name: "held.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("x"),
  });
  await expect(panel.getByTestId("ask-staged-state")).toHaveText("Ready");
  await panel.getByTestId("ask-staged-remove").click();
  // while it cannot land, the pending cleanup is VISIBLE — the quota is not silently promised
  await expect(panel.getByTestId("ask-cleanup-pending")).toContainText(
    "removed file",
  );
  await expect(panel.getByTestId("ask-cleanup-pending")).toHaveCount(0, {
    timeout: 20_000,
  });
  expect(deletes).toBeGreaterThanOrEqual(4); // it kept retrying past the refusals
});


test("a refused send restores the draft and the file, and the retry goes through", async ({
  page,
}) => {
  await mockShell(page);
  let refused = true;
  await page.route("**/api/ask/conversations/*/message/stream", (r) => {
    const body = r.request().postDataJSON() as { text?: string } | null;
    if (refused && (body?.text?.length ?? 0) > 500) {
      return r.fulfill({ status: 422, json: { detail: "text too long" } });
    }
    refused = false;
    return r.fallback();
  });
  await page.goto("/");
  const panel = await openAsk(page);
  await panel.getByTestId("ask-attach-input").setInputFiles({
    name: "with-it.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("file"),
  });
  await expect(panel.getByTestId("ask-staged-state")).toHaveText("Ready");
  await panel.getByTestId("composer-input").fill("x".repeat(600));
  await panel.getByTestId("composer-send").click();
  // the confirmed refusal (nothing written) restores BOTH: the text and the same staged file
  await expect(panel.getByTestId("ask-staged-pill")).toHaveCount(1);
  await expect(panel.getByTestId("composer-input")).toHaveValue("x".repeat(600));
  // and the operator can fix and resend: same file goes with the trimmed question
  await panel.getByTestId("composer-input").fill("short and fine");
  await panel.getByTestId("composer-send").click();
  // two rows: the refused attempt's error stays as the visible record of it, and the retry's
  // turn carries the file. The DRAFT was what came back — not a duplicate of the refusal.
  await expect(panel.getByTestId("ask-turn")).toHaveCount(2);
  // the same file card stands on both rows — the refused attempt and the sent turn
  await expect(panel.getByTestId("ask-attachment")).toHaveCount(2);
  await expect(panel.getByTestId("ask-staged-pill")).toHaveCount(0); // sent with the retry
});


test("an admitted turn is never resent after a New-conversation detour", async ({
  page,
}) => {
  await mockShell(page);
  // The stream is held open until the operator has clicked New — the server ADMITS the turn
  // while the watch is being abandoned. The conversation read then carries it, which is what
  // the reconcile consults before any draft may be offered as "Not sent".
  let releaseStream: (() => void) | null = null;
  const gate = new Promise<void>((r) => (releaseStream = r));
  const admitted: Record<string, unknown> = {};
  await page.route("**/api/ask/conversations/*/message/stream", async (r) => {
    const body = r.request().postDataJSON() as { turn_id: string; text?: string };
    admitted.turn = {
      turn_id: body.turn_id,
      question: body.text ?? "",
      asked_at: 1,
      status: "done",
      answer: "it ran",
      detail: null,
      code: null,
      matches: [],
      mission_matches: [],
      tools: [],
      attachments: [],
    };
    await gate;
    return r.fulfill({
      contentType: "application/x-ndjson",
      body: JSON.stringify({ type: "turn", status: 200, turn: admitted.turn }) + "\n",
    });
  });
  await page.route("**/api/ask/conversations/ask_*", (r) => {
    if (r.request().url().includes("/message") || r.request().url().includes("/attachment"))
      return r.fallback();
    // once the stream gate is past, the server HOLDS the admitted turn
    return r.fulfill({
      json: {
        id: "ask_" + "a".repeat(32),
        title: "an admitted question",
        turns: admitted.turn ? [admitted.turn] : [],
        running: false,
        updates: [],
        following: [],
      },
    });
  });
  await page.goto("/");
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("an admitted question");
  await panel.getByTestId("composer-send").click();
  await panel.getByTestId("ask-new").click(); // the detour: New while the stream is pending
  releaseStream!(); // the server turn settles behind the abandoned watch
  await expect(panel.getByTestId("ask-recovered")).toHaveCount(0, { timeout: 15_000 });
  // and nothing is sitting in the new thread's composer waiting to be sent twice
  await expect(panel.getByTestId("composer-input")).toHaveValue("");
});


test("a turn admitted AFTER the reconcile is never offered for resend", async ({ page }) => {
  await mockShell(page);
  // The ordering that used to slip through: the POST is received and the turn is admitted
  // only AFTER the reconcile read has already run — absence there is NOT proof of refusal.
  let admitted: Record<string, unknown> | null = null;
  await page.route("**/api/ask/conversations/*/message/stream", async (r) => {
    const body = r.request().postDataJSON() as { turn_id: string; text?: string };
    const turn = {
      turn_id: body.turn_id,
      question: body.text ?? "",
      asked_at: 1,
      status: "done",
      answer: "it landed late",
      detail: null,
      code: null,
      matches: [],
      mission_matches: [],
      tools: [],
      attachments: [],
    };
    await new Promise((res) => setTimeout(res, 700)); // admit AFTER the reconcile read
    admitted = turn;
    return r.fulfill({
      contentType: "application/x-ndjson",
      body: JSON.stringify({ type: "turn", status: 200, turn }) + "\n",
    });
  });
  await page.route("**/api/ask/conversations/ask_*", (r) => {
    if (r.request().url().includes("/message") || r.request().url().includes("/attachment"))
      return r.fallback();
    return r.fulfill({
      json: {
        id: "ask_" + "a".repeat(32),
        title: "late landing",
        turns: admitted ? [admitted] : [],
        running: false,
        updates: [],
        following: [],
      },
    });
  });
  await page.goto("/");
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("late landing");
  await panel.getByTestId("composer-send").click();
  await panel.getByTestId("ask-new").click(); // abort while the POST is still in flight
  await page.waitForTimeout(900); // the turn is admitted behind the aborted watch
  const back = await openAsk(page);
  await back.getByTestId("ask-switcher").click();
  await back.getByTestId("ask-switcher-item").first().click();
  // the kept draft is UNCERTAIN: kept, never resendable — the turn may already have landed
  await expect(back.getByTestId("ask-recovered")).toHaveCount(1);
  await expect(back.getByTestId("ask-recovered-restore")).toHaveCount(0);
  await expect(back.getByTestId("ask-recovered")).toContainText("may already be sent");
});
