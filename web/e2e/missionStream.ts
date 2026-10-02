import type { Page, Route } from "@playwright/test";

/** A mission turn's transport since #1224: `POST /api/missions/{id}/message/stream`, NDJSON, whose
 *  last line is `{"type": "turn", "status", "turn"}` — the body `/message` answers. */
export const MISSION_STREAM = "**/api/missions/*/message/stream";

/** Fulfil one turn: any `before` lines (steps, a provisional answer), then the turn itself. */
export function fulfillTurn(
  route: Route,
  turn: Record<string, unknown>,
  before: Record<string, unknown>[] = [],
  status = 200,
): Promise<void> {
  const lines = [...before, { type: "turn", status, turn }];
  return route.fulfill({
    status: 200,
    contentType: "application/x-ndjson",
    body: lines.map((l) => JSON.stringify(l)).join("\n") + "\n",
  });
}

type HeldStream = {
  push: (ev: Record<string, unknown>) => Promise<void>;
  close: () => Promise<void>;
  /** The JSON bodies the composer sent, in order. */
  sent: () => Promise<Record<string, unknown>[]>;
};

/** A turn stream the SPEC feeds line by line. `page.route` can only fulfil a body whole, so the
 *  states BETWEEN lines — the step, a provisional answer — are observable only through a stream the
 *  page reads while the test writes it. Installed before the app loads; only the stream URL is
 *  intercepted, everything else goes to the real `fetch` (and so to `page.route`). */
export async function holdTurnStream(page: Page): Promise<HeldStream> {
  await page.addInitScript(() => {
    type W = {
      __turn: {
        ctl: ReadableStreamDefaultController<Uint8Array> | null;
        sent: Record<string, unknown>[];
      };
    };
    const w = window as unknown as W;
    w.__turn = { ctl: null, sent: [] };
    const real = window.fetch.bind(window);
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (!/\/api\/missions\/[^/]+\/message\/stream$/.test(url)) {
        return real(input, init);
      }
      w.__turn.sent.push(JSON.parse(String(init?.body ?? "{}")));
      const body = new ReadableStream<Uint8Array>({
        start(c) {
          w.__turn.ctl = c;
        },
      });
      return Promise.resolve(
        new Response(body, {
          status: 200,
          headers: { "content-type": "application/x-ndjson" },
        }),
      );
    };
  });
  return {
    push: (ev) =>
      page.evaluate((e) => {
        const w = window as unknown as {
          __turn: { ctl: ReadableStreamDefaultController<Uint8Array> };
        };
        w.__turn.ctl.enqueue(
          new TextEncoder().encode(JSON.stringify(e) + "\n"),
        );
      }, ev),
    close: () =>
      page.evaluate(() => {
        const w = window as unknown as {
          __turn: { ctl: ReadableStreamDefaultController<Uint8Array> };
        };
        w.__turn.ctl.close();
      }),
    sent: () =>
      page.evaluate(
        () =>
          (window as unknown as { __turn: { sent: Record<string, unknown>[] } })
            .__turn.sent,
      ),
  };
}
