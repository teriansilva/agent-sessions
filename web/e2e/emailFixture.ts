/** A stateful stand-in for an email connector's routes (#1836), holding the server's rules the page
 *  depends on: an address save is the whole list with the generation the page showed (else
 *  `stale_candidates`); each address is one plain mailbox (else `invalid_address`, in words) with
 *  its domain lower-cased; a From address change moves the generation and clears the test; a test
 *  signing in as another account switches sending off and keeps the reason. */
import type { Page } from "@playwright/test";

import { mockShell } from "./callFixture";

export const PASSWORD = "app-password-123456";
const ACCOUNTS: Record<string, string> = {
  "hq@example.com": PASSWORD,
  "ops@example.com": PASSWORD,
};

export const EMAIL = {
  id: "smtp",
  label: "Email",
  publisher: "battlelab",
  version: "1",
  category: "messenger",
  adapter: "smtp",
  origin_mode: "operator",
  fixed_origin: null,
  auth: { kind: "basic", scopes: ["app-password"] },
  capabilities: { read: [], send: ["message"] },
  setup:
    "Enter your provider's SMTP server as smtps://host (port 465) or smtp://host (port 587, STARTTLS), the account you sign in with, and an app password made for BattleLab.",
};

type Mail = {
  id: string;
  origin: string;
  account: string;
  generation: number;
  password: string | null;
  login: string | null;
  sender: string;
  recipients: { id: string; label: string; kind: string }[];
  bound: { origin: string; identity: string } | null;
  send: boolean;
  send_off: { reason: string; was: string; now: string; at: number } | null;
};

export type EmailServer = { instances: Mail[]; saves: unknown[]; patches: unknown[] };

export function mailbox(over: Partial<Mail> = {}): Mail {
  return {
    id: `c_${Math.random().toString(16).slice(2, 14)}`,
    origin: "",
    account: "",
    generation: 1,
    password: null,
    login: null,
    sender: "",
    recipients: [],
    bound: null,
    send: false,
    send_off: null,
    ...over,
  };
}

const ADDRESS =
  /^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/;

function bound(i: Mail) {
  return !!i.bound && i.bound.origin === i.origin && i.bound.identity === i.login;
}

function view(i: Mail) {
  const tested = i.login ? { login: i.login, repos: 0, at: 1 } : null;
  return {
    id: i.id,
    manifest_id: "smtp",
    origin: i.origin,
    status: !i.origin ? "needs_origin" : !i.password ? "needs_token" : tested ? "ready" : "needs_test",
    token_set: !!i.password,
    account: i.account,
    generation: i.generation,
    tested_generation: tested ? i.generation : null,
    tested,
    last_test: tested ? { generation: i.generation, ok: true, code: null, at: 1 } : null,
    enabled: { read: false },
    created_at: 1,
    recipients: i.recipients,
    recipients_bound: bound(i),
    recipients_chosen_for: i.bound?.identity ?? null,
    send: i.send && bound(i) && i.recipients.length > 0 && !!tested && !!i.sender,
    send_off: i.send_off,
    sender: i.sender,
  };
}

export async function mockEmail(page: Page, instances: Mail[] = []): Promise<EmailServer> {
  await mockShell(page);
  const srv: EmailServer = { instances, saves: [], patches: [] };
  const refuse = (code: string, detail: string, status = 422) => ({ status, json: { detail, code } });
  await page.route(/\/api\/connectors(\/.*)?$/, async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/^.*\/api\/connectors/, "");
    const method = req.method();
    const body = req.postData() ? JSON.parse(req.postData()!) : {};
    if (method === "GET" && path === "") {
      return route.fulfill({
        json: {
          catalog: [EMAIL],
          instances: srv.instances.map(view),
          disclosure: "What HQ reads through a connector is sent to your AI endpoint.",
        },
      });
    }
    if (method === "POST" && path === "/instances") {
      const i = mailbox();
      srv.instances.push(i);
      return route.fulfill({ status: 201, json: view(i) });
    }
    const m = path.match(/^\/instances\/([^/]+)(\/test|\/capabilities|\/recipients)?$/);
    const i = m ? srv.instances.find((x) => x.id === m[1]) : undefined;
    if (!m || !i) return route.fulfill(refuse("not_found", "no such connector", 404));
    const tail = m[2] ?? "";
    if (method === "PATCH" && tail === "") {
      srv.patches.push(body);
      if (body.sender !== undefined) {
        if (body.sender && !ADDRESS.test(body.sender))
          return route.fulfill(refuse("invalid_sender", "From address: that isn't an address"));
        i.sender = body.sender;
      }
      if (body.origin !== undefined) i.origin = String(body.origin);
      if (body.account !== undefined) i.account = String(body.account);
      if (body.token) i.password = body.token;
      i.generation += 1;
      i.login = null;
      return route.fulfill({ json: view(i) });
    }
    if (method === "POST" && tail === "/test") {
      if (!i.password || ACCOUNTS[i.account] !== i.password)
        return route.fulfill({
          json: { status: "failed", code: "rejected", detail: "the server rejected the account or password", generation: i.generation },
        });
      i.login = i.account;
      if (i.bound && !bound(i)) {
        i.send_off = { reason: "identity_changed", was: i.bound.identity, now: i.account, at: 2 };
        i.send = false;
      }
      return route.fulfill({ json: { status: "ok", login: i.account, repos: 0, generation: i.generation } });
    }
    if (method === "PUT" && tail === "/recipients") {
      srv.saves.push(body);
      if (body.generation !== i.generation)
        return route.fulfill(refuse("stale_candidates", "the connector changed; look at it again"));
      const out = [];
      for (const e of body.addresses as { address: string; label: string }[]) {
        const at = e.address.lastIndexOf("@");
        const a = e.address.slice(0, at + 1) + e.address.slice(at + 1).toLowerCase();
        if (!ADDRESS.test(a))
          return route.fulfill(refuse("invalid_address", "that isn't an address, like name@example.com"));
        out.push({ id: a, label: e.label || a, kind: "email" });
      }
      i.recipients = out;
      i.bound = { origin: i.origin, identity: i.login! };
      i.send_off = null;
      return route.fulfill({ json: view(i) });
    }
    if (method === "POST" && tail === "/capabilities" && "send" in body) {
      if (body.send && !(i.login && i.recipients.length && bound(i) && i.sender))
        return route.fulfill(refuse("needs_recipients", "save at least one address first"));
      i.send = !!body.send;
      return route.fulfill({ json: view(i) });
    }
    return route.fulfill(refuse("bad_body", "unexpected request", 400));
  });
  return srv;
}
