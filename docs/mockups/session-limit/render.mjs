// Run from this directory with a web/ install available; produces the review mockups.
import { chromium } from '../../../web/node_modules/@playwright/test/index.mjs';
import { fileURLToPath } from 'node:url';
const browser = await chromium.launch({ headless: true });
for (const theme of ['dark', 'light']) {
  for (const mobile of [false, true]) {
    const page = await browser.newPage({ viewport: { width: mobile ? 423 : 1200, height: 900 } });
    await page.goto(new URL(`index.html?theme=${theme}${mobile ? '&mobile=1' : ''}`, import.meta.url).href);
    await page.screenshot({ path: fileURLToPath(new URL(`${mobile ? 'mobile' : 'desktop'}-${theme}.png`, import.meta.url)), fullPage: true });
    await page.close();
  }
}
await browser.close();
