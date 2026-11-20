/** Webhooks as a list (#2221): the pure half of Library → Webhooks and the automation editor's
 *  webhook picker. Every word here is built from the server's public view; nothing reads or holds a
 *  secret, and a delivery is described by its field NAMES and TYPES only. */
import type { Tone } from "./automations";
import { whenWords } from "./automations";
import { appUrl } from "./appBase";
import type { Webhook } from "../types/webhooks";

/** The public address a sender delivers to. On a Home Free box it is the webhook's address on the
 *  BattleLab relay (#2221 P1b); otherwise this app's origin + the server's root-relative path. */
export function webhookAddress(path: string, relayUrl?: string | null): string {
  return relayUrl ? relayUrl : `${window.location.origin}${appUrl(path)}`;
}

/** Shown under a relay address: the one thing the relay sees that the app channel never shows it. */
export const RELAY_NOTE =
  "Through the BattleLab relay: it forwards each delivery to this box and sees its contents in transit. It never stores them and has no secret, so it can't fake a delivery.";

/** The LED beside a webhook's last delivery: lit once a signed delivery arrived, grey before. */
export function deliveryTone(w: Pick<Webhook, "last_delivery_at">): Tone {
  return w.last_delivery_at == null ? "idle" : "up";
}

/** "14:02 today" / "no delivery yet". */
export function lastDeliveryWords(
  w: Pick<Webhook, "last_delivery_at">,
  nowS: number,
): string {
  return w.last_delivery_at == null
    ? "no delivery yet"
    : whenWords(w.last_delivery_at, nowS);
}

/** The status stamp beside a webhook's name (frame 7's "waiting for first delivery"). */
export function webhookStatus(w: Pick<Webhook, "last_delivery_at">): string {
  return w.last_delivery_at == null
    ? "waiting for first delivery"
    : "receiving";
}

export function usedByWords(n: number): string {
  if (n === 0) return "not used yet";
  return `used by ${n} ${n === 1 ? "automation" : "automations"}`;
}

/** The review loop a legacy webhook belongs to, by name (its id when the name is not known). */
export function legacyOwner(
  w: Pick<Webhook, "legacy_automation" | "used_by">,
): string {
  if (!w.legacy_automation) return "";
  return (
    w.used_by.find((u) => u.id === w.legacy_automation)?.name ??
    w.legacy_automation
  );
}

/** One row's meta line (frame 1): who uses it, what arrived last, refused deliveries. */
export function webhookMeta(w: Webhook): string {
  const parts = [
    w.legacy_automation
      ? `old address · used by ${legacyOwner(w)}`
      : usedByWords(w.used_by.length),
  ];
  if (w.last_event) parts.push(`last: ${w.last_event}`);
  if (w.last_delivery_at != null || w.unsigned_24h > 0)
    parts.push(unsignedWords(w.unsigned_24h));
  return parts.join(" · ");
}

export function unsignedWords(n: number): string {
  return `${n} unsigned in 24 h`;
}

/** The secret's status — never the secret: "set Thu 12 Nov 14:02 · never shown again". */
export function secretWords(
  w: Pick<Webhook, "secret_set" | "secret_created_at">,
  nowS: number,
): string {
  if (!w.secret_set) return "no secret: rotate to make one";
  const made =
    w.secret_created_at == null
      ? ""
      : ` ${whenWords(w.secret_created_at, nowS)}`;
  return `set${made} · never shown again`;
}

/** Why Delete is not offered, or "" when it is. The server refuses the same cases (409). */
export function deleteBlock(
  w: Pick<Webhook, "legacy_automation" | "used_by">,
): string {
  if (w.legacy_automation)
    return `It is the review loop “${legacyOwner(w)}”’s own address: it goes when that automation moves to a webhook from the list.`;
  const n = w.used_by.length;
  if (n > 0)
    return `It is ${usedByWords(n)}. Pick another webhook in ${n === 1 ? "it" : "them"}, or delete ${n === 1 ? "it" : "them"}, first.`;
  return "";
}

/** What the automation editor offers: only webhooks an automation may pick, by name. */
export function pickableWebhooks(list: Webhook[] | null): Webhook[] {
  return (list ?? [])
    .filter((w) => w.pickable && !w.legacy_automation)
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** One option's words in the picker (frame 3): "shop repo · last: 14:02 today". */
export function pickWords(w: Webhook, nowS: number): string {
  return `${w.name} · ${w.last_delivery_at == null ? "no delivery yet" : `last: ${whenWords(w.last_delivery_at, nowS)}`}`;
}

/** A name the server would refuse (`webhooks._name`: 1–60 characters, no control characters),
 *  or "" when it is fine. The server's answer still wins. */
export function nameProblem(name: string): string {
  const n = name.trim();
  if (!n) return "Give it a name";
  if (n.length > WEBHOOK_NAME_MAX)
    return `At most ${WEBHOOK_NAME_MAX} characters`;
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f\x7f]/.test(n)) return "No control characters";
  return "";
}

/** `webhooks.NAME_MAX`. */
export const WEBHOOK_NAME_MAX = 60;
