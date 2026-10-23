/** HQ's operation cards (#1559), in a real browser, desktop and mobile.
 *
 *  What a DOM emulator cannot prove: that Confirm works by pointer AND keyboard and the card
 *  reads its outcome back; that a destructive card's confirm really paints the danger fill; that
 *  the folder card's picker (a portalled dialog) is keyboard-operable and its pick reaches the
 *  decide request; that the focus ring is visible on the chamfered controls; that every control
 *  meets the 44px floor; that nothing scrolls sideways on a phone; and that the outcome words
 *  hold AA contrast on the card in both themes.
 */
import { expect, test, type Locator } from "@playwright/test";

import {
  decideRoute,
  minTarget,
  noSidewaysScroll,
  open,
  PID,
  shell,
} from "./askOperationShell";

function card(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action_id: PID,
    kind: "operation",
    op: "session.archive",
    risk: "reversible",
    label: "Archive",
    verb: "Archive",
    title: "docs-site: rewrite the install guide",
    text: "Stops its agent and moves it to the archive. You can unarchive it later.",
    needs: [],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    ...over,
  };
}

/** A CSS colour (a `var(...)` included) as the browser resolves it: `rgb(r, g, b)`. */
async function resolvedColor(l: Locator, css: string): Promise<string> {
  return l.evaluate((_el, value) => {
    const p = document.createElement("span");
    p.style.color = value;
    document.body.append(p);
    const c = getComputedStyle(p).color;
    p.remove();
    return c;
  }, css);
}

/** WCAG contrast of an element's text against the face of the card it sits on. NIGHTJAR cards
 *  paint their face from `--nj-face` (a plate), not `background-color`, so that variable is the
 *  ground the text actually sits on. */
async function contrast(l: Locator): Promise<number> {
  const face = await l.evaluate((el) =>
    getComputedStyle(el.closest('[data-testid="ask-operation"]') as HTMLElement)
      .getPropertyValue("--nj-face")
      .trim(),
  );
  const fg = await l.evaluate((el) => getComputedStyle(el).color);
  const bg = await resolvedColor(l, face);
  return l.evaluate(
    (_el, [fgs, bgs]) => {
      // `rgb(r, g, b)` in 0–255, or `color(srgb r g b)` (what `color-mix` resolves to) in 0–1.
      const rgb = (s: string) => {
        const n = (s.match(/[\d.]+/g) || []).map(Number).slice(0, 3);
        return s.startsWith("color(") ? n.map((v) => v * 255) : n;
      };
      const lum = ([r, g, b]: number[]) => {
        const f = (v: number) => {
          const c = v / 255;
          return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
        };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
      };
      const [a, b] = [lum(rgb(fgs)), lum(rgb(bgs))].sort((x, y) => y - x);
      return (a + 0.05) / (b + 0.05);
    },
    [fg, bg] as [string, string],
  );
}

test("Confirm by pointer runs it and the card reads Done; Dismiss runs nothing (#1559)", async ({
  page,
}) => {
  const fake = await shell(page, card());
  const bodies = await decideRoute(fake, page, {
    state: "done",
    summary: "Archived.",
  });
  const c = await open(page);
  await expect(c).toContainText("Reversible");
  await minTarget(c.getByTestId("ask-operation-confirm"));
  await minTarget(c.getByTestId("ask-operation-dismiss"));
  await c.getByTestId("ask-operation-confirm").click();
  await expect(c.getByTestId("ask-operation-outcome")).toContainText("Done", {
    timeout: 20_000,
  });
  await expect(c.getByTestId("ask-operation-outcome")).toContainText(
    "Archived.",
  );
  expect(bodies).toEqual([{ decision: "confirm" }]);
  await expect(c.getByTestId("ask-operation-confirm")).toHaveCount(0);
  await noSidewaysScroll(page);
});

test("Confirm by keyboard, with a visible focus ring (#1559)", async ({
  page,
}) => {
  const fake = await shell(page, card());
  const bodies = await decideRoute(fake, page, {
    state: "done",
    summary: "Archived.",
  });
  const c = await open(page);
  const confirm = c.getByTestId("ask-operation-confirm");
  // Arrive by keyboard (Shift+Tab, then Tab): :focus-visible is the keyboard's, not a script's.
  await confirm.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(confirm).toBeFocused();
  expect(await confirm.evaluate((el) => el.matches(":focus-visible"))).toBe(
    true,
  );
  expect(
    await confirm.evaluate((el) => getComputedStyle(el).outlineStyle),
  ).not.toBe("none");
  await page.keyboard.press("Enter");
  await expect(c.getByTestId("ask-operation-outcome")).toContainText("Done", {
    timeout: 20_000,
  });
  expect(bodies).toEqual([{ decision: "confirm" }]);
});

test("Dismiss runs nothing and says so (#1559)", async ({ page }) => {
  const fake = await shell(page, card());
  const bodies = await decideRoute(fake, page, {
    state: "done",
    summary: "Archived.",
  });
  const c = await open(page);
  await c.getByTestId("ask-operation-dismiss").click();
  await expect(c.getByTestId("ask-operation-outcome")).toContainText(
    "Dismissed",
    { timeout: 20_000 },
  );
  expect(bodies).toEqual([{ decision: "dismiss" }]);
});

test("a destructive card wears the danger confirm and says it can't be undone (#1559)", async ({
  page,
}) => {
  await shell(
    page,
    card({
      op: "project.delete",
      risk: "destructive",
      label: "Delete project",
      verb: "Delete project",
      title: "scratch",
    }),
  );
  const c = await open(page);
  await expect(c).toContainText("Can't be undone");
  await expect(c.getByTestId("ask-operation-danger")).toBeVisible();
  const confirm = c.getByRole("button", { name: "Delete project" });
  // NIGHTJAR controls paint their face from `--nj-face` (a plate), not `background-color`.
  const face = await confirm.evaluate((el) =>
    getComputedStyle(el).getPropertyValue("--nj-face").trim(),
  );
  expect(await resolvedColor(confirm, face)).toBe(
    await resolvedColor(confirm, "var(--status-down)"),
  );
  await minTarget(confirm);
});

