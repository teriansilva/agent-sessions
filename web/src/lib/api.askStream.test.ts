/** `api.pulseAskStream` (#1171): NDJSON read line by line, whatever the chunking — and a stream
 *  that ends without its final answer is a failure, never a turn left spinning. */
import { afterEach, expect, test } from "vitest";

import { api, ApiError, setApiFetch } from "./api";
import type { PulseAskEvent } from "../types/api";

afterEach(() => setApiFetch(null));

const FINAL = {
  type: "answer",
  final: true,
  answer: "ok — ünïcode",
  matches: [],
  stage: "catalog",
  configured: true,
};
const NDJSON =
  [
    { type: "progress", step: "catalog", sessions: 2, missions: 0 },
    { ...FINAL, final: false, answer: "early" },
    FINAL,
  ]
    .map((l) => JSON.stringify(l))
    .join("\n") + "\n";

function serve(chunks: Uint8Array[], init: ResponseInit = { status: 200 }) {
  setApiFetch(async () => {
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        for (const ch of chunks) c.enqueue(ch);
        c.close();
      },
    });
    return new Response(body, init);
  });
}

async function collect(): Promise<PulseAskEvent[]> {
  const seen: PulseAskEvent[] = [];
  await api.pulseAskStream("q", [], (ev) => seen.push(ev));
  return seen;
}

const bytes = new TextEncoder().encode(NDJSON);

test("lines split across chunks — even inside a multi-byte character — arrive whole", async () => {
  // Every byte its own chunk: splits every line and the UTF-8 sequence of "ü".
  serve(Array.from(bytes, (b) => new Uint8Array([b])));
  const seen = await collect();
  expect(seen.map((e) => e.type)).toEqual(["progress", "answer", "answer"]);
  expect((seen[2] as { answer: string }).answer).toBe("ok — ünïcode");
});

test("coalesced lines in one chunk arrive as separate events, in order", async () => {
  serve([bytes]);
  const seen = await collect();
  expect(seen).toHaveLength(3);
  expect((seen[1] as { final: boolean }).final).toBe(false);
});

test("a stream that ends before its final answer is a failure, after what did arrive", async () => {
  const early = NDJSON.split("\n").slice(0, 2).join("\n") + "\n";
  serve([new TextEncoder().encode(early)]);
  const seen: PulseAskEvent[] = [];
  await expect(
    api.pulseAskStream("q", [], (ev) => seen.push(ev)),
  ).rejects.toThrow(/cut off/i);
  expect(seen).toHaveLength(2); // the Stage-1 answer still reached the page
});

test("a half-written last line is the same cut-off failure, not a JSON error", async () => {
  serve([bytes.slice(0, bytes.length - 20)]);
  await expect(collect()).rejects.toThrow(/cut off/i);
});

test("an error line after the start throws its own status and detail", async () => {
  serve([
    new TextEncoder().encode(
      JSON.stringify({ type: "error", status: 502, detail: "endpoint returned HTTP 502" }) + "\n",
    ),
  ]);
  const err = await collect().catch((e: unknown) => e);
  expect(err).toBeInstanceOf(ApiError);
  expect((err as ApiError).status).toBe(502);
  expect((err as ApiError).message).toBe("endpoint returned HTTP 502");
});

test("a refusal before the stream throws the server's detail, like pulseAsk", async () => {
  setApiFetch(async () =>
    Response.json({ detail: "a question is already running" }, { status: 409 }),
  );
  await expect(collect()).rejects.toThrow("a question is already running");
});
