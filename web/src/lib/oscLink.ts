/** OSC 8 hyperlinks in agent output (#2024): one activation policy for touch and desktop.
 *
 *  An OSC 8 link's target is invisible: the agent prints any text and attaches any URL to it.
 *  Claude Code prints its /login URL as hard-wrapped rows, each its own OSC 8 link to the COMPLETE
 *  URL, joined by cursor moves rather than soft wraps — so the visible text of any one row is a
 *  fragment that can't be opened, and in general the visible text proves nothing about where the
 *  link goes (`https://docs.example/login` is a substring of
 *  `https://evil.example/?next=https://docs.example/login`). So:
 *
 *  - every activation asks with the native confirm(), showing the full target — xterm 5.5's own
 *    default for OSC 8, without its `window.open()` + `location` dance;
 *  - only http(s) targets are offered; any other scheme, or a target that doesn't parse, is
 *    refused without asking;
 *  - an accepted target opens through `openTerminalLink`, the one terminal-link opener (#1232);
 *  - a cancelled or refused OSC 8 link opens NOTHING. The caller must not fall back to the
 *    plain-text URL under the finger (#664) — that is only for cells with no OSC 8 link at all.
 *
 *  Where the target comes from: xterm's own OSC 8 data. xterm stores the link id on each cell it
 *  prints while an OSC 8 link is open (so erasing or overwriting the cell drops the link, and
 *  reflow carries it), and its built-in OscLinkProvider turns a buffer row into link ranges for
 *  the desktop linkifier. The touch overlay (#415) swallows taps before that linkifier sees them,
 *  so `oscLinkAtCell` asks the same provider for the tapped row. */
import type { IBufferRange, Terminal } from "@xterm/xterm";
import { openTerminalLink } from "./terminalLink";

export type OscLinkOutcome = "opened" | "cancelled" | "refused";

/** The URL an OSC 8 target may open, normalised exactly as it will be opened — or null when it
 *  is not an http(s) URL. Whitespace and control characters are refused outright rather than
 *  left to a browser's URL parser to repair (Chrome percent-encodes a space in a host). */
