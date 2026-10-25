/** The call-to-HQ mocks (#1397 P1), shared by `hq-call.spec.ts`: `/api/**` by route and the call
 *  socket by `page.routeWebSocket`, so a spec plays the server's side of the protocol. */
import { expect, type Page, type WebSocketRoute } from "@playwright/test";

import { mockAskConversations } from "./askStream";

/** Chromium's fake microphone, granted. Spread into `test.use`. */
export const FAKE_MIC = {
  launchOptions: {
    // The config's CHROMIUM_LAUNCH, repeated because `test.use` replaces the object.
    ignoreDefaultArgs: ["--disable-dev-shm-usage"],
    args: [
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
      "--autoplay-policy=no-user-gesture-required",
    ],
  },
  permissions: ["microphone"],
};

export const CID = "c".repeat(32);
export const AVAILABLE = {
  configured: true,
  runtime: true,
  available: true,
  reason: "",
  captions: true,
  mic: "open",
  active_call: null,
  destinations: {
    stt: "lan",
    tts: "lan",
    ai: "remote",
    stt_host: "gpu-1.lan:8000",
    tts_host: "gpu-1.lan:8880",
    ai_host: "ai.example.com",
  },
};
export const SETTINGS = {
  stt: { base_url: "http://gpu-1.lan:8000/v1", model: "whisper-1", api_key_set: false },
  tts: { base_url: "http://gpu-1.lan:8880/v1", model: "tts", api_key_set: true },
  voices: { woman: "aria", man: "atlas" },
  turn_taking: "natural",
  interrupt: true,
  mic: "open",
  answers: "brief",
  captions: true,
  configured: true,
  contacts: {} as Record<string, unknown>,
  contact_defaults: Object.fromEntries(
    [
      ["asia-woman", "calm", 1], ["asia-man", "serious", 0.95], ["europe-woman", "narrator", 1],
      ["europe-man", "neutral", 1], ["russia-woman", "serious", 1], ["russia-man", "dramatic", 1],
      ["us-woman", "energetic", 1.05], ["us-man", "dramatic", 1], ["africa-woman", "energetic", 1],
      ["africa-man", "calm", 0.95], ["southamerica-woman", "dramatic", 1.05],
      ["southamerica-man", "calm", 1], ["romania-woman", "dramatic", 1],
    ].map(([id, style, speed]) => [
      id,
      { group: String(id).endsWith("-man") ? "man" : "woman", style, speed },
    ]),
  ),
  language: "auto",
  tts_styles: [] as string[],
  tts_languages: [] as string[],
  tts_voices: [] as string[],
  preview_languages: ["en", "de", "fr", "es", "it", "pt", "nl", "pl", "ru", "ja", "zh", "hi", "ko"],
  stt_detects_language: false,
  tts_accepts_language: false,
};

/** `GET /api/voice/local` for an install that never ran `voice setup` (#1397 P4). */
export const LOCAL_NOT_SET_UP = {
  installed: { runtime: false, server: false },
  token: false,
  unit: null,
  systemd: true,
  stt: null,
  last_error: null,
  stale: null,
  url: "http://127.0.0.1:8766/v1",
  running: false,
  in_use: false,
  voices: { woman: "af_heart", man: "am_michael" },
  limits: "hard",
};

export interface Server {
  caps: Record<string, unknown>;
  /** What `GET /api/voice/settings` answers (a save answers it too). */
  settings: Record<string, unknown>;
  local: Record<string, unknown>;
  calls: unknown[];
  hangups: string[];
  saves: Record<string, unknown>[];
  sockets: WebSocketRoute[];
  /** Text messages the page sent, per socket. */
  texts: string[][];
  /** Binary frames the page sent, per socket, and how many arrived before `connected`. */
  binaries: number[];
  binariesBeforeConnected: number[];
  /** Per socket: binary frames that carry sound (not the noise gate's silence). */
  voiced: number[];
  connected: boolean[];
}

