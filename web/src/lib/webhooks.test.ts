/** #2221 — the pure half of Library → Webhooks: the words a row, a page and the picker show, all
 *  built from the server's public view (never a secret, never a delivery's value). */
import { describe, expect, test } from "vitest";

import type { Webhook } from "../types/webhooks";
import {
  deleteBlock,
  deliveryTone,
  lastDeliveryWords,
  legacyOwner,
  nameProblem,
  pickWords,
  pickableWebhooks,
  RELAY_NOTE,
  secretWords,
  usedByWords,
  webhookAddress,
  webhookMeta,
  webhookStatus,
} from "./webhooks";

const NOW = 1_800_000_000;

function hook(over: Partial<Webhook> = {}): Webhook {
  return {
    id: "0a1b2c3d4e5f6a7b",
    name: "shop repo",
    revision: 1,
    path: "/hooks/w/0a1b2c3d4e5f6a7b",
    legacy_automation: null,
    pickable: true,
    secret_set: true,
    secret_created_at: NOW - 3600,
    previous_valid_until: null,
    last_delivery_at: null,
    last_event: "",
    last_note: "",
    unsigned_24h: 0,
    used_by: [],
    created_at: NOW - 86400,
    ...over,
  };
}

describe("a row (frame 1)", () => {
  test("no delivery yet: grey, says so, and shows no unsigned count", () => {
    const w = hook();
    expect(deliveryTone(w)).toBe("idle");
    expect(lastDeliveryWords(w, NOW)).toBe("no delivery yet");
    expect(webhookStatus(w)).toBe("waiting for first delivery");
    expect(webhookMeta(w)).toBe("not used yet");
  });

  test("delivered: lit, who uses it, the last event and the unsigned count", () => {
    const w = hook({
      last_delivery_at: NOW - 120,
      last_event: "pull_request",
      used_by: [
        { id: "a1", name: "Review every PR" },
        { id: "a2", name: "Tell me about deploys" },
      ],
    });
    expect(deliveryTone(w)).toBe("up");
    expect(lastDeliveryWords(w, NOW)).toMatch(/today|yesterday/);
    expect(webhookStatus(w)).toBe("receiving");
    expect(webhookMeta(w)).toBe(
      "used by 2 automations · last: pull_request · 0 unsigned in 24 h",
    );
  });

  test("a legacy webhook names its review loop and its old address", () => {
    const w = hook({
      id: "rv1",
      path: "/hooks/a/rv1",
      legacy_automation: "rv1",
      pickable: false,
      used_by: [{ id: "rv1", name: "Review loop · shop" }],
      unsigned_24h: 3,
    });
    expect(legacyOwner(w)).toBe("Review loop · shop");
    expect(webhookMeta(w)).toBe(
      "old address · used by Review loop · shop · 3 unsigned in 24 h",
    );
    // An owner the server did not name falls back to its id.
    expect(legacyOwner({ ...w, used_by: [] })).toBe("rv1");
  });

  test("used by: none, one, several", () => {
    expect(usedByWords(0)).toBe("not used yet");
    expect(usedByWords(1)).toBe("used by 1 automation");
    expect(usedByWords(3)).toBe("used by 3 automations");
  });
});

describe("a webhook's page (frame 2)", () => {
  test("the address is this app's origin + the server's path", () => {
    expect(webhookAddress("/hooks/w/abc")).toBe(
      `${window.location.origin}/hooks/w/abc`,
    );
  });

  test("the secret's status, never the secret", () => {
    expect(secretWords(hook(), NOW)).toMatch(/^set .+ · never shown again$/);
    expect(secretWords(hook({ secret_created_at: null }), NOW)).toBe(
      "set · never shown again",
    );
    expect(secretWords(hook({ secret_set: false }), NOW)).toBe(
      "no secret: rotate to make one",
    );
  });

  test("delete is off while used, and never for a legacy webhook", () => {
    expect(deleteBlock(hook())).toBe("");
    expect(deleteBlock(hook({ used_by: [{ id: "a1", name: "A" }] }))).toBe(
      "It is used by 1 automation. Pick another webhook in it, or delete it, first.",
    );
    expect(
      deleteBlock(
        hook({
          used_by: [
            { id: "a1", name: "A" },
            { id: "a2", name: "B" },
          ],
        }),
      ),
    ).toMatch(/used by 2 automations\. Pick another webhook in them/);
    expect(
      deleteBlock(
        hook({
          legacy_automation: "rv1",
          used_by: [{ id: "rv1", name: "Loop" }],
        }),
      ),
    ).toMatch(/review loop “Loop”’s own address/);
  });

  test("a name the server would refuse", () => {
    expect(nameProblem("  ")).toBe("Give it a name");
    expect(nameProblem("x".repeat(61))).toBe("At most 60 characters");
    expect(nameProblem("bad\u0007name")).toBe("No control characters");
    expect(nameProblem(" shop repo ")).toBe("");
  });
});

describe("the picker (frame 3)", () => {
  test("offers only pickable webhooks, by name; never a legacy one", () => {
    const list = [
      hook({ id: "b", name: "status page" }),
      hook({
        id: "rv1",
        name: "Review loop",
        legacy_automation: "rv1",
        pickable: false,
      }),
      hook({ id: "a", name: "deploy notices" }),
    ];
    expect(pickableWebhooks(list).map((w) => w.id)).toEqual(["a", "b"]);
    expect(pickableWebhooks(null)).toEqual([]);
  });

  test("each option says when it last delivered", () => {
    expect(pickWords(hook(), NOW)).toBe("shop repo · no delivery yet");
    expect(pickWords(hook({ last_delivery_at: NOW - 60 }), NOW)).toMatch(
      /^shop repo · last: /,
    );
  });
});

describe("the relay address (#2221 P1b)", () => {
  test("gives the sender the relay address on a Home Free box, else this app's own", () => {
    expect(
      webhookAddress("/hooks/w/abc", "https://aaaa.hook.battlelabos.com/"),
    ).toBe("https://aaaa.hook.battlelabos.com/");
    expect(webhookAddress("/hooks/w/abc", null)).toBe(
      `${window.location.origin}/hooks/w/abc`,
    );
    expect(RELAY_NOTE).toMatch(/sees its contents in transit/);
  });
});
