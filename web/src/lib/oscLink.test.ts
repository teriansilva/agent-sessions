import { Terminal } from "@xterm/xterm";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  activateOscLink,
  OSC_LINK_HANDLER,
  oscLinkAtCell,
  oscLinkHref,
  oscLinkRanges,
  oscLinkPrompt,
} from "./oscLink";

// #2024. The policy half is plain logic; the lookup half runs the REAL xterm parser and buffer
// (headless — no renderer needed), because what matters is what xterm does to its per-cell link
// data under erase, overwrite, the alternate buffer and reflow. Synthetic URLs only.

afterEach(() => vi.unstubAllGlobals());

const ESC = "\x1b";
const BEL = "\x07";
const open8 = (uri: string, id?: string) => `${ESC}]8;${id ? `id=${id}` : ""};${uri}${BEL}`;
const close8 = `${ESC}]8;;${BEL}`;
const link = (uri: string, text: string, id?: string) => `${open8(uri, id)}${text}${close8}`;

function term(cols = 40, rows = 12): Terminal {
  return new Terminal({ cols, rows, linkHandler: OSC_LINK_HANDLER, allowProposedApi: true });
}
const write = (t: Terminal, data: string) =>
  new Promise<void>((resolve) => t.write(data, resolve));
/** The OSC 8 target of every cell in [from, to) on an absolute row. */
const targets = (t: Terminal, row: number, from: number, to: number) =>
  Array.from({ length: to - from }, (_, i) => oscLinkAtCell(t, row, from + i));
const rowText = (t: Terminal, row: number) =>
  t.buffer.active.getLine(row)?.translateToString(true) ?? "";

describe("oscLinkHref: only http(s) targets are offered", () => {
  it.each([
    "https://claude.example/oauth/authorize?code=true&state=SYNTHETIC",
    "http://localhost:8080/x",
  ])("offers %s as-is", (u) => {
    expect(oscLinkHref(u)).toBe(u);
  });

  it.each([
    "javascript:alert(1)",
    "file:///etc/passwd",
    "data:text/html,<b>x</b>",
    "mailto:a@example.com",
    "ftp://files.example/x",
  ])("refuses the non-http scheme %s", (u) => {
    expect(oscLinkHref(u)).toBeNull();
  });

  it.each([
    "",
    "not a url",
    "http://[oops",
    "https://",
    "https://exa mple.example/",
    "https://example.com/\tx",
    "https://example.com/\x7f",
  ])("refuses the malformed target %j", (u) => {
    expect(oscLinkHref(u)).toBeNull();
  });
});

describe("activateOscLink: confirm with the full target, open through openTerminalLink", () => {
  const TARGET =
    "https://claude.example/oauth/authorize?code=true&client_id=00000000-test&state=SYNTHETIC";

  it("asks with the full target and opens it on OK (new tab, no opener, no referrer)", () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const ask = vi.fn(() => true);
    expect(activateOscLink(TARGET, false, ask)).toBe("opened");
    expect(ask).toHaveBeenCalledWith(oscLinkPrompt(TARGET));
    expect(ask.mock.calls[0][0]).toContain(TARGET);
    expect(open).toHaveBeenCalledWith(TARGET, "_blank", "noopener,noreferrer");
  });

  it("opens nothing on Cancel", () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    expect(activateOscLink(TARGET, false, () => false)).toBe("cancelled");
    expect(open).not.toHaveBeenCalled();
  });

  it("the spoofing example: the prompt names the real destination, Cancel opens nothing", () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const target = "https://evil.example/?next=https://docs.example/login";
    const ask = vi.fn(() => false);
    expect(activateOscLink(target, false, ask)).toBe("cancelled");
    expect(ask.mock.calls[0][0]).toContain(target);
    expect(open).not.toHaveBeenCalled();
  });

  it.each(["javascript:alert(1)", "http://[oops"])(
    "refuses %s without asking or opening",
    (target) => {
      const open = vi.fn();
      vi.stubGlobal("open", open);
      const ask = vi.fn(() => true);
      expect(activateOscLink(target, false, ask)).toBe("refused");
      expect(ask).not.toHaveBeenCalled();
      expect(open).not.toHaveBeenCalled();
    },
  );

  it("the desktop linkHandler passes ⌘/ctrl through as the new-tab request", () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    vi.stubGlobal("confirm", () => true);
    const here = `${window.location.origin}/s/claude/abc`;
    OSC_LINK_HANDLER.activate(new MouseEvent("click", { ctrlKey: true }), here);
    // A BattleLab link opens in-app unless a new tab was asked for (#1232).
    expect(open).toHaveBeenCalledWith(here, "_blank", "noopener");
  });

  it("the desktop linkHandler asks too, and refuses what isn't http(s)", () => {
    const open = vi.fn();
    const confirm = vi.fn(() => false);
    vi.stubGlobal("open", open);
    vi.stubGlobal("confirm", confirm);
    OSC_LINK_HANDLER.activate(new MouseEvent("click"), TARGET);
    expect(confirm).toHaveBeenCalledWith(oscLinkPrompt(TARGET));
    OSC_LINK_HANDLER.activate(new MouseEvent("click"), "javascript:alert(1)");
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(open).not.toHaveBeenCalled();
    // xterm must hand us non-http links, or it drops them and WebLinksAddon opens the visible text.
    expect(OSC_LINK_HANDLER.allowNonHttpProtocols).toBe(true);
  });
});

