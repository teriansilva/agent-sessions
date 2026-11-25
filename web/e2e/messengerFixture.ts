/** A stateful stand-in for a messenger connector's routes (#1832), holding the server's rules the
 *  page depends on: candidates are a snapshot at a generation; a save names ids only, each an
 *  ELIGIBLE candidate of that snapshot (else `unknown_recipient`), at the same generation (else
 *  `stale_candidates`); labels are the server's; send needs a test and recipients bound to the
 *  current bot; a test naming another bot switches send off and keeps the reason. */
import type { Page } from "@playwright/test";

import { mockShell } from "./callFixture";

export const BOT_TOKEN = "123456789:AAH_good-bot-token-abcdefghijk";
export const OTHER_BOT_TOKEN = "987654321:AAH_other-bot-token-qwertyuiop";
const BOTS: Record<string, string> = { [BOT_TOKEN]: "hq_bot", [OTHER_BOT_TOKEN]: "other_bot" };

export const TELEGRAM = {
  id: "telegram",
  label: "Telegram",
  publisher: "battlelab",
  version: "1",
  category: "messenger",
  adapter: "telegram-bot",
  origin_mode: "fixed",
  fixed_origin: "https://api.telegram.org",
  auth: { kind: "path-token", scopes: ["bot-token"] },
  capabilities: { read: [], send: ["message"] },
  setup: "Create a bot for BattleLab with @BotFather in Telegram (/newbot) and paste its token.",
};
export const MATRIX = {
  ...TELEGRAM,
  id: "matrix",
  label: "Matrix",
  adapter: "matrix-cs",
  origin_mode: "operator",
  fixed_origin: null,
  auth: { kind: "token-header", scopes: ["access-token"] },
  setup: "Create a Matrix account for the bot and paste its access token.",
};

export type Candidate = {
  id: string;
  label: string;
  kind: string;
  members?: number;
  unavailable?: string;
};

type Msg = {
  id: string;
  manifest_id: "telegram" | "matrix";
  origin: string;
  generation: number;
  token: string | null;
  login: string | null; // the identity the current test named
  recipients: { id: string; label: string; kind: string }[];
  bound: { origin: string; identity: string } | null;
  send: boolean;
  send_off: { reason: string; was: string; now: string; at: number } | null;
};

export type MessengerServer = {
  instances: Msg[];
  candidates: Candidate[];
  /** The next candidates read fails with this (status, code, detail). */
  failList: [number, string, string] | null;
  saves: unknown[];
};

export function messenger(over: Partial<Msg> = {}): Msg {
  return {
    id: `c_${Math.random().toString(16).slice(2, 14)}`,
    manifest_id: "telegram",
    origin: "https://api.telegram.org",
    generation: 1,
    token: null,
    login: null,
    recipients: [],
    bound: null,
    send: false,
    send_off: null,
    ...over,
  };
}

function bound(i: Msg) {
  return !!i.bound && i.bound.origin === i.origin && i.bound.identity === i.login;
}

function view(i: Msg) {
  const tested = i.login ? { login: i.login, repos: 0, at: 1 } : null;
  return {
    id: i.id,
    manifest_id: i.manifest_id,
    origin: i.origin,
    status: !i.origin ? "needs_origin" : !i.token ? "needs_token" : tested ? "ready" : "needs_test",
    token_set: !!i.token,
    account: null,
    generation: i.generation,
    tested_generation: tested ? i.generation : null,
    tested,
    last_test: tested ? { generation: i.generation, ok: true, code: null, at: 1 } : null,
    enabled: { read: false },
    created_at: 1,
    recipients: i.recipients,
    recipients_bound: bound(i),
    recipients_chosen_for: i.bound?.identity ?? null,
    send: i.send && bound(i) && i.recipients.length > 0 && !!tested,
    send_off: i.send_off,
  };
}