export async function mockShell(page: Page, caps: Record<string, unknown> = AVAILABLE): Promise<Server> {
  const srv: Server = {
    caps,
    settings: SETTINGS,
    local: LOCAL_NOT_SET_UP,
    calls: [],
    hangups: [],
    saves: [],
    sockets: [],
    texts: [],
    binaries: [],
    binariesBeforeConnected: [],
    voiced: [],
    connected: [],
  };
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "csrf-x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
        hq_contact: "asia-woman",
        hq_contact_rev: 0,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }),
  );
  await mockAskConversations(page);
  await page.route(/\/api\/ask\/conversations\/c{32}$/, (r) =>
    r.fulfill({
      json: {
        id: CID,
        title: "call",
        running: false,
        turns: [
          {
            turn_id: "t1",
            question: "How is api rebase?",
            asked_at: 1,
            status: "done",
            answer: "Two auth tests fail.",
            detail: null,
            code: null,
            matches: [],
            mission_matches: [],
            tools: [],
            ask: null,
            proposals: [
              {
                action_id: "a1",
                kind: "nudge",
                key: "claude:00000000-0000-4000-8000-000000000001",
                title: "api-rebase",
                agent: "claude",
                text: "Rerun the auth tests and fix the two failures.",
                state: "pending",
                projection: "actionable",
                can_approve: true,
                can_reject: true,
                detail: "",
              },
            ],
            contact: "asia-woman",
          },
        ],
      },
    }),
  );
  await page.route("**/api/voice/capabilities", (r) => r.fulfill({ json: srv.caps }));
  await page.route("**/api/voice/settings", (r) => {
    if (r.request().method() === "POST") {
      const patch = r.request().postDataJSON() as Record<string, unknown>;
      srv.saves.push(patch);
      // Like the server: a General's profile is replaced whole, null resets it.
      if (patch.contacts) {
        const contacts = { ...(srv.settings.contacts as Record<string, unknown>) };
        for (const [id, prof] of Object.entries(patch.contacts as Record<string, unknown>)) {
          if (prof === null) delete contacts[id];
          else contacts[id] = prof;
        }
        srv.settings = { ...srv.settings, contacts };
      }
      if (typeof patch.language === "string") srv.settings = { ...srv.settings, language: patch.language };
      return r.fulfill({ json: srv.settings });
    }
    return r.fulfill({ json: srv.settings });
  });
  await page.route("**/api/voice/local", (r) => r.fulfill({ json: srv.local }));
  await page.route("**/api/voice/calls", (r) => {
    srv.calls.push(r.request().postDataJSON());
    return r.fulfill({
      status: 201,
      json: { id: `call${srv.calls.length}`, conversation_id: CID, contact: "asia-woman", state: "dialing", muted: false, elapsed_s: 0, ended_reason: "" },
    });
  });
  await page.route(/\/api\/voice\/calls\/[^/]+$/, (r) => {
    srv.hangups.push(r.request().url());
    return r.fulfill({ json: {} });
  });
  await page.routeWebSocket(/\/ws\/voice\//, (ws) => {
    const i = srv.sockets.length;
    srv.sockets.push(ws);
    srv.texts.push([]);
    srv.binaries.push(0);
    srv.binariesBeforeConnected.push(0);
    srv.voiced.push(0);
    srv.connected.push(false);
    ws.onMessage((m) => {
      if (typeof m === "string") {
        srv.texts[i].push(m);
        if (JSON.parse(m).t === "hello") ws.send(JSON.stringify({ t: "state", state: "dialing" }));
      } else {
        srv.binaries[i] += 1;
        if (!srv.connected[i]) srv.binariesBeforeConnected[i] += 1;
        // A frame with any non-zero sample carries sound; the noise gate sends zeros (#1667).
        if ((m as Buffer).some((b) => b !== 0)) srv.voiced[i] += 1;
      }
    });
  });
  return srv;
}

export const say = (srv: Server, msg: object, i = 0) => srv.sockets[i].send(JSON.stringify(msg));

export async function connect(page: Page, srv: Server) {
  await page.goto("/dashboard");
  await page.getByTestId("hq-primary-call").click();
  const panel = page.getByTestId("call-panel");
  await expect(panel).toBeVisible();
  await expect.poll(() => srv.texts[0]?.length ?? 0).toBeGreaterThan(0);
  return panel;
}
