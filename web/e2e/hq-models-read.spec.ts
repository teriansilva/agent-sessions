import { expect, test } from "@playwright/test";
import { mockShell } from "./callFixture";

test("unreadable HQ settings keep inherited call sources unknown until reload", async ({ page }) => {
  const calls = await mockShell(page);
  calls.settings.thinking = "hq";
  let unavailable = true;
  await page.route("**/api/config", (r) => {
    if (unavailable) return r.fulfill({ status: 503, json: { detail: "settings unavailable" } });
    return r.fulfill({ json: {
      csrf: "csrf-x", auth_mode: "none", onboarded: true, hq_contact: "off",
      ai_review: { model: "endpoint", thinking: "medium" },
      hq_route: { enabled: true, model: "", thinking: "off" },
      hq_models: { typed: { model: "", thinking: "hq" }, mission: { model: "", thinking: "hq" } },
    } });
  });
  await page.goto("/settings/ai-hq#hq-models-h");
  const group = page.getByRole("region", { name: "HQ models", exact: true });
  await expect(group.getByRole("alert")).toContainText("could not be read");
  await expect(group.getByLabel("Model for typed turns")).toBeDisabled();
  await expect(group.getByLabel("Model for calls", { exact: true })).toBeEnabled();
  const call = group.getByRole("group", { name: "Calls", exact: true });
  await expect(call).toContainText("unavailable until settings reload");
  unavailable = false;
  await group.getByRole("button", { name: "Reload settings", exact: true }).click();
  await expect(group.getByLabel("Model for typed turns")).toBeEnabled();
  await expect(call).toContainText("From Endpoint & model · Medium");
  await expect(group.getByRole("alert")).toHaveCount(0);
});
