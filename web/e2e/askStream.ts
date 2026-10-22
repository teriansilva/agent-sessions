import type { Page, Route } from "@playwright/test";

/** Ask's transport since #1443: a durable conversation under `/api/ask/*`, each question streamed
 *  as NDJSON from `POST /api/ask/conversations/{id}/message/stream` — `progress` lines, then one
 *  `turn`. `page.route` delivers a fulfilled body all at once, so the lines land together — enough
 *  for every spec about what an ANSWER looks like. What the stream adds over time (the steps) is
 *  pinned where time can be controlled: `AskConsole.test.tsx` and `tests/test_ask_orchestrator.py`.
 *
 *  `mockAskConversations` is a small fake of the conversation routes (list, create, read, delete), and
 *  `fulfillAsk` records each answered turn into it, so a reload or a switch reads back what was
 *  asked — as the real server does. */
export const ASK_STREAM = "**/api/ask/conversations/*/message/stream";

export interface FakeTurn {
  turn_id: string;
  question: string;
  asked_at: number;
  status: "pending" | "done" | "failed";
  answer: string | null;
  detail: string | null;
  code: string | null;
  matches: { id: string; title: string; why: string }[];
  mission_matches: { id: string; title: string; state: string; why: string }[];
  tools: { name: string; outcome: string }[];
  ask?: {
    question_id: string;
    question: string;
    options: string[];
    allow_text: boolean;
    answered: boolean;
  } | null;
  proposals?: Record<string, unknown>[];
  answers?: string | null;
  /** Who answered (#1525): the request's contact, echoed as the server stores it. */
  contact?: string;
}

export interface FakeConversation {
  id: string;
  created_at: number;
  updated_at: number;
  turns: FakeTurn[];
  /** #1443 Phase 4: the follower's updates, the sessions followed, and an unseen update. */
  updates?: { key: string; kind: string; title: string; ts: number; following: boolean }[];
  following?: string[];
  /** Titles for followed sessions; defaults to the title of their newest update. */
  followTitles?: Record<string, string>;
  /** The update budget is spent (#1517). */
  followsExhausted?: boolean;
  unread?: boolean;
}

export interface FakeAsk {
  conversations: FakeConversation[];
  /** Conversations the server refuses to delete right now (409). */
  busy?: Set<string>;
  /** Ids deleted, in order. */
  deleted?: string[];
}

const fakes = new WeakMap<Page, FakeAsk>();
let clock = 1_700_000_000;

function cidOf(n: number): string {
  return "ask_" + n.toString(16).padStart(32, "0");
}

function summary(c: FakeConversation) {
  return {
    id: c.id,
    title: c.turns[0]?.question ?? "",
    created_at: c.created_at,
    updated_at: c.updated_at,
    unread: Boolean(c.unread),
  };
}

function body(c: FakeConversation) {
  // As the server derives it: a question is answered once a later turn names it.
  const answered = new Set(c.turns.map((t) => t.answers).filter(Boolean));
  const turns = c.turns.map((t) =>
    t.ask ? { ...t, ask: { ...t.ask, answered: answered.has(t.ask.question_id) } } : t,
  );
  return {
    ...summary(c),
    turns,
    running: false,
    updates: c.updates ?? [],
    following: c.following ?? [],
    follows_exhausted: Boolean(c.followsExhausted),
    follows: (c.following ?? []).map((key) => ({
      key,
      title:
        c.followTitles?.[key] ??
        [...(c.updates ?? [])].reverse().find((u) => u.key === key)?.title ??
        "",
    })),
    unread: Boolean(c.unread),
  };
}

