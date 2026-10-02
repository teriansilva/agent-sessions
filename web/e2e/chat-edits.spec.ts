import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";

const SID = "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b";
const TID = "11111111-2222-4333-8444-555555555555";
const PID = "22222222-2222-4333-8444-555555555555";
const KEY = `apichat:${SID}`;
const LONG = `src/${"deeply/nested/".repeat(8)}connection.ts`;

async function setup(page: Page, theme: string, stale = false, loseResponse = false) {
  const decisions: unknown[] = [];
  let decided: "approved" | "rejected" | null = null;
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: {} }));
  await page.route("**/api/config", (r) => r.fulfill({ json: {
    csrf: "x", new_session_engines: ["apichat"], terminal_backend: "ws",
    auth_mode: "none", overview_expanded: [], projects_hidden: [], theme,
  } }));
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: {
    sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] },
  } }));
  await mockRoster(page);
  await page.route("**/api/agents/apichat/endpoint", (r) => r.fulfill({ json: {
    base_url: "https://llm.example.test/v1", model: "test-model", api_key_set: true,
    context_window: 32768, max_output_tokens: 4096, configured: true, tools: "write",
  } }));
  await page.route(`**/api/chat/${encodeURIComponent(KEY)}`, (r) => r.fulfill({ json: {
    session_id: SID, cwd: "/home/op/proj", created_at: 1,
    in_flight: decided ? null : TID,
    turns: [{ turn_id: TID, text: "Improve the connection help", ts: 1,
      status: decided ? "done" : "awaiting_approval", reason: null,
      reply: decided ? "Your decision was recorded." : null,
      reply_ts: null, usage: null, truncated: false, dropped: 0,
      proposals: [{ id: PID, turn_id: TID, path: LONG, base_sha256: "a".repeat(64),
        new_sha256: "b".repeat(64), size: 123, created_at: 1,
        status: decided ?? "awaiting_approval", can_approve: !stale,
        reason: stale ? "the file changed after the agent read it" : null,
        diff: decided ? undefined : "--- connection.ts\n+++ connection.ts\n@@ -1 +1 @@\n-Check settings.\n+Check the server address and try again.\n",
      }],
    }],
  } }));
  await page.route(`**/api/chat/${encodeURIComponent(KEY)}/turns/${TID}/proposals/${PID}/decide`, async (r) => {
    const decision = r.request().postDataJSON();
    decisions.push(decision);
    decided = decision.decision === "reject" ? "rejected" : "approved";
    if (loseResponse) await r.abort();
    else await r.fulfill({ json: { proposal: { id: PID, status: decided } } });
  });
  await page.goto(`/s/apichat/${SID}`);
  return decisions;
}

for (const theme of ["dark", "light"]) {
  test(`review, reload, keyboard approval and retained decision (${theme})`, async ({ page }) => {
    const decisions = await setup(page, theme);
    const proposal = page.getByTestId("chat-proposal");
    await expect(proposal).toContainText(LONG);
    await expect(proposal).toContainText("Check the server address");
    await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
    await page.reload();
    await expect(proposal).toContainText("Awaiting your approval");
    const approve = page.getByRole("button", { name: "Approve & save" });
    const reject = page.getByRole("button", { name: "Reject", exact: true });
    for (const button of [approve, reject]) {
      expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    await reject.focus();
    await page.keyboard.press("Tab");
    await expect(approve).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(proposal).toContainText("Approved");
    expect(decisions).toEqual([{ decision: "approve" }]);
    await expect(page.getByRole("button", { name: "Approve & save" })).toHaveCount(0);
  });

  test(`stale proposal cannot be approved; rejection remains reachable (${theme})`, async ({ page }) => {
    const decisions = await setup(page, theme, true);
    await expect(page.getByTestId("chat-proposal")).toContainText("the file changed");
    await expect(page.getByRole("button", { name: "Approve & save" })).toBeDisabled();
    await page.getByRole("button", { name: "Reject", exact: true }).click();
    await expect(page.getByTestId("chat-proposal")).toContainText("Rejected");
    expect(decisions).toEqual([{ decision: "reject" }]);
    await page.reload();
    await expect(page.getByTestId("chat-proposal")).toContainText("Rejected");
    await expect(page.getByTestId("chat-proposal")).not.toContainText("Approved");
  });
}


test("a lost decision response uses the authoritative outcome without a false error", async ({ page }) => {
  const decisions = await setup(page, "dark", false, true);
  await page.getByRole("button", { name: "Approve & save" }).click();
  const proposal = page.getByTestId("chat-proposal");
  await expect(proposal).toContainText("Approved · saved");
  await expect(proposal.getByRole("alert")).toHaveCount(0);
  expect(decisions).toEqual([{ decision: "approve" }]);
});
