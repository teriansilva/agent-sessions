// node render.cjs <page.html> <id=out.png> ...  — screenshots each #id element of the page.
const path = require('path');
const { chromium } = require(path.join(__dirname, '../../../web/node_modules/playwright'));
(async () => {
  const [page_, ...pairs] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 }, deviceScaleFactor: 1 });
  await page.goto('file://' + path.resolve(__dirname, page_));
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(300);
  for (const p of pairs) {
    const [id, out] = p.split('=');
    await page.locator('#' + id).screenshot({ path: path.resolve(__dirname, 'out', out) });
    console.log('wrote', out);
  }
  const fonts = await page.evaluate(() => [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family + ' ' + f.weight));
  console.log('fonts loaded:', fonts.join(', '));
  await browser.close();
})();
