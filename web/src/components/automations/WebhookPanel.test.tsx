/** #2202 — "Connect Forgejo": the secret is shown ONCE (only from the answer that made it, gone on
 *  Done, never read again), rotation says the old one keeps working, and the delivery facts are
 *  the server's. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { AutomationWebhook } from "../../types/automations";
import { FORGE_EVENTS } from "../../lib/automations";
import { WebhookPanel } from "./WebhookPanel";

const NOW = 1_800_000_000;
const HOOK: AutomationWebhook = {
  path: "/hooks/a/rv1",
  secret_set: false,
  secret_created_at: null,
  previous_valid_until: null,
  last_delivery_at: null,
  last_event: "",
  last_note: "",
  unsigned_24h: 0,
};
const SECRET = "7f3c".padEnd(64, "a");

afterEach(() => vi.restoreAllMocks());

test("the URL is the app's origin + the server's path; no delivery yet says so", () => {
  render(
    <WebhookPanel
      automationId="rv1"
      webhook={HOOK}
      nowS={NOW}
      onWebhook={() => {}}
    />,
  );
  expect(screen.getByTestId("webhook-url").textContent).toBe(
    `${window.location.origin}/hooks/a/rv1`,
  );
  expect(screen.getByTestId("webhook-status").textContent).toBe(
    "no secret yet",
  );
  expect(screen.getByTestId("webhook-last").textContent).toMatch(
    /No delivery yet/,
  );
  expect(screen.getByTestId("webhook-events").textContent).toContain(
    "Pull request reviewed",
  );
  // A push to an open PR is Forgejo's separate `pull_request_sync` subscription: without it the
  // fix → push → re-review loop never gets its next delivery.
  expect(screen.getByTestId("webhook-events").textContent).toContain(
    "Pull request synchronized",
  );
  expect(screen.queryByTestId("webhook-unsigned")).toBeNull();
});

test("Create secret shows it once; Done forgets it, and it is never fetched again", async () => {
  const made = { ...HOOK, secret_set: true, secret_created_at: NOW };
  const spy = vi
    .spyOn(api, "newWebhookSecret")
    .mockResolvedValue({ secret: SECRET, webhook: made });
  const onWebhook = vi.fn();
  render(
    <WebhookPanel
      automationId="rv1"
      webhook={HOOK}
      nowS={NOW}
      onWebhook={onWebhook}
    />,
  );
  await userEvent.click(screen.getByTestId("webhook-make-secret"));
  expect(spy).toHaveBeenCalledWith("rv1");
  expect(onWebhook).toHaveBeenCalledWith(made);
  expect(screen.getByTestId("webhook-secret-value").textContent).toBe(SECRET);
  expect(screen.getByTestId("webhook-secret").textContent).toMatch(
    /Shown once — copy it now/i,
  );
  await userEvent.click(screen.getByTestId("webhook-done"));
  expect(screen.queryByTestId("webhook-secret-value")).toBeNull();
  expect(document.body.textContent).not.toContain(SECRET);
  expect(spy).toHaveBeenCalledTimes(1);
});

test("Copy writes the secret to the clipboard", async () => {
  vi.spyOn(api, "newWebhookSecret").mockResolvedValue({
    secret: SECRET,
    webhook: { ...HOOK, secret_set: true },
  });
  const user = userEvent.setup();
  const write = vi.spyOn(navigator.clipboard, "writeText");
  render(
    <WebhookPanel
      automationId="rv1"
      webhook={HOOK}
      nowS={NOW}
      onWebhook={() => {}}
    />,
  );
  await user.click(screen.getByTestId("webhook-make-secret"));
  await user.click(screen.getByTestId("webhook-copy"));
  expect(write).toHaveBeenCalledWith(SECRET);
  expect(screen.getByTestId("webhook-copied")).toBeTruthy();
});

test("with a secret set: Rotate, the 24 h overlap, the last delivery and the unsigned count", () => {
  render(
    <WebhookPanel
      automationId="rv1"
      webhook={{
        ...HOOK,
        secret_set: true,
        secret_created_at: NOW - 3600,
        previous_valid_until: NOW + 7200,
        last_delivery_at: NOW - 60,
        last_event: "pull_request",
        last_note: "review",
        unsigned_24h: 3,
      }}
      nowS={NOW}
      onWebhook={() => {}}
    />,
  );
  expect(screen.getByTestId("webhook-make-secret").textContent).toBe(
    "Rotate secret",
  );
  expect(screen.getByTestId("webhook-status").textContent).toBe("connected");
  expect(screen.getByTestId("webhook-previous").textContent).toMatch(
    /previous secret keeps working until/,
  );
  expect(document.body.textContent).toMatch(
    /old secret keeps working for 24 h/,
  );
  expect(screen.getByTestId("webhook-last").textContent).toMatch(
    /pull_request · review/,
  );
  expect(screen.getByTestId("webhook-unsigned").textContent).toMatch(
    /3 unsigned deliveries refused/,
  );
  expect(screen.queryByTestId("webhook-secret-value")).toBeNull();
});

test("a refused secret says why and shows nothing", async () => {
  vi.spyOn(api, "newWebhookSecret").mockRejectedValue(
    new Error("only a webhook automation has a webhook secret"),
  );
  render(
    <WebhookPanel
      automationId="rv1"
      webhook={HOOK}
      nowS={NOW}
      onWebhook={() => {}}
    />,
  );
  await userEvent.click(screen.getByTestId("webhook-make-secret"));
  expect((await screen.findByTestId("webhook-error")).textContent).toMatch(
    /only a webhook automation/,
  );
  expect(screen.queryByTestId("webhook-secret-value")).toBeNull();
});

test("the events to tick are exactly the five the loop reads", () => {
  expect(FORGE_EVENTS).toEqual([
    "Issues",
    "Issue comment",
    "Pull request",
    "Pull request synchronized",
    "Pull request reviewed",
  ]);
});
