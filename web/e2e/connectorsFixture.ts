/** A stateful stand-in for `/api/connectors` (#1746 P1b), holding the server's rules that the
 *  settings page depends on: an edit makes the connector untested, a moved address without a new
 *  token is refused (`origin_needs_token`), and reading can only be switched on for a `ready`
 *  connector. The token `good-token` passes the test; any other is rejected. */
import type { Page } from "@playwright/test";

import { mockShell } from "./callFixture";

export const GOOD_TOKEN = "good-token";

export const FORGEJO = {
  id: "forgejo",
  label: "Forgejo",
  publisher: "battlelab",
  version: "1",
  category: "versioning",
  adapter: "forgejo-api",
  origin_mode: "operator",
  fixed_origin: null,
  auth: { kind: "token-header", scopes: ["read:issue", "read:repository"] },
  capabilities: { read: ["repos", "issues", "pulls", "ci", "files"] },
  setup: "Create a token in Forgejo under Settings → Applications with read:issue and read:repository.",
};

export const GITHUB = {
  id: "github",
  label: "GitHub",
  publisher: "battlelab",
  version: "1",
  category: "versioning",
  adapter: "github-rest",
  origin_mode: "fixed",
  fixed_origin: "https://api.github.com",
  auth: {
    kind: "token-header",
    scopes: ["contents:read", "issues:read", "pull_requests:read", "commit_statuses:read", "actions:read"],
  },
  capabilities: { read: ["repos", "issues", "pulls", "ci", "files"] },
  setup: "Create a fine-grained token on GitHub for the repositories HQ may read, read-only.",
};
/** A classic GitHub token that can also write: it passes, with the adapter's warning. */
export const CLASSIC_TOKEN = "ghp_classic-with-repo-scope";

export const DISCLOSURE =
  "What HQ reads through a connector is sent to your AI endpoint (https://api.example.com) as part of " +
  "HQ's conversation. Read stays off until you enable it.";

type Inst = {
  id: string;
  manifest_id: string;
  origin: string;
  generation: number;
  token: string | null;
  tested: { login: string; repos: number; at: number } | null;
  last_test: { generation: number; ok: boolean; code: string | null; at: number } | null;
  read: boolean;
};

export type ConnectorServer = {
  instances: Inst[];
  /** Every PATCH body as sent, token included, so a spec can count where a token travelled. */
  patches: Record<string, unknown>[];
  /** Hold the next test reply until this resolves (set by a spec, consumed once). */
  holdTest: Promise<void> | null;
  /** The forge rejects every token from now on (it was revoked there). */
  revoked: boolean;
  /** Hold the next listing's reply (its snapshot is taken when it is asked) until this resolves. */
  holdList: Promise<void> | null;
};

function status(i: Inst) {
  if (!i.origin) return "needs_origin";
  if (!i.token) return "needs_token";
  if (!i.tested) return "needs_test";
  return "ready";
}

function view(i: Inst) {
  return {
    id: i.id,
    manifest_id: i.manifest_id,
    origin: i.origin,
    status: status(i),
    token_set: !!i.token,
    generation: i.generation,
    tested_generation: i.tested ? i.generation : null,
    tested: i.tested,
    last_test: i.last_test,
    enabled: { read: i.read && !!i.tested },
    created_at: 1,
  };
}

export function instance(over: Partial<Inst> = {}): Inst {
  return {
    id: `c_${Math.random().toString(16).slice(2, 14)}`,
    manifest_id: "forgejo",
    origin: "",
    generation: 1,
    token: null,
    tested: null,
    last_test: null,
    read: false,
    ...over,
  };
}

export async function mockConnectors(page: Page, instances: Inst[] = []): Promise<ConnectorServer> {
  await mockShell(page);
  const srv: ConnectorServer = { instances, patches: [], holdTest: null, revoked: false, holdList: null };
  const refuse = (code: string, detail: string, status = 409) => ({ status, json: { detail, code } });
  await page.route(/\/api\/connectors(\/.*)?$/, async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(/^.*\/api\/connectors/, "");
    const method = req.method();
    const body = req.postData() ? JSON.parse(req.postData()!) : {};
    const find = (id: string) => srv.instances.find((i) => i.id === id);
    if (method === "GET" && path === "") {
      const json = { catalog: [FORGEJO, GITHUB], instances: srv.instances.map(view), disclosure: DISCLOSURE };
      if (srv.holdList) {
        const hold = srv.holdList;
        srv.holdList = null;
        await hold;
      }
      return route.fulfill({ json });
    }
    if (method === "POST" && path === "/instances") {
      const i = instance({
        manifest_id: body.manifest_id,
        origin: body.manifest_id === "github" ? GITHUB.fixed_origin : "",
      });
      srv.instances.push(i);
      return route.fulfill({ status: 201, json: view(i) });
    }
    const m = path.match(/^\/instances\/([^/]+)(\/test|\/capabilities)?$/);
    const i = m ? find(m[1]) : undefined;
    if (!m || !i) return route.fulfill(refuse("not_found", "no such connector", 404));
    if (method === "PATCH" && !m[2]) {
      srv.patches.push(body);
      const origin = "origin" in body ? String(body.origin).replace(/\/+$/, "") : i.origin;
      const moved = origin !== i.origin;
      if (moved && i.token && !("token" in body)) {
        return route.fulfill(refuse("origin_needs_token", "a new address needs the token again"));
      }
      if (!moved && !body.token) return route.fulfill({ json: view(i) });
      i.origin = origin;
      if (body.token) i.token = body.token;
      i.generation += 1;
      i.tested = null;
      i.read = false;
      return route.fulfill({ json: view(i) });
    }
    if (method === "POST" && m[2] === "/test") {
      const gen = i.generation;
      if (srv.holdTest) {
        const hold = srv.holdTest;
        srv.holdTest = null;
        await hold;
      }
      if (gen !== i.generation) return route.fulfill({ json: { status: "stale", generation: gen } });
      if (i.token === CLASSIC_TOKEN) {
        i.tested = { login: "commander", repos: 3, at: 2 };
        i.last_test = { generation: gen, ok: true, code: null, at: 2 };
        return route.fulfill({
          json: {
            status: "ok", login: "commander", repos: 3, generation: gen,
            warning: "This token can also write (repo). HQ only reads; a fine-grained, read-only token is safer.",
          },
        });
      }
      if (i.token === GOOD_TOKEN && !srv.revoked) {
        i.tested = { login: "commander", repos: 7, at: 2 };
        i.last_test = { generation: gen, ok: true, code: null, at: 2 };
        return route.fulfill({ json: { status: "ok", login: "commander", repos: 7, generation: gen } });
      }
      // A failed test of the current configuration withdraws an earlier pass (store.settle_test,
      // #1825), so a connector that was reading stops; the read choice comes back with a pass.
      i.last_test = { generation: gen, ok: false, code: "rejected", at: 2 };
      i.tested = null;
      const host = i.origin.replace(/^https?:\/\//, "");
      return route.fulfill({
        json: { status: "failed", code: "rejected", detail: `${host} rejected the token (401)`, generation: gen },
      });
    }
    if (method === "POST" && m[2] === "/capabilities") {
      if (body.read && !i.tested) return route.fulfill(refuse("needs_test", "test the connector first"));
      i.read = !!body.read;
      return route.fulfill({ json: view(i) });
    }
    if (method === "DELETE" && !m[2]) {
      srv.instances = srv.instances.filter((x) => x !== i);
      return route.fulfill({ json: { removed: i.id } });
    }
    return route.fulfill(refuse("bad_body", "unexpected request", 400));
  });
  return srv;
}
