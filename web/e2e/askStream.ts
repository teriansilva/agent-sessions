import type { Page, Route } from "@playwright/test";

/** Ask's transport since #1443: a durable conversation under `/api/ask/*`, each question streamed
 *  as NDJSON from `POST /api/ask/conversations/{id}/message/stream` — `progress` lines, then one
 *  `turn`. `page.route` delivers a fulfilled body all at once, so the lines land together — enough
 *  for every spec about what an ANSWER looks like. What the stream adds over time (the steps) is
 *  pinned where time can be controlled: `AskConsole.test.tsx` and `tests/test_ask_orchestrator.py`.
 *
 *  `mockAskConversations` is a small fake of the conversation routes (list, create, read), and
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
}

export interface FakeConversation {
  id: string;
  created_at: number;
  updated_at: number;
  turns: FakeTurn[];
}

export interface FakeAsk {
  conversations: FakeConversation[];
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
  };
}

function body(c: FakeConversation) {
  return { ...summary(c), turns: c.turns, running: false };
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
  await page.route(/\/api\/ask\/conversations\/(ask_[0-9a-f]{32})$/, (r) => {
    const id = /\/(ask_[0-9a-f]{32})$/.exec(new URL(r.request().url()).pathname)![1];
    const c = fake.conversations.find((x) => x.id === id);
    return c
      ? r.fulfill({ json: body(c) })
      : r.fulfill({ status: 404, json: { detail: "no such conversation" } });
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
  const req = route.request().postDataJSON() as { turn_id: string; text: string };
  const turn = turnOf(req.text, result, req.turn_id);
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
