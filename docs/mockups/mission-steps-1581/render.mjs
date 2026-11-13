import { chromium } from '../../../web/node_modules/@playwright/test/index.mjs';
import { fileURLToPath } from 'node:url';
const browser = await chromium.launch();
for (const page of ['board', 'states']) for (const theme of ['dark', 'light']) for (const mobile of [false, true]) {
  const p = await browser.newPage({ viewport: { width: mobile ? 420 : 1360, height: 900 }, hasTouch: mobile, isMobile: mobile });
  await p.goto(new URL(`${page}.html?theme=${theme}${mobile ? '&mobile=1' : ''}`, import.meta.url).href);
  await p.waitForTimeout(300);
  // Every control at least 44 px (Hermes on #1951): fail the render instead of shipping a frame.
  const small = await p.$$eval('button, a.btn, .hold, .chopt, summary', (els) =>
    els.filter((e) => e.getBoundingClientRect().height < 44).map((e) => e.textContent.trim()));
  if (small.length) throw new Error(`${page} ${theme} ${mobile}: under 44px: ${small.join(' | ')}`);
  await p.screenshot({ path: fileURLToPath(new URL(`${page}-${mobile ? 'mobile' : 'desktop'}-${theme}.png`, import.meta.url)), fullPage: true });
  await p.close();
}
await browser.close();
