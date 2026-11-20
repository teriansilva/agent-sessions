/** Webhooks as a list (#2221) — the shapes `routes/webhooks.py` produces.
 *
 *  A webhook is a name, an address and a secret, usable by any service and picked by any
 *  automation. The secret itself is never in a read: it is in the ONE answer that makes it
 *  (`WebhookCreated`, `WebhookRotated`) and nowhere else. A delivery's shape is field names and
 *  value types only — the server never keeps a value. */

/** An automation that picked this webhook (or, for a legacy one, the review loop that owns it). */
export interface WebhookUser {
  id: string;
  name: string;
}

export interface Webhook {
  id: string;
  name: string;
  /** Fences a rename (`PATCH` with a stale revision is a 409). */
  revision: number;
  /** Root-relative: `/hooks/w/<id>`, or `/hooks/a/<automation>` for a legacy one. */
  path: string;
  /** On a Home Free box: the webhook's public address on the BattleLab relay, or `null`. */
  relay_url?: string | null;
  /** A #2202 review loop's own webhook: its automation's id. `null` for a webhook from the list. */
  legacy_automation: string | null;
  /** Whether an automation may pick it. A legacy webhook never is. */
  pickable: boolean;
  secret_set: boolean;
  secret_created_at: number | null;
  /** A rotated-out secret keeps verifying deliveries until then. */
  previous_valid_until: number | null;
  last_delivery_at: number | null;
  last_event: string;
  last_note: string;
  /** Unsigned or badly signed deliveries refused in the last 24 h. */
  unsigned_24h: number;
  used_by: WebhookUser[];
  created_at: number;
}

/** A value's type as the server names it (`webhooks._type_of`). */
export type ShapeType =
  "text" | "number" | "yes/no" | "empty" | "list" | "object";

/** One field of the last signed delivery: its path (`pull_request.number`, `labels[].name`) and
 *  its value's type. Never the value. */
export interface ShapeField {
  path: string;
  type: ShapeType | string;
}

/** `GET /api/webhooks/{id}`. */
export interface WebhookDetail extends Webhook {
  last_shape: ShapeField[];
}

export interface WebhookList {
  webhooks: Webhook[];
}

/** `POST /api/webhooks` (201): the new webhook and its secret, this once. */
export interface WebhookCreated {
  webhook: Webhook;
  secret: string;
}

/** `POST /api/webhooks/{id}/rotate` (201): the new secret, this once; the old one works 24 h more. */
export interface WebhookRotated {
  secret: string;
  webhook: WebhookDetail;
}
