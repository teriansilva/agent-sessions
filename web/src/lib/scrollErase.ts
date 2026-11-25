/** Live-stream scrollback-erase sanitization (#600 codex, #1038 kimi).
 *
 *  Two engines' TUIs repaint their whole frame with a full-screen clear that includes
 *  `ESC[3J` (CSI 3J — erase scrollback): Codex's ratatui and Kimi's ink-style TUI. In a
 *  standalone terminal that is reasonable; in BattleLab it deletes the semantic
 *  transcript/scrollback we intentionally keep above the live frame (the inline-history
 *  contract), leaving touch scroll with nothing to move — and on Kimi the mid-stream buffer
 *  collapse also drags xterm's viewport off the live tail, where the client's sticky
 *  reader-anchor pins it (the "kimi randomly scrolls up on updates" bug).
 *
 *  The SERVER applies the identical filter to the live dtach stream (`scrollback.sanitize_live_output`)
 *  before ring/mirror/ws; this client-side copy protects the post-`seq` stream (and the #348
 *  rewrite replay) even on sessions whose ring predates the server fix. Server-authored attach
 *  clears (`ESC[H ESC[2J ESC[3J`) intentionally wipe pre-attach junk, so the caller gates on the
 *  replay boundary: only LIVE bytes (after the `{"t":"seq"}` frame) are filtered.
 *
 *  The strip is byte-exact and carries a PARTIAL `ESC[3J` across chunk boundaries — a read
 *  boundary can split the sequence — and a partial that never completes passes through verbatim
 *  (only complete wipe sequences are ever dropped). */

// Which engines do it is the manifest's `terminal.repaint = "wipe"`, served on the roster (#853
// P4) — this used to be a second copy of the server's set, the #454 drift shape.
import { wipesOnRepaint } from "../app/engineRoster";

const SCROLLBACK_ERASE = new Uint8Array([0x1b, 0x5b, 0x33, 0x4a]); // CSI 3J

export interface ScrollEraseStripper {
  /** Filter one live chunk for `engine`. Non-member engines and the attach replay
   *  (`live: false`) pass through byte-for-byte and drop any partial carry — the same
   *  reset the original inline closure performed at each gate flip. */
  strip: (engine: string, live: boolean, bytes: Uint8Array) => Uint8Array;
  /** Drop a partial carry at a stream boundary (attach replay end / reconnect). */
  reset: () => void;
}

export function createScrollEraseStripper(): ScrollEraseStripper {
  let carry = new Uint8Array(0);
  const reset = () => {
    carry = new Uint8Array(0);
  };
  const strip = (engine: string, live: boolean, bytes: Uint8Array): Uint8Array => {
    // Before the roster loads no engine "wipes on repaint", so nothing is stripped here — the
    // server's `sanitize_live_output` already strips these bytes; this is the belt to its braces.
    if (!live || !wipesOnRepaint(engine)) {
      reset();
      return bytes;
    }
    let src = bytes;
    const hadCarry = carry.length > 0;
    if (carry.length) {
      src = new Uint8Array(carry.length + bytes.length);
      src.set(carry, 0);
      src.set(bytes, carry.length);
      reset();
    }
    const out = new Uint8Array(src.length);
    let outLen = 0;
    let i = 0;
    while (i < src.length) {
      const remaining = src.length - i;
      const full =
        remaining >= SCROLLBACK_ERASE.length &&
        SCROLLBACK_ERASE.every((v, j) => src[i + j] === v);
      if (full) {
        i += SCROLLBACK_ERASE.length;
        continue;
      }
      const partial =
        remaining < SCROLLBACK_ERASE.length &&
        SCROLLBACK_ERASE.slice(0, remaining).every((v, j) => src[i + j] === v);
      if (partial) {
        carry = src.slice(i);
        break;
      }
      out[outLen++] = src[i];
      i++;
    }
    const unchanged = !hadCarry && outLen === src.length && carry.length === 0;
    return unchanged ? bytes : out.slice(0, outLen);
  };
  return { strip, reset };
}