describe("oscLinkAtCell against the real xterm parser", () => {
  const FULL =
    "https://claude.example/oauth/authorize?code=true&client_id=00000000-test&scope=user%3Ainference&state=SYNTHETIC0000";

  it("Claude's hard-wrapped /login rows: every cell of every row resolves to the FULL target", async () => {
    const t = term(40, 12);
    const chunks = FULL.match(/.{1,30}/g)!;
    // The recorded shape: each row its own OSC 8 link to the whole URL, rows joined by `\r ESC[1B`.
    await write(
      t,
      `Sign in:\r\n${chunks.map((c) => `${ESC}[2C${link(FULL, c, "15azii9")}\r${ESC}[1B`).join("")}`,
    );
    chunks.forEach((c, i) => {
      const row = 1 + i;
      expect(rowText(t, row)).toBe(`  ${c}`);
      expect(targets(t, row, 2, 2 + c.length)).toEqual(Array(c.length).fill(FULL));
      expect(oscLinkAtCell(t, row, 0)).toBeNull(); // the indent is not the link
      expect(oscLinkAtCell(t, row, 2 + c.length)).toBeNull(); // nor past its end
      // Not soft-wrapped: the #664 join can't see these rows as one line.
      expect(t.buffer.active.getLine(row)?.isWrapped).toBe(false);
    });
    expect(oscLinkAtCell(t, 0, 2)).toBeNull(); // the header has no link
  });

  it("returns non-http targets too, so the caller refuses them instead of falling back", async () => {
    const t = term();
    await write(t, link("javascript:alert(1)", "https://docs.example/x"));
    expect(oscLinkAtCell(t, 0, 3)).toBe("javascript:alert(1)");
  });

  it("multiple spans on one row resolve to their own targets; the gap to none", async () => {
    const t = term();
    await write(t, `${link("https://a.example/", "AAAA")} - ${link("https://b.example/", "BBBB")}`);
    expect(targets(t, 0, 0, 4)).toEqual(Array(4).fill("https://a.example/"));
    expect(targets(t, 0, 4, 7)).toEqual([null, null, null]);
    expect(targets(t, 0, 7, 11)).toEqual(Array(4).fill("https://b.example/"));
  });

  it("adjacent spans with no gap stay distinct", async () => {
    const t = term();
    await write(t, `${link("https://a.example/", "AAAA")}${link("https://b.example/", "BBBB")}x`);
    expect(targets(t, 0, 0, 8)).toEqual([
      ...Array(4).fill("https://a.example/"),
      ...Array(4).fill("https://b.example/"),
    ]);
  });

  it("erasing the linked cells (EL / ED) with no further OSC 8 leaves nothing clickable", async () => {
    const t = term();
    await write(t, `${link("https://a.example/", "AAAAAAAA")}\r\n${link("https://b.example/", "BBBB")}`);
    await write(t, `${ESC}[1;1H${ESC}[2K`); // erase line 1
    expect(targets(t, 0, 0, 8)).toEqual(Array(8).fill(null));
    expect(oscLinkAtCell(t, 1, 0)).toBe("https://b.example/");
    await write(t, `${ESC}[2J`); // erase the screen
    expect(oscLinkAtCell(t, 1, 0)).toBeNull();
    // Fresh text where the links were is plain.
    await write(t, `${ESC}[1;1Hplain text here`);
    expect(targets(t, 0, 0, 15)).toEqual(Array(15).fill(null));
  });

  it("overwriting part of a link unlinks exactly the overwritten cells", async () => {
    const t = term();
    await write(t, link("https://a.example/", "AAAAAAAA"));
    await write(t, "\rxyz");
    expect(targets(t, 0, 0, 8)).toEqual([null, null, null, ...Array(5).fill("https://a.example/")]);
  });

  it("alternate buffer: the alt screen has no normal-buffer links; leaving it restores them and drops its own", async () => {
    const t = term();
    await write(t, link("https://normal.example/", "NNNN"));
    await write(t, `${ESC}[?1049h`);
    expect(oscLinkAtCell(t, 0, 1)).toBeNull();
    await write(t, `${ESC}[H${link("https://alt.example/", "AAAA")}`);
    expect(oscLinkAtCell(t, 0, 1)).toBe("https://alt.example/");
    await write(t, `${ESC}[?1049l`);
    expect(oscLinkAtCell(t, 0, 1)).toBe("https://normal.example/");
    await write(t, `${ESC}[?1049h`); // the alt screen comes back cleared
    expect(oscLinkAtCell(t, 0, 1)).toBeNull();
  });

  it("resize / reflow carries the link with its cells", async () => {
    const t = term(20, 12);
    const text = "L".repeat(30); // soft-wraps at 20 columns
    // Then off the line: xterm doesn't reflow the line the cursor is on.
    await write(t, `ab${link("https://wrap.example/", text)}\r\n`);
    expect(oscLinkAtCell(t, 0, 5)).toBe("https://wrap.example/");
    expect(oscLinkAtCell(t, 1, 5)).toBe("https://wrap.example/");
    t.resize(40, 12); // reflow joins the two rows
    expect(rowText(t, 0)).toBe(`ab${text}`);
    expect(targets(t, 0, 0, 2)).toEqual([null, null]);
    expect(targets(t, 0, 2, 32)).toEqual(Array(30).fill("https://wrap.example/"));
    expect(oscLinkAtCell(t, 1, 0)).toBeNull();
    t.resize(10, 12); // and shrinking splits it again
    expect(oscLinkAtCell(t, 0, 1)).toBeNull();
    expect(oscLinkAtCell(t, 0, 5)).toBe("https://wrap.example/");
    expect(oscLinkAtCell(t, 3, 1)).toBe("https://wrap.example/");
  });

  it("(a) an incomplete OSC control sequence links nothing", async () => {
    const t = term();
    // No BEL / ST: the parser is still inside the OSC string, so this text is its payload.
    await write(t, `pre ${ESC}]8;;https://evil.example/swallowed`);
    expect(rowText(t, 0)).toBe("pre ");
    expect(targets(t, 0, 0, 10)).toEqual(Array(10).fill(null));
    // CAN aborts the sequence; what follows prints as plain text.
    await write(t, "\x18after");
    expect(rowText(t, 0)).toBe("pre after");
    expect(targets(t, 0, 0, 10)).toEqual(Array(10).fill(null));
  });

  it("(b) a valid OSC 8 open never closed links exactly the cells printed after it, until erased", async () => {
    const t = term();
    await write(t, `pre ${open8("https://open.example/")}tail\r\nnext`);
    expect(targets(t, 0, 0, 4)).toEqual([null, null, null, null]);
    expect(targets(t, 0, 4, 8)).toEqual(Array(4).fill("https://open.example/"));
    expect(oscLinkAtCell(t, 0, 8)).toBeNull(); // never printed
    expect(targets(t, 1, 0, 4)).toEqual(Array(4).fill("https://open.example/"));
    await write(t, `${ESC}[2J`);
    expect(targets(t, 0, 0, 8)).toEqual(Array(8).fill(null));
    expect(targets(t, 1, 0, 4)).toEqual(Array(4).fill(null));
  });

  it("plain text URLs are not OSC 8 links (the #664 fallback handles them)", async () => {
    const t = term();
    await write(t, "see https://plain.example/x");
    expect(targets(t, 0, 0, 27)).toEqual(Array(27).fill(null));
  });

  it("a one-cell link in a row's LAST occupied cell resolves (xterm's own provider drops it)", async () => {
    // Hermes on PR #2030: OSC8(target) + "X" + close + CRLF gave `null` through the provider,
    // while "XX" resolved. The final fragment of a hard-wrapped login URL can be one character.
    const full = "https://claude.example/oauth/authorize?code=true&state=SYNTHETICtail";
    const t = term(40, 6);
    await write(t, `${link(full, "X", "a")}\r\n${link(full, "XX", "a")}\r\n`);
    expect(rowText(t, 0)).toBe("X");
    expect(oscLinkAtCell(t, 0, 0)).toBe(full);
    expect(oscLinkAtCell(t, 1, 0)).toBe(full); // the positive control
    expect(oscLinkAtCell(t, 1, 1)).toBe(full);
    expect(oscLinkAtCell(t, 0, 1)).toBeNull(); // past the link
  });

  it("a hard-wrapped URL whose final row is a single character: every row resolves", async () => {
    const full = "https://claude.example/oauth/authorize?code=true&state=SYNTHETIC0123456789Z";
    const t = term(25, 10);
    // Claude's shape: each row its own OSC 8 span to the FULL url, joined by cursor moves.
    const rows = full.match(/.{1,24}/g)!;
    expect(rows[rows.length - 1].length).toBeLessThanOrEqual(24);
    const shaped = [...rows.slice(0, -1), "Z"];
    await write(t, shaped.map((r) => `${link(full, r, "w")}\r${ESC}[1B`).join(""));
    shaped.forEach((r, i) => {
      for (let c = 0; c < r.length; c++) expect(oscLinkAtCell(t, i, c)).toBe(full);
    });
  });

  it("adjacent spans ending in a one-cell link keep their own targets", async () => {
    const t = term(30, 4);
    await write(t, `${link("https://a.example/", "aaa")}${link("https://b.example/", "b")}`);
    expect(targets(t, 0, 0, 5)).toEqual([
      "https://a.example/",
      "https://a.example/",
      "https://a.example/",
      "https://b.example/",
      null,
    ]);
  });

  it("the desktop provider's ranges include the one-cell tail run (1-based, inclusive)", async () => {
    const t = term(30, 4);
    await write(t, `xx${link("https://a.example/", "aaa")}${link("https://b.example/", "b")}`);
    expect(oscLinkRanges(t, 0)).toEqual([
      { text: "https://a.example/", range: { start: { x: 3, y: 1 }, end: { x: 5, y: 1 } } },
      { text: "https://b.example/", range: { start: { x: 6, y: 1 }, end: { x: 6, y: 1 } } },
    ]);
    expect(oscLinkRanges(t, 1)).toEqual([]);
  });

  it("finds xterm's own OSC link provider (pins the internal this relies on)", async () => {
    const t = term();
    const providers = (t as unknown as { _core: { _linkProviderService: { linkProviders: object[] } } })
      ._core._linkProviderService.linkProviders;
    expect(providers.length).toBeGreaterThan(0);
    expect("_oscLinkService" in providers[0]).toBe(true);
  });
});
