import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";

import { FAKE_MIC } from "./callFixture";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

/** Calling an API session (#2341 P2), in a real browser on desktop and mobile, both themes.
 *
 *  The API pane's composer keeps the mic as a glyph (hold to dictate, unchanged, S1) and gains
 *  Call before Send. Call admits a call whose PARTY is this session; the call screen then shows
 *  the session — title, agent, project, voice, the skip-permissions marker — and Back to HQ in
 *  its own row. Dictation still writes into the draft. The server is mocked: `/api/**` by route
 *  and the call socket by `page.routeWebSocket`, the server's side of the call protocol. */

test.use(FAKE_MIC);

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;
const URL_PATH = `/s/codex-api/${ID}`;
const SESSION = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`);
const PARTY = {
  key: KEY,
  title: "docs-refresh",
  engine: "Codex",
  project: "agent-sessions",
  voice: "sol",
  voice_shared: false,
  bypass: true,
};

// Dictation as the pane's own e2e drives it, WITHOUT replacing `mediaDevices`: the call needs the
// fake microphone Chromium provides.
const SPEECH_STUB = `
window.SpeechRecognition = class {
  constructor() { this.onresult = null; this.onerror = null; this.onend = null; }
  start() {
    setTimeout(() => this.onresult && this.onresult({ resultIndex: 0,
      results: [{ 0: { transcript: "check the logout spec" }, isFinal: true, length: 1 }] }), 60);
  }
  stop() { setTimeout(() => this.onend && this.onend(), 20); }
  abort() { this.onend && this.onend(); }
};
`;

interface Server {
  calls: Record<string, unknown>[];
  sockets: WebSocketRoute[];
  texts: string[];
}

async function setup(page: Page, theme: "dark" | "light"): Promise<Server> {
  const srv: Server = { calls: [], sockets: [], texts: [] };
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.addInitScript(SPEECH_STUB);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["codex-api"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        theme,
        agent_defaults: { bypass: false },
        hq_contact: "asia-woman",
        hq_contact_rev: 0,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route(new RegExp(`/api/sessions/codex-api(?::|%3A)${ID}$`), (r) =>
    r.fulfill({
      json: {
        id: KEY, engine: "codex-api", uuid: ID, short_uuid: ID.slice(0, 8), cwd: "/home/u/agent-sessions",
        title: "docs-refresh", archived: false, last_mtime: 1_790_000_000,
        project: { kind: "folder", id: "/home/u/agent-sessions", name: "agent-sessions" },
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await mockRoster(page, {
    overrides: {
      "codex-api": {
        present: true,
        supports_new: true,
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null, can_bypass: true },
      },
    },
  });
  const snapshot = {
    session_key: KEY,
    revision: 4,
    event_cursor: 4,
    cwd: "/home/u/agent-sessions",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: "gpt-5-codex",
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n1", worker: "w1", background_active: false },
    read_only: null,
    bypass: true,
  };
  await page.route(SESSION, (r) => {
    const sub = r.request().url().match(SESSION)?.[1] ?? "";
    if (sub === "/events") {
      return r.fulfill({ json: { session_key: KEY, revision: 4, next_cursor: 4, events: [] } });
    }
    if (sub === "") return r.fulfill({ json: snapshot });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  // Calls are set up; HQ's own endpoint is NOT (a session call needs speech only, #2341 §6b).
  await page.route("**/api/voice/capabilities", (r) =>
    r.fulfill({
      json: {
        configured: true,
        runtime: true,
        available: false,
        reason: "HQ's AI endpoint is not configured",
        sessions: true,
        session_reason: "",
        captions: true,
        mic: "open",
        active_call: null,
        destinations: { stt: "lan", tts: "lan", ai: null, stt_host: "gpu-1.lan:8000", tts_host: "gpu-1.lan:8880", ai_host: "" },
      },
    }),
  );
  await page.route("**/api/voice/calls", (r) => {
    srv.calls.push(r.request().postDataJSON() as Record<string, unknown>);
    return r.fulfill({
      status: 201,
      json: { id: "call1", conversation_id: "c".repeat(32), contact: "asia-woman", state: "dialing",
              muted: false, elapsed_s: 0, ended_reason: "", party: PARTY },
    });
  });
  await page.route(/\/api\/voice\/calls\/[^/]+$/, (r) => r.fulfill({ json: {} }));
  await page.routeWebSocket(/\/ws\/voice\//, (ws) => {
    srv.sockets.push(ws);
    ws.onMessage((m) => {
      if (typeof m !== "string") return;
      srv.texts.push(m);
      if (JSON.parse(m).t === "hello") {
        ws.send(JSON.stringify({ t: "state", state: "dialing" }));
        ws.send(JSON.stringify({ t: "connected" }));
        ws.send(JSON.stringify({ t: "caption", who: "session", id: "g1", text: "docs-refresh here. Go ahead.", kind: "greeting" }));
      }
    });
  });
  return srv;
}

for (const theme of ["dark", "light"] as const) {
  test(`Call on the API pane calls that session; the mic still dictates (#2341, ${theme})`, async ({ page }) => {
    const srv = await setup(page, theme);
    await page.goto(URL_PATH);
    const form = page.getByRole("form", { name: "Compose message" });
    const mic = form.getByRole("button", { name: /voice input/i });
    const call = form.getByTestId("structured-call");
    const send = form.getByRole("button", { name: "Send" });
    await expect(call).toBeVisible();
    await expect(call).toHaveAttribute("data-call", "ready");
    await expect(call).toHaveAccessibleName("Call docs-refresh");
    // The mic is a glyph now: no "Talk" label; Call sits between it and Send, on one row.
    await expect(mic).not.toContainText("Talk");
    const [m, c, s] = [(await mic.boundingBox())!, (await call.boundingBox())!, (await send.boundingBox())!];
    expect(m.x + m.width).toBeLessThanOrEqual(c.x + 1);
    expect(c.x + c.width).toBeLessThanOrEqual(s.x + 1);
    expect(Math.abs(c.y + c.height / 2 - (s.y + s.height / 2))).toBeLessThanOrEqual(1);
    const touch = await page.evaluate(() => matchMedia("(pointer: coarse), (max-width: 800px)").matches);
    if (touch) {
      for (const b of [m, c, s]) expect(b.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);

    // Dictation is unchanged: hold the mic, and the words land in the draft.
    await page.mouse.move(m.x + m.width / 2, m.y + m.height / 2);
    await page.mouse.down();
    await expect(form.getByRole("textbox", { name: /Message/ })).toHaveValue("check the logout spec");
    await page.mouse.up();

    // Call: admitted with this session as the party.
    await call.click();
    await expect.poll(() => srv.calls.length).toBe(1);
    expect(srv.calls[0]).toMatchObject({ party: KEY, conversation_id: null });
    const panel = page.getByTestId("call-panel");
    await expect(panel).toBeVisible();
    await expect(panel).toHaveAttribute("data-party", "session");
    await expect(panel).toHaveAttribute("data-phase", "connected");
    await expect(panel.getByTestId("call-party-title")).toHaveText("docs-refresh");
    await expect(panel.getByTestId("call-party-facts")).toHaveText("Codex · agent-sessions · voice sol");
    await expect(panel.getByTestId("call-party-bypass")).toContainText(
      "Skips permission prompts — what you say runs without asking.",
    );
    await expect(panel.getByTestId("call-caption").first()).toContainText("docs-refresh here. Go ahead.");
    // No HQ-only controls on a session leg: no cards, no "What needs me?", no Type.
    await expect(panel.getByTestId("call-needs-you")).toHaveCount(0);
    await expect(panel.getByTestId("call-type")).toHaveCount(0);
    // Back to HQ has its own row; tapping it asks the server, which answers with the party change.
    const back = panel.getByTestId("call-back-hq");
    await expect(back).toBeVisible();
    if (touch) expect((await back.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    // The session's working band: open with Stop from the moment its turn is followed, before
    // any tool event (review 9031 P2: the server's work-start carries no step count), then its
    // fixed step per tool, and Stop names the session throughout.
    srv.sockets[0].send(JSON.stringify({ t: "state", state: "thinking" }));
    srv.sockets[0].send(JSON.stringify({ t: "progress", step: "Working on it." }));
    await expect(panel.getByTestId("call-work-step")).toHaveText("Working on it.");
    await expect(panel.getByTestId("call-work-tally")).toContainText("0 steps");
    await expect(panel.getByRole("button", { name: "Stop docs-refresh" })).toBeVisible();
    srv.sockets[0].send(JSON.stringify({ t: "progress", step: "Running a command.", steps: 1 }));
    await expect(panel.getByTestId("call-work-step")).toHaveText("Running a command.");
    await expect(panel.getByRole("button", { name: "Stop docs-refresh" })).toBeVisible();
    await panel.getByRole("button", { name: "Stop docs-refresh" }).click();
    await expect.poll(() => srv.texts.some((t) => JSON.parse(t).t === "stop")).toBe(true);
    srv.sockets[0].send(JSON.stringify({ t: "leg_turn", state: "interrupted" }));
    await expect(panel.getByTestId("call-work")).toHaveCount(0);

    // Minimised, the composer's Call says this session is on the call.
    await panel.getByTestId("call-minimise").click();
    await expect(call).toHaveAttribute("data-call", "active");
    await expect(call).toContainText("On call");
    await expect(page.getByTestId("call-bar")).toContainText("docs-refresh");
    await call.click();
    await expect(panel).toBeVisible();

    await back.click();
    await expect.poll(() => srv.texts.some((t) => JSON.parse(t).t === "back")).toBe(true);
    srv.sockets[0].send(JSON.stringify({ t: "party", party: null, hq: true }));
    await expect(panel).toHaveAttribute("data-party", "hq");
    await expect(panel.getByTestId("call-back-hq")).toHaveCount(0);
  });
}

test("a session that can't take calls says so with its fixed line (#2341)", async ({ page }) => {
  await setup(page, "dark");
  await page.route("**/api/voice/calls", (r) =>
    r.fulfill({
      status: 409,
      json: { detail: "docs-refresh can't take calls right now.", code: "unavailable", why: "archived" },
    }),
  );
  await page.goto(URL_PATH);
  await page.getByTestId("structured-call").click();
  await expect(page.getByTestId("call-ended-text")).toHaveText("docs-refresh can't take calls right now.");
  await expect(page.getByTestId("call-continue-text")).toHaveCount(0);
});

for (const theme of ["dark", "light"] as const) {
  test(`HQ puts you through: the transfer card, Stay with HQ, and the session's voice pick (#2341 P3, ${theme})`, async ({
    page,
  }) => {
    const srv = await setup(page, theme);
    await page.goto(URL_PATH);
    await page.getByTestId("structured-call").click();
    const panel = page.getByTestId("call-panel");
    await expect(panel).toHaveAttribute("data-phase", "connected");
    const ws = () => srv.sockets[0];
    const sent = (t: string) => srv.texts.map((m) => JSON.parse(m)).filter((m) => m.t === t);
    // Back with HQ, then HQ puts the call through again: the card, then Stay with HQ drops it.
    ws().send(JSON.stringify({ t: "party", party: null, hq: true }));
    await expect(panel).toHaveAttribute("data-party", "hq");
    ws().send(JSON.stringify({ t: "transfer", party: PARTY }));
    const card = panel.getByTestId("call-transfer");
    await expect(card).toBeVisible();
    await expect(card).toContainText("docs-refresh");
    await expect(card).toContainText("Connecting…");
    const stay = card.getByTestId("call-stay-hq");
    const touch = await page.evaluate(() => matchMedia("(pointer: coarse), (max-width: 800px)").matches);
    if (touch) expect((await stay.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await stay.click();
    await expect.poll(() => sent("stay").length).toBe(1);
    await expect(card).toHaveCount(0);
    // Through for real this time: the session's header, and its voice picked from its pool.
    ws().send(JSON.stringify({ t: "transfer", party: PARTY }));
    await expect(card).toBeVisible();
    ws().send(JSON.stringify({ t: "party", party: { ...PARTY, voices: ["sol", "nova"] }, hq: true }));
    await expect(card).toHaveCount(0);
    await expect(panel).toHaveAttribute("data-party", "session");
    const voice = panel.getByTestId("call-party-voice");
    await expect(voice).toHaveValue("sol");
    await voice.selectOption("nova");
    await expect.poll(() => sent("voice")).toEqual([{ t: "voice", voice: "nova" }]);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
}

test("call me back: the pane's banner with Cancel, then the bell's notice calls the session back (#2341 P4)", async ({
  page,
}) => {
  const srv = await setup(page, "dark");
  const CB = "0123456789abcdef0123456789abcdef";
  let waiting = true;
  const cancelled: string[] = [];
  await page.route(/\/api\/voice\/callbacks(\?.*)?$/, (r) =>
    r.fulfill({
      json: { callbacks: waiting ? [{ id: CB, key: KEY, turn_id: "11111111-2222-4333-8444-555555555555", at: 1 }] : [] },
    }),
  );
  await page.route(/\/api\/voice\/callbacks\/[0-9a-f]+$/, (r) => {
    cancelled.push(r.request().method());
    waiting = false;
    return r.fulfill({ json: { cancelled: true } });
  });
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({
      json: {
        notifications: [
          {
            id: "n1", ts: Math.floor(Date.now() / 1000), read: false, title: "docs-refresh",
            project: "agent-sessions", reason: "docs-refresh is done.", session_id: KEY,
            engine: "codex-api", action_id: `voicecb:${CB}`,
          },
        ],
        unread: 1,
        uncertain: 0,
        settled: [],
      },
    }),
  );
  await page.goto(URL_PATH);
  const banner = page.getByTestId("structured-callback");
  await expect(banner).toContainText("Calls you back when this turn finishes");
  await banner.getByTestId("structured-callback-cancel").click();
  await expect.poll(() => cancelled).toEqual(["DELETE"]);
  await expect(banner).toHaveCount(0);
  // The notice, when the turn settled: a Call straight to the session.
  await page.getByRole("button", { name: /notifications/i }).first().click();
  const call = page.getByTestId("bell-callback-call");
  await expect(call).toBeVisible();
  await expect(call).toHaveAccessibleName("Call docs-refresh");
  await call.click();
  await expect.poll(() => srv.calls.length).toBe(1);
  expect(srv.calls[0]).toMatchObject({ party: KEY });
  await expect(page.getByTestId("call-panel")).toHaveAttribute("data-party", "session");
});
