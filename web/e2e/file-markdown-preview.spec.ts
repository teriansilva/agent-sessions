import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { headAction, FILES_ACTION } from "./headActions";
import { TOUCH_FLOOR } from "./touchTarget";

/** Markdown preview in the Files viewer (#2114) — real-browser proof, desktop AND mobile.
 *
 *  The browser renders the document, decodes the repo image from real PNG bytes, and draws the
 *  Mermaid diagram as an image, so "it rendered" is the element's own state (`naturalWidth`), not
 *  markup. The API is mocked at the network edge; the Home Free transport is proven against the
 *  real stack by `appmode-markdown.spec.ts`.
 *
 *  The safety fixtures are hostile on purpose: raw-HTML script and handlers, `javascript:` links,
 *  remote images in every spelling the sanitiser lets through (Markdown, `//host`, `srcset`,
 *  `<picture><source>`), and a Mermaid diagram with hostile labels. Nothing may run, and no request
 *  may leave the app's origin. */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";
const PNG = readFileSync(join(process.cwd(), "e2e", "fixtures", "media", "dot.png"));

const README = `---
title: agent-sessions
tags: [docs]
---
# agent-sessions

A mobile-first session organizer. See the [guide](docs/guide.md#setup), [Engines](#engines)
and [the site](https://example.test/docs). A [bad link](javascript:window.__pwned='link').

![Screenshot](shots/dot.png "the app")

Remote: ![badge](https://img.shields.io/badge/x.svg) and ![cdn](//cdn.example.test/y.png).

<p align="center"><img src="shots/dot.png" srcset="https://evil.example.test/a.png 2x" alt="centered" width="32"></p>

<picture><source srcset="https://evil.example.test/b.png"><img src="shots/dot.png" alt="pictured"></picture>

<img src="x" onerror="window.__pwned='onerror'"><script>window.__pwned='script'</script>

## How it fits together

\`\`\`mermaid
flowchart LR
  Browser --> FastAPI --> Agent
  Browser --> dtach --> Agent
\`\`\`

## Engines

| Engine | Native id | Status |
|---|---|---|
| claude | \`uuid\` | stable |
| opencode | \`ses_*\` | stable |

- [x] Install it
- [ ] Set the origin

\`\`\`ts
const port: number = 8080; // listen here
\`\`\`

Footnote ref[^1].

${"\nfiller paragraph to make the document scroll.\n".repeat(40)}

[^1]: The footnote.
`;

const GUIDE = "# Guide\n\n## Setup\n\nBack to the [readme](../README.md).\n";

const HOSTILE = `# Hostile diagrams

\`\`\`mermaid
flowchart LR
  A["<img src=x onerror=window.__pwned='label'>"] --> B["<script>window.__pwned='lscript'</script>"]
  C["<a href='https://evil.example.test/l'>remote</a>"] --> A
  click A "javascript:window.__pwned='click'"
  click B callMe
\`\`\`

\`\`\`mermaid
flowchart LR
  this is not ((( a valid diagram
\`\`\`
`;

const TEXT_FILES: Record<string, string> = {
  [`${CWD}/README.md`]: README,
  [`${CWD}/docs/guide.md`]: GUIDE,
  [`${CWD}/hostile.md`]: HOSTILE,
};

const SESSION = {
  id: "claude:aaaaaaaa-0000-4000-8000-000000000002",
  engine: "claude",
  title: "markdown preview session",
  cwd: CWD,
  project: { kind: "folder", id: CWD, name: "proj" },
  last_mtime: NOW - 120,
  archived: false,
  favorite: false,
};

interface Mock {
  rawPaths: string[];
  /** Requests to any origin but the app's (data:/blob: are not requests to anyone). */
  foreign: string[];
}

async function mockApp(page: Page): Promise<Mock> {
  const m: Mock = { rawPaths: [], foreign: [] };
  page.on("request", (r) => {
    const u = r.url();
    if (/^(data|blob):/.test(u)) return;
    if (new URL(u).hostname !== "localhost" && new URL(u).hostname !== "127.0.0.1") m.foreign.push(u);
  });
  // Anything foreign is refused at the edge too, so a regression is a failed assertion, not a
  // real request to the internet.
  await page.route(/^https?:\/\/(?!localhost|127\.0\.0\.1)/, (r) => r.abort());
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: { csrf: "x", new_session_engines: [], terminal_backend: "ws", auth_mode: "none", hostname: "test" },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [SESSION], next_offset: null, total: 1, facets: { projects: [], engines: ["claude"] } },
    }),
  );
  await page.route("**/api/files/capabilities", (r) => r.fulfill({ json: { ok: true, reason: "" } }));
  await page.route("**/api/files/list**", (r) =>
    r.fulfill({
      json: {
        path: CWD,
        parent: "/home/u",
        root: "/home/u",
        entries: ["README.md", "hostile.md"].map((name) => ({
          name,
          path: `${CWD}/${name}`,
          kind: "file",
          size: 2048,
          mtime: NOW,
        })),
        total: 2,
        complete: true,
        truncated: false,
      },
    }),
  );
  await page.route("**/api/files/read**", (r) => {
    const p = new URL(r.request().url()).searchParams.get("path") ?? "";
    const content = TEXT_FILES[p];
    if (content === undefined) return r.fulfill({ status: 404, json: { detail: "no such file or directory" } });
    return r.fulfill({
      json: {
        path: p,
        size: Buffer.byteLength(content),
        binary: false,
        mime: "text/markdown",
        preview: null,
        content,
        truncated: false,
        version: "v1",
        editable: true,
        readonly_reason: null,
        eol: "\n",
        bom: false,
      },
    });
  });
  await page.route("**/api/files/raw**", (r: Route) => {
    const p = new URL(r.request().url()).searchParams.get("path") ?? "";
    m.rawPaths.push(p);
    if (!p.endsWith("/dot.png")) return r.fulfill({ status: 404, json: { detail: "no such file or directory" } });
    return r.fulfill({ status: 200, contentType: "image/png", body: PNG });
  });
  return m;
}