export async function mockMessengers(page: Page, instances: Msg[] = []): Promise<MessengerServer> {
  await mockShell(page);
  const srv: MessengerServer = { instances, candidates: [], failList: null, saves: [] };
  const snapshots = new Map<string, { generation: number; ids: Map<string, Candidate> }>();
  const refuse = (code: string, detail: string, status = 422) => ({ status, json: { detail, code } });
  await page.route(/\/api\/connectors(\/.*)?$/, async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/^.*\/api\/connectors/, "");
    const method = req.method();
    const body = req.postData() ? JSON.parse(req.postData()!) : {};
    if (method === "GET" && path === "") {
      return route.fulfill({
        json: {
          catalog: [TELEGRAM, MATRIX],
          instances: srv.instances.map(view),
          disclosure: "What HQ reads through a connector is sent to your AI endpoint.",
        },
      });
    }
    if (method === "POST" && path === "/instances") {
      const i = messenger({
        manifest_id: body.manifest_id,
        origin: body.manifest_id === "telegram" ? TELEGRAM.fixed_origin : "",
      });
      srv.instances.push(i);
      return route.fulfill({ status: 201, json: view(i) });
    }
    const m = path.match(/^\/instances\/([^/]+)(\/test|\/capabilities|\/recipients(?:\/candidates)?)?$/);
    const i = m ? srv.instances.find((x) => x.id === m[1]) : undefined;
    if (!m || !i) return route.fulfill(refuse("not_found", "no such connector", 404));
    const tail = m[2] ?? "";
    if (method === "PATCH" && tail === "") {
      if (body.origin !== undefined) i.origin = String(body.origin);
      if (body.token) i.token = body.token;
      i.generation += 1;
      i.login = null;
      return route.fulfill({ json: view(i) });
    }
    if (method === "POST" && tail === "/test") {
      const who = i.token ? BOTS[i.token] : undefined;
      if (!who) return route.fulfill({ json: { status: "failed", code: "rejected", detail: "rejected", generation: i.generation } });
      i.login = who;
      if (i.bound && !bound(i)) {
        const reason = i.bound.origin !== i.origin ? "origin_changed" : "identity_changed";
        i.send_off = { reason, was: reason === "origin_changed" ? i.bound.origin : i.bound.identity, now: reason === "origin_changed" ? i.origin : who, at: 2 };
        i.send = false;
      }
      return route.fulfill({ json: { status: "ok", login: who, repos: 0, generation: i.generation } });
    }
    if (method === "GET" && tail === "/recipients/candidates") {
      if (srv.failList) {
        const [status, code, detail] = srv.failList;
        srv.failList = null;
        return route.fulfill(refuse(code, detail, status));
      }
      snapshots.set(i.id, { generation: i.generation, ids: new Map(srv.candidates.map((c) => [c.id, c])) });
      const allowed = new Set(i.recipients.map((r) => r.id));
      return route.fulfill({ json: { candidates: srv.candidates.map((c) => ({ ...c, allowed: allowed.has(c.id) })) } });
    }
    if (method === "PUT" && tail === "/recipients") {
      srv.saves.push(body);
      const snap = snapshots.get(i.id);
      if (!snap || snap.generation !== i.generation)
        return route.fulfill(refuse("stale_candidates", "the connector changed; list the chats again"));
      const out = [];
      for (const id of body.ids as string[]) {
        const c = snap.ids.get(id);
        const kept = i.recipients.find((r) => r.id === id);
        // Like the server: an unlisted saved chat is kept only by the same bot at the same origin.
        if (!c && kept && bound(i)) {
          out.push({ ...kept });
          continue;
        }
        if (!c || c.unavailable) return route.fulfill(refuse("unknown_recipient", "that chat can't be used"));
        out.push({ id, label: c.label, kind: c.kind });
      }
      i.recipients = out;
      i.bound = { origin: i.origin, identity: i.login! };
      i.send_off = null;
      return route.fulfill({ json: view(i) });
    }
    if (method === "POST" && tail === "/capabilities" && "send" in body) {
      if (body.send && !(i.login && i.recipients.length && bound(i)))
        return route.fulfill(refuse("needs_recipients", "save at least one recipient first"));
      i.send = !!body.send;
      return route.fulfill({ json: view(i) });
    }
    return route.fulfill(refuse("bad_body", "unexpected request", 400));
  });
  return srv;
}
