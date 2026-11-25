import { chromium } from '../../../web/node_modules/@playwright/test/index.mjs';
import { fileURLToPath } from 'node:url';
const browser = await chromium.launch();
for (const theme of ['dark', 'light']) for (const mobile of [false, true]) {
  const p = await browser.newPage({ viewport: { width: mobile ? 420 : 1360, height: 900 }, hasTouch: mobile, isMobile: mobile });
  await p.goto(new URL(`repos.html?theme=${theme}${mobile ? '&mobile=1' : ''}`, import.meta.url).href);
  await p.waitForTimeout(300);
  const small = await p.$$eval('button, .seg, .tab, .select', (els) =>
    els.filter((e) => e.getBoundingClientRect().height < 44).map((e) => e.textContent.trim()));
  if (small.length) throw new Error(`${theme} ${mobile}: under 44px: ${small.join(' | ')}`);
  const wide = await p.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  if (wide) throw new Error(`${theme} ${mobile}: sideways scroll`);
  await p.screenshot({ path: fileURLToPath(new URL(`repos-${mobile ? 'mobile' : 'desktop'}-${theme}.png`, import.meta.url)), fullPage: true });
  await p.close();
}
await browser.close();
