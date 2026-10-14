/** The visitor's client for a public share link (#1404 P4). It talks ONLY to `/api/public/*`.
 *
 *  This module is the viewer bundle's whole network surface. It must never import the operator's
 *  client (`lib/api.ts`): no admin CSRF token, no operator route, no auth gate that would bounce a
 *  visitor to `/login`. `shareBundle.test.ts` walks the viewer's imports to keep it that way.
 *
 *  The share cookie is HttpOnly and scoped to `/api/public`, so the browser sends it on these
 *  calls by itself; the visitor's CSRF token comes from the unlock (or `GET /api/public/share`)
 *  answer and is sent on every mutation, as the server's `require_share(send=True)` demands. */
import type { ShareKind, ShareMode } from "../lib/shareFormat";

export interface VisitorShare {
  /** The link's opaque identity. The share cookie is one per browser, so another tab unlocking
   *  another link changes what this page's reads return: every snapshot and events page carries
   *  the link it is for, and the view refuses one that isn't this. */
  link: string;
  kind: ShareKind;
  mode: ShareMode;
  title: string;
  expires_at: number | null;
  /** The name the visitor gave — self-chosen, a label, not an identity. */
  name: string | null;
  /** A send link whose visitor hasn't named themselves yet: ask before the composer. */
  name_required: boolean;
}

export interface Unlocked {
  share: VisitorShare;
  csrf: string;
}

export interface PublicAttachment {
  name: string;
  mime: string;
  /** Opaque handle, read back through `/api/public/uploads/{image}`. */
  image?: string;
}

export interface PublicTool {
  name: string | null;
  outcome: string | null;
  summary: string | null;
}

export interface PublicTurn {
  /** `agent`: a turn the agent ran for itself (#1438) — no text, attachments or author. */
  kind?: "operator" | "agent";
  turn_id: string;
  state: string;
  delivery?: string | null;
  text?: string | null;
  text_truncated?: boolean;
  reply: string | null;
  reply_ts: number | null;
  reply_truncated: boolean;
  attachments?: PublicAttachment[];
  tools: PublicTool[];
  tools_truncated: boolean;
  author?: { name: string; via: "public_link" } | null;
  /** Sent through THIS link — what the server checks Send now against (never the name). */
  mine?: boolean;
}

export interface PublicRequest {
  request_id: string;
  turn_id: string;
  kind: string;
  summary: string;
}

export interface PublicSnapshot {
  link: string;
  revision: number;
  event_cursor: number;
  state: string;
  active_turn: string | null;
  model: string | null;
  turns: PublicTurn[];
  omitted_turns: number;
  pending_requests: PublicRequest[];
  read_only: boolean;
  images: boolean;
  can_send: boolean;
  mission_held: boolean;
  /** A background task runs with no turn running (#1430). */
  background?: boolean;
}

export interface PublicEventPage {
  link: string;
  revision: number;
  next_cursor: number;
}

/** A refused public call. `status` drives the viewer: 404 = the link is gone (revoked, expired,
 *  session gone), 401 = the share cookie is missing or too old (unlock again). */
export class PublicError extends Error {
  status: number;
  body: Record<string, unknown>;
  constructor(status: number, message: string, body: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function call<T>(
  path: string,
  init: { method?: string; csrf?: string; json?: unknown; body?: Blob; type?: string } = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (init.csrf) headers["X-CSRF-Token"] = init.csrf;
  let body: BodyInit | undefined;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  } else if (init.body) {
    headers["Content-Type"] = init.type || "application/octet-stream";
    body = init.body;
  }
  let r: Response;
  try {
    r = await fetch(path, {
      method: init.method ?? "GET",
      credentials: "same-origin",
      // The token is in the page's own path: never let a request carry it as a Referer.
      referrerPolicy: "no-referrer",
      cache: "no-store",
      headers,
      body,
    });
  } catch {
    throw new PublicError(0, "Can’t reach this BattleLab right now.");
  }
  let parsed: Record<string, unknown> = {};
  try {
    parsed = (await r.json()) as Record<string, unknown>;
  } catch {
    /* an empty or non-JSON body */
  }
  if (!r.ok) {
    const detail = typeof parsed.detail === "string" ? parsed.detail : `HTTP ${r.status}`;
    throw new PublicError(r.status, detail, parsed);
  }
  return parsed as T;
}

export const publicApi = {
  /** `name` re-sends the name a visitor already gave, so unlocking again (the 12 h cookie ran
   *  out) doesn't ask a `send` visitor for it twice. */
  unlock: (token: string, password?: string, name?: string | null) =>
    call<Unlocked>("/api/public/unlock", {
      method: "POST",
      json: { token, ...(password ? { password } : {}), ...(name ? { name } : {}) },
    }),
  share: () => call<Unlocked>("/api/public/share"),
  setName: (csrf: string, name: string) =>
    call<{ share: VisitorShare }>("/api/public/name", { method: "POST", csrf, json: { name } }),
  session: () => call<PublicSnapshot>("/api/public/session"),
  events: (after: number) => call<PublicEventPage>(`/api/public/events?after=${after}&limit=1`),
  turn: (
    csrf: string,
    body: { client_key: string; text: string; attachments: string[] },
  ) => call<{ state: string | null }>("/api/public/turns", { method: "POST", csrf, json: body }),
  interrupt: (csrf: string, turnId: string) =>
    call<{ interrupted: boolean }>("/api/public/interrupt", {
      method: "POST",
      csrf,
      json: { turn_id: turnId },
    }),
  sendNow: (csrf: string, queuedTurnId: string) =>
    call<{ sent: boolean }>("/api/public/send-now", {
      method: "POST",
      csrf,
      json: { queued_turn_id: queuedTurnId },
    }),
  upload: (csrf: string, file: Blob) =>
    call<{ stored: string; image: string; mime: string }>("/api/public/upload", {
      method: "POST",
      csrf,
      body: file,
      type: file.type,
    }),
};

export function imageUrl(handle: string): string {
  return `/api/public/uploads/${encodeURIComponent(handle)}`;
}

/** The token in `/p/<token>`, or null on the bare `/p/` the viewer moves to after unlocking. */
export function tokenFromPath(pathname: string): string | null {
  const m = /^\/p\/([A-Za-z0-9_-]+)\/?$/.exec(pathname);
  return m ? m[1] : null;
}

/** Where the viewer moves the address bar once the token has been traded for a cookie. */
export const VIEWER_PATH = "/p/";
