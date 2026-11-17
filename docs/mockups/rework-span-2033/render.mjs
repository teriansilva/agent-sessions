import { chromium } from '../../../web/node_modules/@playwright/test/index.mjs';
import { fileURLToPath } from 'node:url';
const browser = await chromium.launch();
for (const theme of ['dark', 'light']) for (const mobile of [false, true]) {
  const p = await browser.newPage({ viewport: { width: mobile ? 420 : 1360, height: 900 } });
  await p.goto(new URL(`span.html?theme=${theme}${mobile ? '&mobile=1' : ''}`, import.meta.url).href);
  await p.waitForTimeout(300);
  await p.screenshot({ path: fileURLToPath(new URL(`span-${mobile ? 'mobile' : 'desktop'}-${theme}.png`, import.meta.url)), fullPage: true });
  await p.close();
}
await browser.close();
