import { chromium } from '../../../web/node_modules/@playwright/test/index.mjs';
import { fileURLToPath } from 'node:url';
const browser = await chromium.launch();
for (const theme of ['dark', 'light']) {
  for (const mobile of [false, true]) {
    const page = await browser.newPage({ viewport: { width: mobile ? 844 : 1360, height: 1000 } });
    await page.goto(new URL(`index.html?theme=${theme}${mobile ? '&mobile=1' : ''}`, import.meta.url).href);
    await page.screenshot({ path: fileURLToPath(new URL(`${mobile ? 'mobile' : 'desktop'}-${theme}.png`, import.meta.url)), fullPage: true });
    await page.close();
  }
}
await browser.close();