/** Install the fake conversation routes. `seed` conversations are listed newest-updated first. */
export async function mockAskConversations(
  page: Page,
  seed: FakeConversation[] = [],
): Promise<FakeAsk> {
  const fake: FakeAsk = { conversations: [...seed] };
  fakes.set(page, fake);
  const latestFirst = () =>
    [...fake.conversations].sort((a, b) => b.updated_at - a.updated_at);
  await page.route(/\/api\/ask\/conversations$/, (r) => {
    if (r.request().method() === "POST") {
      const c: FakeConversation = {
        id: cidOf(fake.conversations.length + 1 + Math.floor(Math.random() * 1e6)),
        created_at: ++clock,
        updated_at: clock,
        turns: [],
      };
      fake.conversations.push(c);
      return r.fulfill({ status: 201, json: body(c) });
    }
    return r.fulfill({
      json: { conversations: latestFirst().map(summary), configured: true },
    });
  });
  // #1443 Phase 4: the unread dot, marking a conversation seen, and Unfollow.
  await page.route(/\/api\/ask\/unread$/, (r) =>
    r.fulfill({
      json: {
        unread: fake.conversations.some((c) => c.unread),
        conversations: fake.conversations.filter((c) => c.unread).length,
      },
    }),
  );
  await page.route(/\/api\/ask\/conversations\/(ask_[0-9a-f]{32})\/seen$/, (r) => {
    const id = /\/(ask_[0-9a-f]{32})\/seen$/.exec(new URL(r.request().url()).pathname)![1];
    const c = fake.conversations.find((x) => x.id === id);
    if (c) c.unread = false;
    return r.fulfill({ json: { unread: false } });
  });
  await page.route(/\/api\/ask\/conversations\/(ask_[0-9a-f]{32})\/follow\/.+$/, (r) => {
    const m = /\/(ask_[0-9a-f]{32})\/follow\/(.+)$/.exec(new URL(r.request().url()).pathname)!;
    const key = decodeURIComponent(m[2]);
    const c = fake.conversations.find((x) => x.id === m[1]);
    if (!c || !(c.following ?? []).includes(key))
      return r.fulfill({ status: 404, json: { detail: "not followed" } });
    c.following = (c.following ?? []).filter((k) => k !== key);
    c.updates = (c.updates ?? []).map((u) => (u.key === key ? { ...u, following: false } : u));
    return r.fulfill({ json: { following: false } });
  });
  await page.route(/\/api\/ask\/conversations\/(ask_[0-9a-f]{32})$/, (r) => {
    const id = /\/(ask_[0-9a-f]{32})$/.exec(new URL(r.request().url()).pathname)![1];
    const c = fake.conversations.find((x) => x.id === id);
    if (!c) return r.fulfill({ status: 404, json: { detail: "no such conversation" } });
    if (r.request().method() === "DELETE") {
      // The operator deletes it (`ask_store.delete_conversation`); `busy` stands in for a running
      // turn or a session still starting.
      if (fake.busy?.has(id))
        return r.fulfill({
          status: 409,
          json: { detail: "HQ is still answering in this conversation — try again once it settles" },
        });
      fake.conversations = fake.conversations.filter((x) => x.id !== id);
      fake.deleted = [...(fake.deleted ?? []), id];
      return r.fulfill({ json: { deleted: true, id } });
    }
    return r.fulfill({ json: body(c) });
  });
  return fake;
}

/** A settled turn, as the server stores it. */
export function turnOf(
  question: string,
  result: Record<string, unknown>,
  turnId = crypto.randomUUID(),
): FakeTurn {
  const matches = (result.matches as Record<string, unknown>[] | undefined) ?? [];
  const missions = (result.mission_matches as Record<string, unknown>[] | undefined) ?? [];
  return {
    turn_id: turnId,
    question,
    asked_at: ++clock,
    status: "done",
    answer: String(result.answer ?? ""),
    detail: null,
    code: null,
    // Only what the store keeps: key, title and why.
    matches: matches.map((m) => ({
      id: String(m.id),
      title: String(m.title ?? ""),
      why: String(m.why ?? ""),
    })),
    mission_matches: missions.map((m) => ({
      id: String(m.id),
      title: String(m.title ?? ""),
      state: String(m.state ?? ""),
      why: String(m.why ?? ""),
    })),
    tools: [],
  };
}

/** Fulfil one question with `result` as its settled answer, after any `before` progress lines. */
export function fulfillAsk(
  route: Route,
  result: Record<string, unknown>,
  before: Record<string, unknown>[] = [],
): Promise<void> {
  const req = route.request().postDataJSON() as {
    turn_id: string;
    text: string;
    answers?: string;
    option?: number;
    contact?: string;
  };
  const id = /\/(ask_[0-9a-f]{32})\/message\/stream$/.exec(
    new URL(route.request().url()).pathname,
  )?.[1];
  let page: Page | null;
  try {
    page = route.request().frame().page();
  } catch {
    page = null; // a request with no frame (a worker's) — nothing to record into
  }
  const fake = page ? fakes.get(page) : undefined;
  const c = fake?.conversations.find((x) => x.id === id);
  // As the server does: an option is answered with the reply text the question stored.
  let text = req.text;
  if (req.answers && req.option !== undefined) {
    const q = c?.turns.find((t) => t.ask?.question_id === req.answers)?.ask;
    text = q?.options[req.option] ?? text;
  }
  const turn = {
    ...turnOf(text, result, req.turn_id),
    answers: req.answers ?? null,
    contact: req.contact ?? "off",
  };
  if (c) {
    c.turns = [...c.turns.filter((t) => t.turn_id !== turn.turn_id), turn];
    c.updated_at = ++clock;
  }
  const lines = [...before, { type: "turn", status: 200, turn }];
  return route.fulfill({
    status: 200,
    contentType: "application/x-ndjson",
    body: lines.map((l) => JSON.stringify(l)).join("\n") + "\n",
  });
}
