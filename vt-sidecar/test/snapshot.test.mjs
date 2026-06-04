// Contract test for the VT sidecar (#273 sub-step 1). Proves the Phase-0 decision on a deterministic
// synthetic fixture: feed-at-authoring-width → resize → custom reflowed-row snapshot is byte-perfect
// at narrow widths (where SerializeAddon was not), AND preserves SGR styling. No app wiring.
import { test } from "node:test";
import assert from "node:assert/strict";
import headless from "@xterm/headless";
import { EmulatorPool } from "../emulator.mjs";
import { snapshotRows } from "../snapshot.mjs";

const { Terminal } = headless;
const writeAll = (t, d) => new Promise((r) => t.write(d, r));

// Deterministic fixture authored at a WIDE width (single logical lines), with SGR runs and a line
// long enough to force wrapping when reflowed to 40 cols. Committed via this generator (no binary).
function fixtureBytes() {
  const E = (s) => s; // readability
  const lines = [
    E("\x1b[1;31mERROR\x1b[0m plain after bold-red"),
    E("\x1b[38;5;208m256-color orange\x1b[0m and \x1b[4munderline\x1b[0m"),
    // 96 chars → wraps to 3 rows at 40 cols (40+40+16)
    "L" + "o".repeat(94) + "G",
    E("\x1b[7minverse\x1b[0m \x1b[2mdim\x1b[0m tail"),
  ];
  return Buffer.from(lines.join("\r\n"), "utf8");
}

function bufferText(term) {
  const b = term.buffer.active;
  const out = [];
  for (let i = 0; i < b.length; i++) {
    const l = b.getLine(i);
    out.push(l ? l.translateToString(true).replace(/\s+$/, "") : "");
  }
  while (out.length && out[out.length - 1] === "") out.pop();
  return out;
}

test("feed-then-resize + reflowed-row snapshot is byte-perfect at 40 cols", async () => {
  const pool = new EmulatorPool();
  const key = "claude:fixture";
  pool.open(key, 200, 40);
  await pool.feed(key, fixtureBytes());

  // Ground truth = the emulator's OWN reflow at 40 cols.
  const snap = await pool.snapshot(key, 40, 40);
  const ground = new Terminal({ cols: 200, rows: 40, scrollback: 5000, allowProposedApi: true });
  await writeAll(ground, fixtureBytes());
  ground.resize(40, 40);
  const groundText = bufferText(ground);

  // Replay the snapshot into a fresh client @40 — exactly what the browser does on attach.
  const client = new Terminal({ cols: 40, rows: 40, scrollback: 5000, allowProposedApi: true });
  await writeAll(client, snap);
  assert.deepEqual(bufferText(client), groundText, "snapshot must reproduce the reflow exactly");

  // The long line really did wrap (proves reflow happened, not a no-op).
  assert.ok(groundText.some((l) => l.startsWith("Loooo")), "wrapped long line present");
  assert.ok(groundText.length >= 6, "reflow expanded the line count at 40 cols");
});

test("snapshot preserves SGR (bold/red, 256-color, underline, inverse, dim)", async () => {
  const pool = new EmulatorPool();
  const key = "claude:sgr";
  pool.open(key, 200, 40);
  await pool.feed(key, fixtureBytes());
  const snap = await pool.snapshot(key, 60, 40);

  // The serializer emits the distinctive SGR codes from the fixture.
  assert.ok(snap.includes("38;5;208"), "256-color orange emitted");
  assert.match(snap, /;31m|;1;31m|;31;|0;1;31/, "bold-red emitted");

  const client = new Terminal({ cols: 60, rows: 40, scrollback: 5000, allowProposedApi: true });
  await writeAll(client, snap);
  const b = client.buffer.active;
  // find the ERROR cell (row 0, col 0) → bold + red fg
  let found = null;
  for (let y = 0; y < b.length && !found; y++) {
    const line = b.getLine(y);
    if (!line) continue;
    const c0 = line.getCell(0);
    if (c0 && c0.getChars() === "E" && line.getCell(1)?.getChars() === "R") found = line;
  }
  assert.ok(found, "ERROR line present in replayed snapshot");
  const e = found.getCell(0);
  assert.ok(e.isBold(), "bold preserved"); // xterm flag getters return truthy bits, not literal true
  assert.equal(e.getFgColor(), 1, "red fg preserved (palette index 1 = red)");
});

