import { createRequire } from "node:module";
const require = createRequire(new URL("../../../web/package.json", import.meta.url).pathname);
const { chromium } = require("@playwright/test");
const here = new URL(".", import.meta.url).pathname;
const b = await chromium.launch();
for (const [board, name] of [["main", "main.png"], ["states", "states.png"]]) {
  const pg = await b.newPage({ viewport: { width: 1900, height: 900 }, deviceScaleFactor: 1 });
  await pg.goto(`file://${here}hints.html?board=${board}`);
  await pg.evaluate(() => document.fonts.ready);
  await pg.waitForTimeout(400);
  const box = await pg.locator("#board").boundingBox();
  await pg.screenshot({ path: here + name, fullPage: true, clip: { x: 0, y: 0, width: box.x + box.width + 28, height: box.y + box.height + 28 } });
}
await b.close();
