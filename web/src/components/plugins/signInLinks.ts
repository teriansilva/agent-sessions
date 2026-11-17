/** Links in a vendor's sign-in output. The sign-in terminal offers the last one as a button, so a
 *  long OAuth URL that soft-wraps across rows never has to be selected by hand. */
import { URL_RE, type RowLike } from "../../lib/linkHitTest";

export interface ScanBuffer {
  readonly length: number;
  getLine(y: number): RowLike | undefined;
}

/** Only an absolute http(s) URL ever opens; anything else (javascript:, file:, garbage) is null. */
export function safeSignInUrl(uri: string): string | null {
  try {
    const url = new URL(uri);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

/** The last http(s) URL in the buffer, joining soft-wrapped rows into logical lines first. */
export function lastUrl(buf: ScanBuffer): string | null {
  let found: string | null = null;
  let logical = "";
  const flush = () => {
    for (const match of logical.matchAll(URL_RE)) {
      const safe = safeSignInUrl(match[0]);
      if (safe) found = safe;
    }
    logical = "";
  };
  for (let y = 0; y < buf.length; y++) {
    const row = buf.getLine(y);
    if (!row) continue;
    if (!row.isWrapped) flush();
    // Untrimmed, so a URL that fills a row exactly continues into the next one.
    logical += row.translateToString(false);
  }
  flush();
  return found;
}

/** The vendor's output as readable lines (#2106): soft-wrapped rows joined, trailing spaces and
 *  runs of blank lines dropped. The emulator (xterm) already resolved cursor moves and redraws, so
 *  this is the screen's text, never the raw PTY stream with its escape sequences. */
export function screenLines(buf: ScanBuffer): string[] {
  const out: string[] = [];
  let logical: string | null = null;
  const flush = () => {
    if (logical === null) return;
    const line = logical.replace(/\s+$/, "");
    if (line || (out.length > 0 && out[out.length - 1] !== "")) out.push(line);
    logical = null;
  };
  for (let y = 0; y < buf.length; y++) {
    const row = buf.getLine(y);
    if (!row) continue;
    if (!row.isWrapped) flush();
    logical = (logical ?? "") + row.translateToString(false);
  }
  flush();
  while (out.length && out[out.length - 1] === "") out.pop();
  return out;
}

/** One line split into plain text and safe http(s) links, for rendering as anchors. */
export function linkSegments(line: string): { text: string; href: string | null }[] {
  const parts: { text: string; href: string | null }[] = [];
  let at = 0;
  for (const match of line.matchAll(URL_RE)) {
    const start = match.index ?? 0;
    if (start > at) parts.push({ text: line.slice(at, start), href: null });
    parts.push({ text: match[0], href: safeSignInUrl(match[0]) });
    at = start + match[0].length;
  }
  if (at < line.length) parts.push({ text: line.slice(at), href: null });
  return parts;
}

const LOOPBACK = /^https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):\d+(?:[/?#]|$)/i;

/** An address the operator's browser lands on when a sign-in finishes on a loopback callback. */
export function isLoopbackAddress(text: string): boolean {
  return LOOPBACK.test(text.trim());
}

/** The vendor printed a loopback callback (#2106): its sign-in finishes on THIS host's 127.0.0.1,
 *  either named outright ("listening on http://localhost:1455") or as a link's `redirect_uri`. */
export function expectsLoopbackCallback(lines: readonly string[]): boolean {
  for (const line of lines) {
    for (const match of line.matchAll(URL_RE)) {
      // A URL at the end of a sentence carries its full stop ("…on http://localhost:1455.").
      if (isLoopbackAddress(match[0].replace(/[.,;:!?)\]]+$/, ""))) return true;
      let url: URL;
      try { url = new URL(match[0]); } catch { continue; }
      for (const value of url.searchParams.values()) {
        if (isLoopbackAddress(value)) return true;
      }
    }
  }
  return false;
}
