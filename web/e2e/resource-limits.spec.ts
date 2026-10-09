import { expect, test, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { settingsPath } from "../src/routes/settingsTabs";
import type { Resources, ResourceValues } from "../src/types/resources";

test("Resources has its own menu and editable memory budget without running usage", async ({
  page,
}) => {
  await setup(page);
  await page.goto(settingsPath());
  await page
    .getByRole("link", { name: "Resources", exact: true })
    .click({ timeout: 5000 });
  const memory = page.getByLabel("API session memory limit (GiB)");
  await expect(memory).toHaveValue("8");
  await expect(
    page.getByText("Running resource usage", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Console session task limit")).toBeHidden();
  await memory.fill("32");
  await page.getByRole("button", { name: "Save limits" }).click();
  await expect(page.getByRole("status")).toContainText(
    "Saved for new launches",
  );
  await page.reload();
  await expect(memory).toHaveValue("32");
});

const fixture = (): Resources => ({
  settings: {
    values: {
      console_tasks: 4096,
      api_tasks: 4096,
      library_threads: 8,
      api_memory_gib: 8,
    },
    recommended: {
      console_tasks: 4096,
      api_tasks: 4096,
      library_threads: 8,
      api_memory_gib: 8,
    },
    bounds: {
      console_tasks: { min: 256, max: 16384 },
      api_tasks: { min: 256, max: 16384 },
      library_threads: { min: 1, max: 64 },
      api_memory_gib: { min: 1, max: 1024 },
    },
    sources: {
      console_tasks: "recommended",
      api_tasks: "recommended",
      library_threads: "recommended",
      api_memory_gib: "recommended",
    },
    console_tasks_max: "4096",
    library_overrides: [],
    notice: null,
  },
  usage: {
    observed_at: 1791479400,
    console_containment: "verified",
    truncated: false,
    error: null,
    groups: [
      {
        unit: "as-fixture-shared.scope",
        kind: "console",
        own: {
          group: "as-fixture-shared.scope",
          current: 100,
          maximum: 4096,
          unlimited: false,
          denied: 7,
        },
        ancestors: [
          {
            group: "shared-parent.slice",
            current: 1980,
            maximum: 2048,
            unlimited: false,
            denied: 18,
          },
        ],
        severity: "critical",
        pressure: 1980 / 2048,
        headroom: 68,
        incomplete: false,
      },
    ],
  },
});
async function setup(page: Page, data = fixture(), failSave = false) {
  await commonMocks(page);
  await page.route("**/api/system", (route) =>
    route.fulfill({ json: { os: "Linux", cpus: 112, python: "3.11" } }),
  );
  const writes: Partial<ResourceValues>[] = [];
  await page.route("**/api/system/resources*", (route) => {
    if (route.request().method() === "POST") {
      if (failSave)
        return route.fulfill({
          status: 503,
          json: { detail: "could not save resource limits" },
        });
      const patch = route.request().postDataJSON() as Partial<ResourceValues>;
      writes.push(patch);
      data.settings.values = { ...data.settings.values, ...patch };
      for (const key of Object.keys(patch) as (keyof ResourceValues)[])
        data.settings.sources[key] = "settings";
      if (patch.console_tasks !== undefined)
        data.settings.console_tasks_max = String(patch.console_tasks);
      return route.fulfill({ json: { settings: data.settings } });
    }
    return route.fulfill({ json: data });
  });
  await page.goto(settingsPath("resources"));
  await page
    .getByText("Advanced process and thread limits", { exact: true })
    .click();
  await expect(page.getByLabel("Console session task limit")).toHaveValue(
    String(data.settings.values.console_tasks),
  );
  return writes;
}

test("save, reload and restore recommended values", async ({ page }) => {
  const writes = await setup(page);
  await page.getByLabel("Console session task limit").fill("3072");
  await page.getByLabel("API worker task limit").fill("6144");
  await page.getByLabel("Background library threads").fill("4");
  await page.getByRole("button", { name: "Save limits" }).click();
  await expect(page.getByRole("status")).toContainText(
    "Saved for new launches",
  );
  expect(writes).toEqual([
    { console_tasks: 3072, api_tasks: 6144, library_threads: 4 },
  ]);
  await page.reload();
  await page
    .getByText("Advanced process and thread limits", { exact: true })
    .click();
  await expect(page.getByLabel("Console session task limit")).toHaveValue(
    "3072",
  );
  await page.getByRole("button", { name: "Restore recommended" }).click();
  await expect(page.getByLabel("Console session task limit")).toHaveValue(
    "4096",
  );
  expect(writes).toHaveLength(1); // selecting recommendations is still a draft
  await page.getByRole("button", { name: "Save limits" }).click();
  await expect(page.getByRole("status")).toContainText(
    "Saved for new launches",
  );
  expect(writes[1]).toEqual({
    console_tasks: 4096,
    api_tasks: 4096,
    library_threads: 8,
    api_memory_gib: 8,
  });
});

test("advanced environment overrides remain explicit", async ({ page }) => {
  const data = fixture();
  data.settings.sources.console_tasks = "environment";
  data.settings.console_tasks_max = "50%";
  data.settings.library_overrides = [
    { name: "OPENBLAS_NUM_THREADS", value: "1" },
  ];
  data.usage.console_containment = "disabled";
  data.usage.error = "Running resource readings are unavailable on this host.";
  data.usage.groups = [];
  const writes = await setup(page, data);
  await expect(page.getByText(/currently uses TasksMax=50%/)).toBeVisible();
  await page.getByText("Library environment overrides (1)").click();
  await expect(page.getByText("OPENBLAS_NUM_THREADS")).toBeVisible();
  await page.getByRole("button", { name: "Save limits" }).click();
  await expect(page.getByRole("status")).toContainText(
    "No limits were changed",
  );
  expect(writes).toEqual([]);
  await page.getByRole("button", { name: "Use console value above" }).click();
  expect(writes).toEqual([]); // replacement is a draft until Save
  await page.getByRole("button", { name: "Keep host override" }).click();
  await page.getByRole("button", { name: "Save limits" }).click();
  await expect(page.getByRole("status")).toContainText(
    "No limits were changed",
  );
  expect(writes).toEqual([]);
  await page.getByRole("button", { name: "Use console value above" }).click();
  await page.getByRole("button", { name: "Save limits" }).click();
  await expect(page.getByText(/currently uses TasksMax=50%/)).toHaveCount(0);
  expect(writes).toEqual([{ console_tasks: 4096 }]);
});

for (const legacy of ["50%", "infinity", "2048"])
  test(`API and library edits preserve untouched ${legacy} console override`, async ({
    page,
  }) => {
    const data = fixture();
    data.settings.sources.console_tasks = "environment";
    data.settings.console_tasks_max = legacy;
    if (legacy === "2048") data.settings.values.console_tasks = 2048;
    const writes = await setup(page, data);
    await page.getByLabel("API worker task limit").fill("1024");
    await page.getByRole("button", { name: "Save limits" }).click();
    await expect(page.getByRole("status")).toContainText(
      "Saved for new launches",
    );
    expect(writes).toEqual([{ api_tasks: 1024 }]);
    await expect(
      page.getByText(`TasksMax=${legacy}`, { exact: false }),
    ).toBeVisible();
    await page.getByLabel("Background library threads").fill("4");
    await page.getByRole("button", { name: "Save limits" }).click();
    await expect(page.getByRole("status")).toContainText(
      "Saved for new launches",
    );
    expect(writes).toEqual([{ api_tasks: 1024 }, { library_threads: 4 }]);
    await page.reload();
    await page
      .getByText("Advanced process and thread limits", { exact: true })
      .click();
    await expect(
      page.getByText(`TasksMax=${legacy}`, { exact: false }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Restore recommended" }).click();
    expect(writes).toHaveLength(2);
    await page.getByRole("button", { name: "Save limits" }).click();
    await expect(
      page.getByText(`TasksMax=${legacy}`, { exact: false }),
    ).toHaveCount(0);
    expect(writes[2]).toEqual({
      console_tasks: 4096,
      api_tasks: 4096,
      library_threads: 8,
      api_memory_gib: 8,
    });
  });

test("invalid values cannot save and a failed save retains the draft", async ({
  page,
}) => {
  const writes = await setup(page, fixture(), true);
  await page.getByLabel("API session memory limit (GiB)").fill("0");
  await expect(
    page.getByRole("button", { name: "Save limits" }),
  ).toBeDisabled();
  await page.getByLabel("API session memory limit (GiB)").fill("1025");
  await expect(
    page.getByRole("button", { name: "Save limits" }),
  ).toBeDisabled();
  await page.getByLabel("API session memory limit (GiB)").fill("32");
  await page.getByRole("button", { name: "Save limits" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "could not save resource limits",
  );
  await expect(page.getByLabel("API session memory limit (GiB)")).toHaveValue(
    "32",
  );
  expect(writes).toEqual([]);
});

for (const theme of ["dark", "light"])
  test(`${theme}: 320px layout, controls and text remain usable`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 320, height: 850 });
    await setup(page);
    await page.evaluate(
      (t) => document.documentElement.setAttribute("data-theme", t),
      theme,
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    for (const label of [
      "API session memory limit (GiB)",
      "Console session task limit",
      "API worker task limit",
      "Background library threads",
    ]) {
      const box = await page.getByLabel(label).boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(44);
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(320);
    }
  });

test("Host no longer includes resource controls or running usage", async ({
  page,
}) => {
  await setup(page);
  await page.goto(settingsPath("system"));
  await expect(page.getByLabel("API session memory limit (GiB)")).toHaveCount(
    0,
  );
  await expect(page.getByLabel("Console session task limit")).toHaveCount(0);
  await expect(
    page.getByText("Running resource usage", { exact: true }),
  ).toHaveCount(0);
});
