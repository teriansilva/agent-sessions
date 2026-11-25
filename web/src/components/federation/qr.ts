/** The pairing invite as a QR code (#2052), computed in the browser by the bundled
 *  `@paulmillr/qr` (already a dependency: Settings → Security and onboarding draw the TOTP code
 *  with it). Nothing is fetched, and the invite never leaves this module as anything but geometry.
 *
 *  This is the ONE federation module that imports the encoder. It returns the module matrix as a
 *  single SVG path, so the page draws it as React elements: no markup string, no data URI. */
import encodeQR from "@paulmillr/qr";

/** The quiet zone, in modules, on every side. The QR standard asks for four. */
export const QR_QUIET = 4;

export interface QrShape {
  /** The side of the square in modules, quiet zone included: the SVG's `viewBox` is `0 0 size size`. */
  size: number;
  /** Every dark module as one path of unit squares (run-length merged along a row). */
  path: string;
}

/** `null` when `text` is too long for a QR code (the encoder refuses); the invite is still shown
 *  as text, which is the form the other operator pastes anyway. */
export function qrShape(text: string): QrShape | null {
  let rows: boolean[][];
  try {
    rows = encodeQR(text, "raw", { border: 0 });
  } catch {
    return null;
  }
  let path = "";
  rows.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      if (!row[x]) {
        x++;
        continue;
      }
      let run = 1;
      while (row[x + run]) run++;
      path += `M${x + QR_QUIET} ${y + QR_QUIET}h${run}v1h-${run}z`;
      x += run;
    }
  });
  return { size: rows.length + 2 * QR_QUIET, path };
}
