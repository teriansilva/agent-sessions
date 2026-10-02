/** Share a BattleLab link (#1232). The installed app has no address bar, so this is the only way
 *  to pass a session or mission on from it.
 *
 *  The system share sheet where the platform has one; otherwise the clipboard. The URL is built
 *  from the page's own origin, so it names whichever host the operator is on. */
export type ShareOutcome = "shared" | "copied" | "dismissed" | "failed";

export function absoluteLink(path: string, origin = window.location.origin): string {
  return new URL(path, origin).toString();
}

export async function shareLink(opts: { title: string; path: string }): Promise<ShareOutcome> {
  const url = absoluteLink(opts.path);
  const data = { title: opts.title, url };
  if (typeof navigator.share === "function" && (navigator.canShare?.(data) ?? true)) {
    try {
      await navigator.share(data);
      return "shared";
    } catch (err) {
      // The operator closed the sheet: that is an answer, not a failure — and falling through to
      // the clipboard would do something they just declined.
      if (err instanceof DOMException && err.name === "AbortError") return "dismissed";
    }
  }
  try {
    await navigator.clipboard.writeText(url);
    return "copied";
  } catch {
    return "failed";
  }
}
