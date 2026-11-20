/** A small, STATEFUL stand-in for `/api/webhooks` (#2221), shaped like `routes/webhooks.py`.
 *
 *  Stateful because the flows under test are sequences: create → the secret is in THAT answer only
 *  → the page is read again without it; rotate → a new secret, the old one valid 24 h more. Every
 *  request is recorded, so a spec asserts what the page SENT. Like the server, no read ever carries
 *  a secret, and a delivery's shape is names and types only. Register it AFTER `mockAutomations`
 *  (the later route wins). */
import type { Page, Route } from "@playwright/test";

import { WEBHOOKS_API } from "../src/lib/apiPaths";

type Json = Record<string, unknown>;

const DAY = 86_400;

export interface FakeHook {
  id: string;
  name: string;
  revision: number;
  path: string;
  legacy_automation: string | null;
  pickable: boolean;
  secret_set: boolean;
  secret_created_at: number | null;
  previous_valid_until: number | null;
  last_delivery_at: number | null;
  last_event: string;
  last_note: string;
  unsigned_24h: number;
  used_by: { id: string; name: string }[];
  created_at: number;
  last_shape: { path: string; type: string }[];
}

export function webhook(over: Partial<FakeHook> = {}): FakeHook {
  const now = Math.floor(Date.now() / 1000);
  const id = over.id ?? "0a1b2c3d4e5f6a7b";
  const legacy = over.legacy_automation ?? null;
  return {
    id,
    name: "shop repo",
    revision: 1,
    path: legacy ? `/hooks/a/${legacy}` : `/hooks/w/${id}`,
    legacy_automation: legacy,
    pickable: !legacy,
    secret_set: true,
    secret_created_at: now - 8 * DAY,
    previous_valid_until: null,
    last_delivery_at: null,
    last_event: "",
    last_note: "",
    unsigned_24h: 0,
    used_by: [],
    created_at: now - 8 * DAY,
    last_shape: [],
    ...over,
  };
}

/** The last delivery a forge's pull-request event leaves behind: names and types, never values. */
export const PR_SHAPE = [
  { path: "action", type: "text" },
  { path: "number", type: "number" },
  { path: "pull_request.head.ref", type: "text" },
  { path: "pull_request.draft", type: "yes/no" },
  { path: "pull_request.labels", type: "list" },
  { path: "pull_request.labels[].name", type: "text" },
  { path: "repository.full_name", type: "text" },
  { path: "sender", type: "object" },
  { path: "pull_request.merged_by", type: "empty" },
];

export interface HookFake {
  hooks: FakeHook[];
  requests: { method: string; path: string; body: Json | null }[];
  /** Every secret the fake handed out, in order. */
  secrets: string[];
}

const pub = (h: FakeHook): Json => {
  const { last_shape: _shape, ...rest } = h;
  void _shape;
  return rest;
};

export async function mockWebhooks(page: Page, initial: FakeHook[] = []): Promise<HookFake> {
  const fake: HookFake = { hooks: initial.map((h) => ({ ...h })), requests: [], secrets: [] };
  let seq = 0;
  const mint = () => {
    seq += 1;
    const s = `${String(seq).padStart(4, "0")}${"5ec7e7".repeat(11)}`.slice(0, 64);
    fake.secrets.push(s);
    return s;
  };
  const prefix = new RegExp(`^${WEBHOOKS_API}`);
  await page.route(new RegExp(`${WEBHOOKS_API}(/[^?]*)?(\\?.*)?$`), async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(prefix, "");
    const method = req.method();
    const body = (req.postDataJSON?.() ?? null) as Json | null;
    fake.requests.push({ method, path, body });
    const json = (status: number, payload: unknown) => route.fulfill({ status, json: payload });
    const parts = path.split("/").filter(Boolean);
    const now = Math.floor(Date.now() / 1000);

    if (!parts.length && method === "GET") return json(200, { webhooks: fake.hooks.map(pub) });
    if (!parts.length && method === "POST") {
      const name = typeof body?.name === "string" ? body.name.trim() : "";
      if (!name || name.length > 60) return json(422, { detail: "name must be 1–60 characters" });
      const id = `${(fake.hooks.length + 1).toString(16).padStart(4, "0")}feedc0de1234`;
      const h = webhook({ id, name, secret_created_at: now, created_at: now });
      fake.hooks.push(h);
      return json(201, { webhook: pub(h), secret: mint() });
    }
    const h = fake.hooks.find((x) => x.id === parts[0]);
    if (!h) return json(404, { detail: "webhook not found" });
    if (parts.length === 1 && method === "GET") return json(200, h);
    if (parts.length === 1 && method === "PATCH") {
      if (body?.revision != null && body.revision !== h.revision) return json(409, { detail: "conflict" });
      h.name = String(body?.name ?? h.name).trim();
      h.revision += 1;
      return json(200, pub(h));
    }
    if (parts.length === 1 && method === "DELETE") {
      if (h.used_by.length || h.legacy_automation)
        return json(409, { detail: "this webhook is used by an automation; change that first" });
      fake.hooks = fake.hooks.filter((x) => x !== h);
      return json(200, { deleted: h.id });
    }
    if (parts[1] === "rotate" && method === "POST") {
      if (h.secret_set) h.previous_valid_until = now + DAY;
      h.secret_set = true;
      h.secret_created_at = now;
      h.revision += 1;
      return json(201, { secret: mint(), webhook: h });
    }
    return json(405, { detail: `unmocked ${method} ${path}` });
  });
  return fake;
}
