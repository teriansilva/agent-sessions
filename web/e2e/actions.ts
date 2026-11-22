/** The action gallery (#2248 P1) for the browser tests: `GET /api/actions` answered with the
 *  server's own output (`fixtures/actions-gallery.json`, pinned by `tests/test_actions.py`), so a
 *  spec can never pass against a shape or wording the server does not send. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { Page } from "@playwright/test";

import { ACTIONS_API } from "../src/lib/apiPaths";
import type { ActionGallery } from "../src/types/actions";

export const ACTION_GALLERY: ActionGallery = JSON.parse(
  readFileSync(resolve(process.cwd(), "e2e/fixtures/actions-gallery.json"), "utf8"),
);

/** The five actions automations had before the plugins (#2248 P1): the gallery the existing
 *  wizard and gallery specs are written against. The plugin actions (#2248 P2) have their own
 *  spec, over the whole answer. */
export const BUILTIN_IDS = [
  "start_mission",
  "start_session",
  "send_to_session",
  "forge_review_loop",
  "tell_hq",
];
export const BUILTIN_GALLERY: ActionGallery = {
  ...ACTION_GALLERY,
  actions: ACTION_GALLERY.actions.filter((a) => BUILTIN_IDS.includes(a.id)),
};

/** Answer `GET /api/actions`; `gallery` changes what it says (a requirement not met, a failure). */
export async function mockActions(
  page: Page,
  gallery: ActionGallery | { status: number; detail: string } = BUILTIN_GALLERY,
): Promise<{ reads: () => number }> {
  let reads = 0;
  await page.route(`**${ACTIONS_API}`, (r) => {
    reads++;
    return "status" in gallery
      ? r.fulfill({ status: gallery.status, json: { detail: gallery.detail } })
      : r.fulfill({ json: gallery });
  });
  return { reads: () => reads };
}

/** The gallery with one plugin's requirement NOT met: the wizard's needs-setup state. */
export function withSetupMissing(id: string, detail: string): ActionGallery {
  const g: ActionGallery = structuredClone(ACTION_GALLERY);
  const a = g.actions.find((x) => x.id === id)!;
  a.requires = a.requires.map((r) => ({ ...r, ok: false, detail }));
  a.available = false;
  return g;
}

const MESSENGER_MANIFESTS = [
  { id: "telegram", label: "Telegram", adapter: "telegram-bot" },
  { id: "matrix", label: "Matrix", adapter: "matrix-cs" },
  { id: "smtp", label: "Mail", adapter: "smtp" },
].map((m) => ({
  ...m,
  publisher: "battlelab",
  version: "1",
  category: "messenger",
  origin_mode: m.id === "telegram" ? "fixed" : "operator",
  fixed_origin: m.id === "telegram" ? "https://api.telegram.org" : null,
  auth: { kind: "path-token", scopes: [] },
  capabilities: { read: [], send: ["message"] },
  setup: "",
}));

/** A messenger instance as `GET /api/connectors` reports it (`connectors/store.public`). */
export function messenger(over: Record<string, unknown> = {}) {
  return {
    id: "c_0123456789ab",
    manifest_id: "telegram",
    origin: "https://api.telegram.org",
    status: "ready",
    token_set: true,
    account: null,
    generation: 3,
    tested_generation: 3,
    tested: { login: "ops_bot", repos: 0, at: 1 },
    last_test: null,
    enabled: { read: false },
    recipients: [
      { id: "42", label: "Marcus", kind: "private chat" },
      { id: "-1009", label: "Release crew", kind: "supergroup" },
    ],
    recipients_bound: true,
    recipients_chosen_for: "ops_bot",
    send: true,
    send_off: null,
    created_at: 1,
    ...over,
  };
}

/** Answer `GET /api/connectors` with these messengers (Settings → HQ connectors' read). */
export async function mockMessengers(page: Page, instances = [messenger()]): Promise<void> {
  await page.route("**/api/connectors", (r) =>
    r.fulfill({ json: { catalog: MESSENGER_MANIFESTS, instances, disclosure: "" } }),
  );
}
