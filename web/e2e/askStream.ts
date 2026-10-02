import type { Route } from "@playwright/test";

/** Ask's transport since #1171: `POST /api/pulse/ask/stream`, NDJSON. `page.route` delivers a
 *  fulfilled body all at once, so the events land together — enough for every spec that is about
 *  what an ANSWER looks like. What the stream adds (the step, Stage 1's answer while Stage 2 runs)
 *  is pinned where time can be controlled: `AskConsole.test.tsx` and `tests/test_pulse_chat.py`. */
export const ASK_STREAM = "**/api/pulse/ask/stream";

/** Fulfil one ask with `result` as its final answer, after any `before` events. */
export function fulfillAsk(
  route: Route,
  result: Record<string, unknown>,
  before: Record<string, unknown>[] = [],
): Promise<void> {
  const lines = [...before, { type: "answer", final: true, ...result }];
  return route.fulfill({
    status: 200,
    contentType: "application/x-ndjson",
    body: lines.map((l) => JSON.stringify(l)).join("\n") + "\n",
  });
}
