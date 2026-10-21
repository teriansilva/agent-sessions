import type { Page } from "@playwright/test";

/** Slow only this page for a bounded sensitivity probe, never the shared runner. */
export async function throttleIfAsked(page: Page) {
  const rate = Number(process.env.E2E_CPU_THROTTLE ?? "0");
  if (!rate) return;
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate });
}
