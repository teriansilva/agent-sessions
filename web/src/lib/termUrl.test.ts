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
