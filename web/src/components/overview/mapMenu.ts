/** Pure rules behind the Overview map's session menu (#968). Kept out of the canvas so the two
 *  that decide correctness — which windows an archive must close, and where focus lands when the
 *  element that opened the menu is gone — are testable without React Flow. */

/** The open windows showing a session, matched on `actionKey`: the id the server acts on.
 *
 *  NOT the transport `key`. A window launched under a `new-<uuid>` placeholder keeps that key for
 *  life (its socket was opened under it, #127), while the chip — and therefore the menu — names
 *  the engine's real id once it has converged. Matching on `key` would miss exactly that window
 *  and leave its socket alive through the archive that reaps its runtime (#523/#631). */
export function windowsForSession<W extends { key: string; actionKey: string }>(
  windows: readonly W[],
  sessionKey: string,
): W[] {
  return windows.filter((w) => w.actionKey === sessionKey);
}

/** Move focus back after the menu or one of its dialogs closes: to the element that opened it
 *  while it is still in the document, else to the map. An archive removes the chip — and with it
 *  the ⋯ that opened the menu — and focus must not fall through to `<body>`. */
export function restoreFocus(
  opener: HTMLElement | null | undefined,
  fallback: HTMLElement | null | undefined,
): void {
  const target = opener?.isConnected ? opener : fallback;
  target?.focus({ preventScroll: true });
}
