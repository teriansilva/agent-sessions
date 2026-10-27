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