for (const [state, word, extra] of [
  [
    "partly_done",
    "Partly done",
    { summary: "12 archived; 2 could not be. Open Sessions to see which." },
  ],
  [
    "failed",
    "Failed",
    {
      summary:
        "Not archived: a mission holds this session. Its agent may already have been stopped.",
    },
  ],
  [
    "stale",
    "Out of date",
    {
      reason:
        "it now has 4 sessions, not the 3 shown since this was proposed; nothing ran",
    },
  ],
] as const) {
  for (const theme of ["dark", "light"] as const) {
    test(`a ${state} card reads ${word} with the route's own words, at AA contrast in ${theme} (#1559)`, async ({
      page,
    }) => {
      await page.addInitScript(
        (t) => localStorage.setItem("tr-theme", t),
        theme,
      );
      await shell(
        page,
        card({ state, can_confirm: false, can_dismiss: false, ...extra }),
      );
      const c = await open(page);
      const outcome = c.getByTestId("ask-operation-outcome");
      await expect(outcome).toContainText(word);
      await expect(outcome).toContainText(Object.values(extra)[0].slice(0, 20));
      expect(
        await contrast(outcome.getByText(word, { exact: true })),
      ).toBeGreaterThanOrEqual(4.5);
      await expect(c.getByTestId("ask-operation-confirm")).toHaveCount(0);
      await noSidewaysScroll(page);
    });
  }
}

test("the folder card: the operator picks the folder by keyboard, and the pick reaches the decide (#1559)", async ({
  page,
}) => {
  const fake = await shell(
    page,
    card({
      op: "project.create",
      label: "Create project",
      verb: "Create project",
      title: "",
      needs: ["folder"],
      text: "Creates the project “web” in the folder you choose below.",
    }),
  );
  const bodies = await decideRoute(fake, page, {
    state: "done",
    summary: "Project created.",
  });
  const c = await open(page);
  const confirm = c.getByTestId("ask-operation-confirm");
  await expect(confirm).toBeDisabled();
  await expect(c.getByTestId("ask-operation-mkdir-note")).toContainText(
    "creates the folder now",
  );
  const pick = c.getByTestId("ask-operation-pick");
  await minTarget(pick);
  await pick.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: /choose the folder/i });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "web" }).focus();
  await page.keyboard.press("Enter");
  await dialog.getByRole("button", { name: /^Select ~\/web$/ }).focus();
  await page.keyboard.press("Enter");
  await expect(dialog).toHaveCount(0);
  await expect(c.getByTestId("ask-operation-folder")).toHaveText("/home/u/web");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(c.getByTestId("ask-operation-outcome")).toContainText("Done", {
    timeout: 20_000,
  });
  expect(bodies).toEqual([
    { decision: "confirm", inputs: { folder: "/home/u/web" } },
  ]);
  await noSidewaysScroll(page);
});

test("a picked project folder the refreshed card no longer offers is dropped, never confirmed (#1571 review)", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const fake = await shell(
    page,
    card({
      op: "project.default_folder",
      label: "Default launch folder",
      verb: "Set default folder",
      title: "web",
      needs: ["folder"],
      text: "New sessions in web start in the folder you choose below.",
      choices: { folder: ["/srv/a", "/srv/x"] },
    }),
  );
  const bodies = await decideRoute(fake, page, {
    state: "done",
    summary: "Default folder set.",
  });
  const c = await open(page);
  const confirm = c.getByTestId("ask-operation-confirm");
  await c.getByRole("radio", { name: "/srv/a" }).click();
  await expect(c.getByTestId("ask-operation-folder")).toHaveText("/srv/a");
  await expect(confirm).toBeEnabled();
  // Meanwhile folder A was replaced by B: the server's next read offers B, not A.
  fake.conversations[0].turns[0].proposals![0].choices = {
    folder: ["/srv/b", "/srv/x"],
  };
  await expect(c.getByRole("radio", { name: "/srv/b" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(c.getByTestId("ask-operation-folder")).toHaveText(
    "Choose below",
  );
  await expect(confirm).toBeDisabled();
  for (const r of await c.getByRole("radio").all()) {
    await expect(r).toHaveAttribute("aria-checked", "false");
  }
  expect(bodies).toEqual([]);
});

test("cancelling the folder picker by its backdrop closes only the picker, never HQ (#1571 review)", async ({
  page,
}) => {
  const fake = await shell(
    page,
    card({
      op: "project.create",
      label: "Create project",
      verb: "Create project",
      title: "",
      needs: ["folder"],
      text: "Creates the project “web” in the folder you choose below.",
    }),
  );
  const bodies = await decideRoute(fake, page, { state: "done", summary: "Project created." });
  const c = await open(page);
  await c.getByTestId("ask-operation-pick").click();
  const dialog = page.getByRole("dialog", { name: /choose the folder/i });
  await expect(dialog).toBeVisible();
  // A press on the backdrop, outside the picker's sheet (the top-left corner on both layouts).
  await page.mouse.click(4, 4);
  await expect(dialog).toHaveCount(0);
  // Not visibility: a closing drawer is still visible while it slides out. Its open state.
  await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "true");
  await page.waitForTimeout(400); // a close lands on the press; give it time to show
  await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "true");
  await expect(c.getByTestId("ask-operation-pick")).toBeInViewport();
  await expect(c).toHaveAttribute("data-state", "pending");
  expect(bodies).toEqual([]);
});