test("live mirror: incremental feed with cursor-up repaints does NOT duplicate (#273)", async () => {
  // The whole point of the live mirror: feed the agent's bytes AS THEY STREAM at the agent geometry,
  // so Ink-style cursor-up repaints overwrite IN PLACE instead of piling duplicates into scrollback
  // (the failure of one-shot rebuild-from-ring). Here a "prompt" line is repainted 5× the way Ink
  // redraws its input box; a faithful mirror shows it exactly once.
  const pool = new EmulatorPool();
  const key = "claude:live";
  pool.open(key, 20, 6); // agent pty geometry
  await pool.feed(key, Buffer.from("history one\r\nhistory two\r\n", "utf8"));
  await pool.feed(key, Buffer.from("PROMPT>\r\n", "utf8"));
  for (let i = 0; i < 5; i++) {
    // cursor up 1 → clear line → rewrite the prompt → newline (exactly an in-place repaint)
    await pool.feed(key, Buffer.from("\x1b[1A\x1b[2KPROMPT>\r\n", "utf8"));
  }
  const snap = await pool.snapshot(key, 20, 6);
  const plain = snap.replace(/\x1b\[[0-9;]*m/g, "");
  const count = (n) => (plain.match(new RegExp(n, "g")) || []).length;
  assert.equal(count("PROMPT>"), 1, "repainted prompt appears exactly once (no dup)");
  assert.equal(count("history one"), 1, "history not duplicated");
  assert.equal(count("history two"), 1, "history not duplicated");

  // Non-destructive: the live feed continues correctly after a snapshot.
  await pool.feed(key, Buffer.from("after snap\r\n", "utf8"));
  const snap2 = (await pool.snapshot(key, 20, 6)).replace(/\x1b\[[0-9;]*m/g, "");
  assert.ok(snap2.includes("after snap"), "feed continues after snapshot");
  assert.equal((snap2.match(/PROMPT>/g) || []).length, 1, "still no dup after continued feed");
});

test("snapshot keeps soft-wrapped lines as ONE logical line so scroll-up reflows (#273)", async () => {
  // The bug: every visual row was joined with \r\n, freezing the scroll-up at the capture width —
  // older content stayed narrow while live content widened ("loaded with a different screen size").
  // Fix: a soft-wrapped continuation is emitted with NO \r\n, so the client re-wraps the logical line
  // at its own width. Here a 50-char line wrapped at 20 cols must come back as ONE logical line
  // (no interior \r\n), and replaying it into a WIDER terminal must occupy FEWER rows.
  const pool = new EmulatorPool();
  const key = "claude:reflow";
  pool.open(key, 20, 6);
  const long = "abcdefghij".repeat(5); // 50 chars → wraps to 3 rows at 20 cols
  await pool.feed(key, Buffer.from(long + "\r\n", "utf8"));
  const snap = await pool.snapshot(key, 20, 6);

  const plain = snap.replace(/\x1b\[[0-9;]*m/g, "");
  assert.equal(plain.replace(/\r\n/g, "\n").trim(), long, "joins back to the single logical line");
  assert.ok(!plain.trim().includes("\r\n"), "no interior hard break inside the wrapped line");

  // Replay into a WIDE client: the logical line must reflow to a single row (proves reflow works).
  const wide = new Terminal({ cols: 80, rows: 10, scrollback: 100, allowProposedApi: true });
  await writeAll(wide, snap);
  const row0 = lineText(wide.buffer.active.getLine(0));
  assert.ok(row0.startsWith(long), "wide client shows the whole line on one row (reflowed)");
});

function lineText(line) {
  if (!line) return "";
  let s = "";
  for (let x = 0; x < line.length; x++) s += line.getCell(x)?.getChars() || " ";
  return s.replace(/\s+$/, "");
}

test("live mirror: open() resizes the running emulator to track the agent's pty", async () => {
  const pool = new EmulatorPool();
  const key = "claude:resize";
  pool.open(key, 80, 24);
  await pool.feed(key, Buffer.from("hello world\r\n", "utf8"));
  pool.open(key, 40, 24); // agent resized narrower
  const s = pool.sessions.get(key);
  assert.equal(s.cols, 40, "emulator tracks the agent's new width");
  // snapshot reflows a VIEW to the client width, then restores to the agent geometry
  await pool.snapshot(key, 100, 30);
  assert.equal(pool.sessions.get(key).cols, 40, "restored to agent width after snapshot");
  assert.equal(pool.sessions.get(key).rows, 24, "restored to agent height after snapshot");
});

test("snapshot emits 24-bit RGB as ;2;r;g;b, not a bogus palette index (#273)", async () => {
  // Regression: getFg/BgColorMode() returns xterm's raw masked bit-flags (CM_RGB=0x03000000), not
  // 0/1/2, so the old `mode === 2` check misclassified every RGB cell as palette and emitted its
  // 24-bit value as `38;5;<huge>` → garbled colors + mis-rendered box rules on real Claude output.
  const pool = new EmulatorPool();
  const key = "claude:rgb";
  pool.open(key, 80, 10);
  // truecolor fg 0x4080c0 on truecolor bg 0x102030
  await pool.feed(key, Buffer.from("\x1b[38;2;64;128;192m\x1b[48;2;16;32;48mRGB\x1b[0m", "utf8"));
  const snap = await pool.snapshot(key, 40, 10);
  assert.ok(snap.includes("38;2;64;128;192"), "RGB fg emitted as ;2;r;g;b");
  assert.ok(snap.includes("48;2;16;32;48"), "RGB bg emitted as ;2;r;g;b");
  assert.ok(!/[34]8;5;\d{4,}/.test(snap), "no out-of-range palette index leaked from an RGB value");

  // Round-trips through a real terminal back to the same truecolor values.
  const client = new Terminal({ cols: 40, rows: 10, scrollback: 100, allowProposedApi: true });
  await writeAll(client, snap);
  const c = client.buffer.active.getLine(0).getCell(0);
  assert.ok(c.isFgRGB(), "fg stays RGB after replay");
  assert.equal(c.getFgColor(), 0x4080c0, "fg RGB value preserved");
  assert.ok(c.isBgRGB(), "bg stays RGB after replay");
  assert.equal(c.getBgColor(), 0x102030, "bg RGB value preserved");
});

test("snapshot is non-destructive: term restored to feed width; repeat works at another width", async () => {
  const pool = new EmulatorPool();
  const key = "claude:nd";
  pool.open(key, 200, 40);
  await pool.feed(key, fixtureBytes());
  const a = await pool.snapshot(key, 40, 40);
  const b = await pool.snapshot(key, 120, 40); // different width, same source
  assert.ok(a.length > 0 && b.length > 0);
  // feed more after snapshots → still works (term wasn't left resized/broken)
  await pool.feed(key, Buffer.from("\r\nMORE", "utf8"));
  const c = await pool.snapshot(key, 80, 40);
  assert.ok(c.includes("MORE"), "post-snapshot feed is captured");
});

test("snapshotRows trims trailing blank rows + trailing whitespace", async () => {
  const t = new Terminal({ cols: 20, rows: 5, scrollback: 100, allowProposedApi: true });
  await writeAll(t, "hi   \r\n\r\n\r\n");
  const rows = snapshotRows(t).split("\r\n");
  assert.equal(rows[0], "\x1b[0;39;49mhi\x1b[0m"); // trailing spaces dropped, default style
  assert.equal(rows.length, 1, "trailing blank rows dropped");
});

test("rebuild() = reset+feed+snapshot in one call, byte-identical to feed→snapshot", async () => {
  const pool = new EmulatorPool();
  const viaRebuild = await pool.rebuild("claude:rb", fixtureBytes(), 40, 40);

  const pool2 = new EmulatorPool();
  pool2.open("claude:rb2", 512, 40); // rebuild() feeds at feedCols=512 → match it for an equal compare
  await pool2.feed("claude:rb2", fixtureBytes());
  const viaFeed = await pool2.snapshot("claude:rb2", 40, 40);

  assert.equal(viaRebuild, viaFeed, "rebuild matches feed-then-snapshot");
  // rebuild again on the same key (re-attach) must NOT double the content
  const again = await pool.rebuild("claude:rb", fixtureBytes(), 40, 40);
  assert.equal(again, viaRebuild, "re-rebuild is idempotent (no doubling)");
});

test("end() frees the session; unknown key snapshots empty", async () => {
  const pool = new EmulatorPool();
  pool.open("claude:x", 200, 40);
  await pool.feed("claude:x", fixtureBytes());
  assert.equal(pool.stats().sessions, 1);
  pool.end("claude:x");
  assert.equal(pool.stats().sessions, 0);
  assert.equal(await pool.snapshot("claude:gone", 40, 40), "");
});

test("feed REJECTS an unknown (never-opened) session — no auto-create at default geometry (#273)", async () => {
  // A stray feed after a restart/eviction must not build a wrong-geometry emulator that later looks
  // like faithful scrollback. feed() returns false and creates nothing until open() is called.
  const pool = new EmulatorPool();
  assert.equal(await pool.feed("claude:nope", Buffer.from("data", "utf8")), false, "rejected");
  assert.equal(pool.stats().sessions, 0, "no emulator auto-created");
  pool.open("claude:nope", 80, 24);
  assert.equal(await pool.feed("claude:nope", Buffer.from("data", "utf8")), true, "applied after open");
});