export function oscLinkHref(target: string): string | null {
  // eslint-disable-next-line no-control-regex -- refusing C0/DEL controls is the point
  if (!target || /[\s\x00-\x1f\x7f]/.test(target)) return null;
  let url: URL;
  try {
    url = new URL(target);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (!url.hostname) return null;
  return url.href;
}

/** The confirm() text: the full destination, nothing the agent chose to display. */
export function oscLinkPrompt(href: string): string {
  return `Open this link from the terminal?\n\n${href}`;
}

/** Activate an OSC 8 link: refuse, or ask with the full target and open it on OK. */
export function activateOscLink(
  target: string,
  newTab = false,
  ask: (message: string) => boolean = (m) => window.confirm(m),
): OscLinkOutcome {
  const href = oscLinkHref(target);
  if (href === null) return "refused";
  if (!ask(oscLinkPrompt(href))) return "cancelled";
  openTerminalLink(href, newTab);
  return "opened";
}

/** The `linkHandler` option for the xterm instance: the desktop click path into the policy.
 *  `allowNonHttpProtocols` makes xterm hand us EVERY OSC 8 link so the policy refuses the
 *  non-http ones itself. Left false, xterm drops them from its provider and the click falls
 *  through to WebLinksAddon, which opens whatever URL the agent printed as visible text. */
export const OSC_LINK_HANDLER = {
  activate: (e: MouseEvent, target: string): void => {
    activateOscLink(target, e.metaKey || e.ctrlKey);
  },
  allowNonHttpProtocols: true,
};

interface OscLinkServiceLike {
  getLinkData(linkId: number): { uri: string } | undefined;
}
/** What `IBufferLine.getCell` returns at runtime in xterm 5.5: its internal CellData, which carries
 *  the cell's OSC 8 link id. Not public API; read defensively. */
interface CellWithLink {
  hasExtendedAttrs?: () => number | boolean;
  extended?: { urlId?: number };
}

/** xterm's OSC link service — the id → target table every OSC 8 cell points into. Reached through
 *  xterm's built-in OscLinkProvider, the first registered link provider and the one holding the
 *  service. Not public API: pinned against the installed xterm by oscLink.test.ts; if a version
 *  moves it, the lookup returns null and taps fall back to the plain-text path (#664). */
function oscLinkService(term: Terminal): OscLinkServiceLike | null {
  const core = (term as unknown as {
    _core?: { _linkProviderService?: { linkProviders?: unknown[] } };
  })._core;
  const first = core?._linkProviderService?.linkProviders?.[0] as
    | { _oscLinkService?: Partial<OscLinkServiceLike> }
    | undefined;
  const service = first?._oscLinkService;
  if (!service || typeof service.getLinkData !== "function") return null;
  return service as OscLinkServiceLike;
}

/** The OSC 8 link id stored on one cell of the active buffer, or 0 for none. */
function linkIdAt(term: Terminal, absRow: number, col: number): number {
  const cell = term.buffer.active.getLine(absRow)?.getCell(col) as CellWithLink | undefined;
  if (!cell || typeof cell.hasExtendedAttrs !== "function" || !cell.hasExtendedAttrs()) return 0;
  const id = cell.extended?.urlId;
  return typeof id === "number" && id > 0 ? id : 0;
}

/** The raw OSC 8 target on the active buffer's cell (absolute row, 0-based column), or null when
 *  no OSC 8 link covers that cell. Not filtered: the caller runs it through `activateOscLink`,
 *  which refuses what it can't open — and a refused link must not fall back to the fragment.
 *
 *  Read from the CELL itself, not from xterm's OscLinkProvider ranges: in xterm 5.5 that provider
 *  never emits a link whose first cell is the row's last occupied cell (it `continue`s past the
 *  only flush), so a one-cell final fragment of a hard-wrapped URL had no target (Hermes on
 *  PR #2030). Per-cell data has no such edge. */
export function oscLinkAtCell(term: Terminal, absRow: number, col: number): string | null {
  if (absRow < 0 || col < 0) return null;
  const service = oscLinkService(term);
  if (!service) return null;
  const id = linkIdAt(term, absRow, col);
  if (!id) return null;
  return service.getLinkData(id)?.uri ?? null;
}

/** The OSC 8 runs of one row as link ranges (1-based, inclusive), from per-cell data — every run,
 *  including a one-cell run in the row's last occupied cell. */
export function oscLinkRanges(
  term: Terminal,
  absRow: number,
): { text: string; range: IBufferRange }[] {
  const service = oscLinkService(term);
  const line = term.buffer.active.getLine(absRow);
  if (!service || !line) return [];
  const out: { text: string; range: IBufferRange }[] = [];
  const width = line.length;
  let start = -1;
  let current = 0;
  const flush = (endExclusive: number) => {
    const uri = current ? service.getLinkData(current)?.uri : undefined;
    if (uri && start >= 0) {
      out.push({
        text: uri,
        range: { start: { x: start + 1, y: absRow + 1 }, end: { x: endExclusive, y: absRow + 1 } },
      });
    }
  };
  for (let x = 0; x <= width; x++) {
    const id = x < width ? linkIdAt(term, absRow, x) : 0;
    if (id === current) continue;
    if (current) flush(x);
    current = id;
    start = id ? x : -1;
  }
  return out;
}

/** A link provider for the desktop linkifier that offers OSC 8 runs from per-cell data. xterm's
 *  own provider is registered first and still answers for every run it sees; this one covers the
 *  run it drops (a link starting in the row's last occupied cell), so a click on ANY row of an
 *  OSC 8 link reaches the same policy (#2024). Returns the disposable. */
export function registerOscTailLinks(term: Terminal): { dispose(): void } {
  // A terminal without link providers (a stand-in in tests, a future build) just goes without.
  if (typeof term.registerLinkProvider !== "function") return { dispose() {} };
  return term.registerLinkProvider({
    provideLinks(y, callback) {
      const links = oscLinkRanges(term, y - 1).map((l) => ({
        text: l.text,
        range: l.range,
        activate: (e: MouseEvent, text: string) => {
          activateOscLink(text, e.metaKey || e.ctrlKey);
        },
      }));
      callback(links.length ? links : undefined);
    },
  });
}
