import type { BrowserContext } from "@playwright/test";

/** A context a spec makes itself (`browser.newContext()`) does not get the config's storage, so it
 *  would meet the #1232 "Full screen or map?" prompt on its first session URL. This makes the same
 *  choice the suite's default storage does. */
export async function openLinksFullScreen(ctx: BrowserContext): Promise<void> {
  await ctx.addInitScript(() => {
    try {
      localStorage.setItem("battlelab.linkOpenMode", "fullscreen");
    } catch {
      // about:blank and other opaque origins have no storage.
    }
  });
}