async function start(page: Page): Promise<Mock> {
  const m = await mockApp(page);
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await expect(page.locator("#root")).toBeVisible();
  return m;
}

async function openFile(page: Page, name: string) {
  const viewer = page.locator("[data-file-viewer]");
  const panel = page.locator("[data-file-panel]");
  if (!(await panel.isVisible())) {
    const trigger = await headAction(page, FILES_ACTION);
    await trigger.click();
    await expect(panel).toBeVisible();
  }
  await page.locator("[data-file-row]", { hasText: name }).first().click();
  await expect(viewer).toBeVisible();
  return viewer;
}

const pwned = (page: Page) => page.evaluate(() => (window as unknown as { __pwned?: string }).__pwned ?? null);

test.describe("markdown preview", () => {
  test("a Markdown file opens on Preview, rendered: headings, table, repo image, diagram, code", async ({ page }) => {
    const m = await start(page);
    const viewer = await openFile(page, "README.md");
    const doc = viewer.locator("[data-md-preview]");
    await expect(viewer.locator("[data-md-view='preview']")).toHaveAttribute("aria-pressed", "true");
    await expect(doc.getByRole("heading", { level: 1, name: "agent-sessions" })).toBeVisible();
    await expect(doc.locator("table")).toContainText("opencode");
    await expect(doc.locator("[data-md-code='yaml']")).toContainText("tags: [docs]");

    // The repo image, resolved against the document's folder and decoded by the browser.
    const img = doc.locator("img[alt='Screenshot']");
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(64);
    expect(m.rawPaths).toContain(`${CWD}/shots/dot.png`);

    // The diagram, drawn as an image.
    const diagram = doc.locator("[data-md-mermaid]").first();
    await diagram.scrollIntoViewIfNeeded();
    await expect(diagram).toHaveAttribute("data-md-mermaid", "ok", { timeout: 30_000 });
    const svg = diagram.locator("img");
    expect(await svg.getAttribute("src")).toMatch(/^blob:/);
    await expect.poll(() => svg.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(100);

    // Highlighted code: the keyword is a coloured span, not plain text.
    const code = doc.locator("[data-md-code='ts']");
    await expect(code.locator("span", { hasText: "const" }).first()).toBeVisible();

    // Task list + footnote.
    await expect(doc.locator("input[type='checkbox']")).toHaveCount(2);
    await expect(doc.locator("input[type='checkbox']").first()).toBeChecked();
    await expect(doc).toContainText("The footnote.");
    await expect(viewer.locator("[data-viewer-foot]")).toContainText("Markdown // UTF-8");
  });

  test("Preview | Source: Source is the editor, Edit goes to Source and holds Preview", async ({ page }) => {
    await start(page);
    const viewer = await openFile(page, "README.md");
    const preview = viewer.locator("[data-md-view='preview']");
    const source = viewer.locator("[data-md-view='source']");
    await source.click();
    await expect(viewer.locator(".cm-content")).toBeVisible();
    await expect(viewer.locator("[data-md-preview]")).toHaveCount(0);
    await preview.click();
    await expect(viewer.locator("[data-md-preview]")).toBeVisible();
    await expect(viewer.locator(".cm-content")).toBeHidden();

    await viewer.locator("[data-edit-toggle]").click();
    await expect(source).toHaveAttribute("aria-pressed", "true");
    await expect(preview).toBeDisabled();
    await expect(viewer.locator(".cm-content")).toBeVisible();
    await viewer.locator("[data-edit-done]").click();
    await preview.click();
    await expect(viewer.locator("[data-md-preview]")).toBeVisible();
  });

  test("links: #anchors scroll the preview, a repo file opens in the viewer, the web opens a tab", async ({ page }) => {
    await start(page);
    const viewer = await openFile(page, "README.md");
    const doc = viewer.locator("[data-md-preview]");

    const external = doc.getByRole("link", { name: "the site" });
    await expect(external).toHaveAttribute("target", "_blank");
    await expect(external).toHaveAttribute("rel", "noopener noreferrer");
    await expect(doc.getByRole("link", { name: "bad link" })).toHaveCount(0);

    const engines = doc.getByRole("heading", { name: "Engines" });
    await doc.getByRole("link", { name: "Engines", exact: true }).click();
    await expect(engines).toBeInViewport();

    await doc.getByRole("link", { name: "guide" }).click();
    await expect(viewer.getByRole("heading", { level: 1, name: "Guide" })).toBeVisible();
    await expect(viewer).toContainText(`${CWD}/docs/guide.md`);
    await viewer.getByRole("link", { name: "readme" }).click();
    await expect(viewer.getByRole("heading", { level: 1, name: "agent-sessions" })).toBeVisible();
  });

  test("hostile HTML runs nothing and no image is fetched from anywhere but the box", async ({ page }) => {
    const m = await start(page);
    const dialogs: string[] = [];
    page.on("dialog", (d) => {
      dialogs.push(d.message());
      void d.dismiss();
    });
    const viewer = await openFile(page, "README.md");
    const doc = viewer.locator("[data-md-preview]");
    await expect(doc.locator("[data-md-remote-image]")).toHaveCount(2);
    await expect(doc.locator("[data-md-remote-image='img.shields.io']")).toContainText("not loaded here");
    // The raw-HTML images load from the box; their srcset / <source> never reach the page.
    await expect.poll(() => doc.locator("img[alt='centered']").evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(64);
    await expect.poll(() => doc.locator("img[alt='pictured']").evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(64);
    await expect(doc.locator("[srcset], source, script, [onerror]")).toHaveCount(0);
    // Bring the diagram and the end of the document into view, so every lazy image and diagram
    // has had its chance to load.
    await doc.locator("[data-md-mermaid]").scrollIntoViewIfNeeded();
    await expect(doc.locator("[data-md-mermaid='ok']")).toHaveCount(1, { timeout: 30_000 });
    await doc.locator("text=The footnote.").scrollIntoViewIfNeeded();
    expect(await pwned(page)).toBeNull();
    expect(dialogs).toEqual([]);
    expect(m.foreign).toEqual([]);
  });

  test("a hostile Mermaid diagram runs nothing, leaves no scratch node, and a broken one says why", async ({ page }) => {
    const m = await start(page);
    const viewer = await openFile(page, "hostile.md");
    const diagrams = viewer.locator("[data-md-mermaid]");
    await expect(diagrams).toHaveCount(2);
    await expect(diagrams.nth(0)).not.toHaveAttribute("data-md-mermaid", "waiting", { timeout: 30_000 });
    await diagrams.nth(1).scrollIntoViewIfNeeded();
    await expect(diagrams.nth(1)).toHaveAttribute("data-md-mermaid", "error", { timeout: 30_000 });
    await expect(diagrams.nth(1)).toContainText("Diagram // Could not render");
    await expect(diagrams.nth(1).locator("pre")).toContainText("not ((( a valid diagram");
    // The hostile one is drawn (strict mode keeps its text as text) or refused — never executed.
    expect(["ok", "error"]).toContain(await diagrams.nth(0).getAttribute("data-md-mermaid"));
    expect(await pwned(page)).toBeNull();
    expect(m.foreign).toEqual([]);
    // Mermaid's render-stage scratch nodes are gone from <body>; the only SVG trace is the image.
    const leftovers = await page.evaluate(
      () => document.querySelectorAll("body > [id^='dmd-mermaid'], body > [id^='md-mermaid'], [id^='md-mermaid-']").length,
    );
    expect(leftovers).toBe(0);
  });

  test("phone and desktop: nothing scrolls sideways, the table scrolls in its box, controls hold 44px", async ({
    page,
    isMobile,
  }) => {
    await start(page);
    const viewer = await openFile(page, "README.md");
    await expect(viewer.locator("[data-md-preview] h1")).toBeVisible();
    const [scroll, client] = await page.evaluate(() => [
      document.scrollingElement!.scrollWidth,
      document.scrollingElement!.clientWidth,
    ]);
    expect(scroll).toBeLessThanOrEqual(client);
    const docBox = await viewer.locator("[data-md-preview]").boundingBox();
    const vw = page.viewportSize()!.width;
    expect(docBox!.x + docBox!.width).toBeLessThanOrEqual(vw + 0.5);
    if (isMobile) {
      for (const sel of ["[data-md-view='preview']", "[data-md-view='source']", "[data-edit-toggle]"]) {
        const box = await viewer.locator(sel).boundingBox();
        expect(box!.height, sel).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
      const copy = viewer.locator("[data-md-code='ts'] button");
      await copy.scrollIntoViewIfNeeded();
      expect((await copy.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  });
});
