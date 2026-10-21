import { expect, test } from "vitest";
import { termWsUrl } from "./termUrl";

// jsdom serves location as http://localhost:3000 → ws (not wss).
test("builds an attach URL carrying the resume offset", () => {
  expect(termWsUrl("claude", "abc-123", 0)).toBe(
    `ws://${location.host}/ws/term/claude:abc-123?have=0`,
  );
  expect(termWsUrl("claude", "abc-123", 4096)).toContain("?have=4096");
});

test("a fresh session adds new=1, cwd and bypass", () => {
  const url = termWsUrl("claude", "id1", 0, {
    cwd: "/home/m/proj",
    bypass: true,
  });
  const q = new URL(url.replace(/^ws/, "http")).searchParams;
  expect(q.get("new")).toBe("1");
  expect(q.get("cwd")).toBe("/home/m/proj");
  expect(q.get("bypass")).toBe("1");
  expect(q.get("have")).toBe("0");
});

test("bypass=false is forwarded as 0", () => {
  const url = termWsUrl("claude", "id1", 0, { cwd: "/x", bypass: false });
  expect(new URL(url.replace(/^ws/, "http")).searchParams.get("bypass")).toBe(
    "0",
  );
});

test("engine and id are URL-encoded into the path segment", () => {
  expect(termWsUrl("open code", "a/b", 0)).toContain(
    "/ws/term/open%20code:a%2Fb?",
  );
});

test("the device label is forwarded for the take-over gate, omitted when absent (#293)", () => {
  const url = termWsUrl("claude", "id1", 0, undefined, {
    label: "Mac · Chrome",
  });
  expect(new URL(url.replace(/^ws/, "http")).searchParams.get("label")).toBe(
    "Mac · Chrome",
  );
  const bare = new URL(termWsUrl("claude", "id1", 0).replace(/^ws/, "http"))
    .searchParams;
  expect(bare.has("label")).toBe(false);
});

test("the initial grid (cols/rows) is forwarded so the server sizes the pty up front (#227)", () => {
  const url = termWsUrl("claude", "id1", 0, undefined, { cols: 96, rows: 30 });
  const q = new URL(url.replace(/^ws/, "http")).searchParams;
  expect(q.get("cols")).toBe("96");
  expect(q.get("rows")).toBe("30");
  // Omitted when unknown (no zero/NaN params leak through).
  const bare = new URL(termWsUrl("claude", "id1", 0).replace(/^ws/, "http"))
    .searchParams;
  expect(bare.has("cols")).toBe(false);
  expect(bare.has("rows")).toBe(false);
});

test("a chosen model rides the fresh launch; default sends nothing (#1189)", () => {
  const q = (fresh: Parameters<typeof termWsUrl>[3]) =>
    new URL(termWsUrl("claude", "id1", 0, fresh).replace(/^ws/, "http"))
      .searchParams;
  expect(q({ cwd: "/x", bypass: true, model: "claude-opus-5" }).get("model")).toBe(
    "claude-opus-5",
  );
  expect(q({ cwd: "/x", bypass: true, model: "default" }).has("model")).toBe(false);
  expect(q({ cwd: "/x", bypass: true }).has("model")).toBe(false);
  // An attach never carries one: attaching never changes a running session's model.
  expect(q(undefined).has("model")).toBe(false);
  // A hostile value is still ONE query parameter; the server refuses it.
  const hostile = q({ cwd: "/x", bypass: true, model: "x&bypass=1 --yolo" });
  expect(hostile.get("model")).toBe("x&bypass=1 --yolo");
  expect(hostile.getAll("bypass")).toEqual(["1"]);
});
