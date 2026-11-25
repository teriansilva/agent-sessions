/** #2221 — the secret shown once (copy, Done forgets it), the shared webhook's read-only panel (no
 *  secret controls, a link to its page) and the editor's picker (pickable only, + New webhook…). */
import { createRef } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import type { Webhook } from "../../types/webhooks";
import { SecretOnce, SharedWebhookPanel } from "./WebhookBits";
import { WebhookPicker } from "./WebhookPicker";

const NOW = 1_800_000_000;
const SECRET = "9a1f".padEnd(64, "c");

afterEach(() => vi.restoreAllMocks());

function hook(over: Partial<Webhook> = {}): Webhook {
  return {
    id: "0a1b2c3d4e5f6a7b",
    name: "shop repo",
    revision: 1,
    path: "/hooks/w/0a1b2c3d4e5f6a7b",
    legacy_automation: null,
    pickable: true,
    secret_set: true,
    secret_created_at: NOW,
    previous_valid_until: null,
    last_delivery_at: null,
    last_event: "",
    last_note: "",
    unsigned_24h: 0,
    used_by: [],
    created_at: NOW,
    ...over,
  };
}

test("SecretOnce: the secret, Copy secret and Copy address each copy their own value; Done", async () => {
  const user = userEvent.setup();
  const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
  const done = vi.fn();
  render(
    <SecretOnce
      secret={SECRET}
      address="https://example.test/hooks/w/x"
      addressEl={createRef<HTMLElement>()}
      nowS={NOW}
      onDone={done}
    />,
  );
  expect(screen.getByTestId("webhook-secret-value").textContent).toBe(SECRET);
  expect(screen.getByTestId("webhook-secret").textContent).toMatch(/shown once — copy it now/i);
  await user.click(screen.getByTestId("webhook-copy-secret"));
  expect(write).toHaveBeenLastCalledWith(SECRET);
  expect(screen.getByTestId("webhook-copied").textContent).toBe("Secret copied.");
  await user.click(screen.getByTestId("webhook-copy-address-once"));
  expect(write).toHaveBeenLastCalledWith("https://example.test/hooks/w/x");
  expect(screen.getByTestId("webhook-copied").textContent).toBe("Address copied.");
  await user.click(screen.getByTestId("webhook-secret-done"));
  expect(done).toHaveBeenCalledOnce();
});

test("SecretOnce after a rotation says how long the old secret keeps working", () => {
  render(
    <SecretOnce
      secret={SECRET}
      address="a"
      addressEl={createRef<HTMLElement>()}
      previousUntil={NOW + 86_400}
      nowS={NOW}
    />,
  );
  expect(screen.getByTestId("webhook-secret").textContent).toMatch(/the old one keeps working until/);
  expect(screen.queryByTestId("webhook-secret-done")).toBeNull();
});

test("SecretOnce without a clipboard selects the value for a manual copy", async () => {
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
  render(<SecretOnce secret={SECRET} address="a" addressEl={createRef<HTMLElement>()} nowS={NOW} />);
  await user.click(screen.getByTestId("webhook-copy-secret"));
  expect(screen.getByTestId("webhook-copied").textContent).toMatch(/secret is selected: copy it by hand/);
  expect(window.getSelection()?.toString()).toBe(SECRET);
});

test("SharedWebhookPanel: name links to its page, the address, the last delivery — and no secret controls", () => {
  render(
    <MemoryRouter>
      <SharedWebhookPanel
        id="0a1b2c3d4e5f6a7b"
        name="shop repo"
        path="/hooks/w/0a1b2c3d4e5f6a7b"
        lastDeliveryAt={NOW - 60}
        lastEvent="pull_request"
        lastNote=""
        unsigned24h={2}
        nowS={NOW}
      />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("shared-webhook-link").getAttribute("href")).toBe(
    "/library/webhooks/0a1b2c3d4e5f6a7b",
  );
  expect(screen.getByTestId("webhook-url").textContent).toBe(
    `${window.location.origin}/hooks/w/0a1b2c3d4e5f6a7b`,
  );
  expect(screen.getByTestId("webhook-last").textContent).toMatch(/pull_request/);
  expect(screen.getByTestId("webhook-unsigned").textContent).toMatch(/2 unsigned in 24 h/);
  expect(screen.queryByTestId("webhook-make-secret")).toBeNull();
  expect(screen.queryByText(/rotate/i)).toBeNull();
});

test("WebhookPicker: only pickable webhooks with their last delivery, + New webhook… opens the flow", async () => {
  const user = userEvent.setup();
  const onPick = vi.fn();
  const onNew = vi.fn();
  render(
    <WebhookPicker
      id="p"
      value=""
      list={[
        hook(),
        hook({ id: "rv1", name: "Old loop", legacy_automation: "rv1", pickable: false }),
      ]}
      error=""
      nowS={NOW}
      onPick={onPick}
      onNew={onNew}
    />,
  );
  const select = screen.getByTestId("trigger-webhook") as HTMLSelectElement;
  expect([...select.options].map((o) => o.textContent)).toEqual([
    "Choose a webhook…",
    "shop repo · no delivery yet",
    "+ New webhook…",
  ]);
  await user.selectOptions(select, "0a1b2c3d4e5f6a7b");
  expect(onPick).toHaveBeenCalledWith("0a1b2c3d4e5f6a7b");
  await user.selectOptions(select, "+ New webhook…");
  expect(onNew).toHaveBeenCalledOnce();
  expect(onPick).toHaveBeenCalledTimes(1);
});

test("WebhookPicker keeps a picked id that left the list, and says a read failed", () => {
  render(
    <WebhookPicker id="p" value="gone1234" list={[]} error="" nowS={NOW} onPick={() => {}} onNew={() => {}} />,
  );
  const select = screen.getByTestId("trigger-webhook") as HTMLSelectElement;
  expect(select.value).toBe("gone1234");
  expect(select.selectedOptions[0].textContent).toBe("gone1234 (not in the list any more)");
  expect(screen.getByTestId("trigger-webhook-empty")).toBeTruthy();
  render(
    <WebhookPicker id="q" value="" list={null} error="the store failed" nowS={NOW} onPick={() => {}} onNew={() => {}} />,
  );
  expect(screen.getByTestId("trigger-webhook-error").textContent).toMatch(/the store failed/);
});
