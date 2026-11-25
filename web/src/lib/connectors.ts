/** HQ connectors (#1746): the client view of `/api/connectors`, and the few rules the settings
 *  page needs to say what state a connector is in.
 *
 *  Everything here is a READING of what the server returned. The server decides every state
 *  (`status`, `enabled.read`, whether a test is current); the page never infers one the server did
 *  not report, because a page that thinks a connector is ready when the store does not would offer
 *  a switch the server then refuses. */

export type ConnectorStatus =
  "unavailable" | "needs_origin" | "needs_token" | "needs_test" | "ready";

export type ConnectorManifest = {
  id: string;
  label: string;
  publisher: string;
  version: string;
  category: string;
  adapter: string;
  origin_mode: "operator" | "fixed" | string;
  fixed_origin: string | null;
  auth: { kind: string; scopes: string[] };
  capabilities: { read: string[]; send?: string[] };
  setup: string;
};

export type ConnectorInstance = {
  id: string;
  manifest_id: string;
  origin: string;
  status: ConnectorStatus;
  token_set: boolean;
  /** A `basic` connector's account name (#1829); `null` for a token connector. Never secret. */
  account?: string | null;
  generation: number;
  tested_generation: number | null;
  tested: { login: string; repos: number; at: number } | null;
  last_test: {
    generation: number;
    ok: boolean;
    code: string | null;
    at: number;
  } | null;
  enabled: { read: boolean };
  /** A messenger's (#1832): who it may message, set only from server-listed candidates. */
  recipients?: ConnectorRecipient[];
  /** The recipients were chosen for THIS bot and origin (a different one needs a re-save). */
  recipients_bound?: boolean;
  /** The bot identity the saved recipients were chosen for. */
  recipients_chosen_for?: string | null;
  /** Whether HQ may propose messages now (the switch, and everything it needs). */
  send?: boolean;
  /** Why sending was last switched off by a change of bot or origin. */
  send_off?: {
    reason: "identity_changed" | "origin_changed";
    was: string;
    now: string;
    at: number;
  } | null;
  /** An email connector's From address (#1836): `""` until set. Absent on every other kind. */
  sender?: string;
  /** A messenger's notify route (#1841): the settings, and whether it is on NOW. */
  notify?: NotifySettings;
  /** Whether notify can be switched on now; when not, `notify_why` says why in words. */
  notify_ok?: boolean;
  notify_why?: string | null;
  created_at: number;
};

export const NOTIFY_CLASSES = [
  "needs you",
  "mission escalation",
  "automation failed",
] as const;
export type NotifyClass = (typeof NOTIFY_CLASSES)[number];
export const NOTIFY_WINDOWS = [0, 30, 60, 120, 300, 600] as const;

export type NotifyQuiet = { from: string; to: string; tz: string };

/** A messenger's notify route (#1841), as the server reports it. */
export type NotifySettings = {
  enabled: boolean;
  destination: string;
  /** What notify was pinned to when it was switched on (`member`: Matrix's other person). */
  identity: { id: string; label: string; member: string } | null;
  classes: NotifyClass[];
  window_s: number;
  quiet: NotifyQuiet | null;
  cap_per_hour: number;
  include_line: boolean;
  since: number | null;
  /** Why the route went back to off by itself. */
  off: {
    reason:
      | "connector_changed"
      | "needs_test"
      | "recipients_unbound"
      | "destination_removed";
    at: number;
  } | null;
};

export type NotifyBody = {
  enabled: boolean;
  destination_id: string;
  classes: NotifyClass[];
  window_s: number;
  quiet: NotifyQuiet | null;
  cap_per_hour: number;
  include_line: boolean;
};

/** One copy a route sent, skipped or gave up (#1841): never its text. */
export type ConnectorDelivery = {
  id: string;
  state: string;
  at: number;
  settled_at: number | null;
  code: string;
  members: { label: string; link: string; sent: boolean; resolved: boolean }[];
};

export const NOTIFY_CLASS_LABEL: Record<NotifyClass, string> = {
  "needs you": "Needs you — a session is waiting on you",
  "mission escalation": "Mission escalations",
  "automation failed": "Automation failures",
};

/** A saved recipient a route may use: a Telegram private chat, a Matrix room (checked for just
 *  two people when switched on), or a typed address. */
export function notifyEligible(adapter: string, kind: string): boolean {
  if (adapter === "telegram-bot") return kind === "private chat";
  if (adapter === "matrix-cs") return kind === "room";
  return adapter === "smtp" && kind === "email";
}

/** Why a route went back to off by itself, in words. */
export function notifyOffLine(off: NotifySettings["off"]): string | null {
  if (!off) return null;
  return {
    connector_changed:
      "Notifications were switched off because this connector changed (its token, address or account). Switch them on again to send here.",
    needs_test:
      "Notifications were switched off because the connector’s last test failed. Test it again, then switch them on.",
    recipients_unbound:
      "Notifications were switched off because the recipients were chosen for another bot or account. Save them again, then switch notifications on.",
    destination_removed:
      "Notifications were switched off because their destination is no longer a saved recipient.",
  }[off.reason];
}

/** One delivery, in plain words: ``[what happened, detail or null]``. */
export function deliveryWords(d: ConnectorDelivery): [string, string | null] {
  const code = d.code ? ` (${d.code.replace(/_/g, " ")})` : "";
  switch (d.state) {
    case "retracted":
      return ["Sent · ✓ resolved", null];
    case "delivered":
    case "delivered_final":
    case "retract_unsupported":
      return ["Sent", null];
    case "retract_failed":
      return ["Sent", "couldn’t mark it resolved yet; trying again"];
    case "retract_gave_up":
      return ["Sent", "couldn’t mark it resolved"];
    case "retract_lost":
      return ["Sent", "can’t be marked resolved: the connector changed"];
    case "retract_window_passed":
      return ["Sent", "no longer marked when handled (after 30 days)"];
    case "retract_evicted":
      return [
        "No longer marked when handled",
        "too many recent notifications were kept",
      ];
    case "skipped_closed":
      return ["Skipped · already handled", null];
    case "skipped_quiet":
      return ["Skipped · quiet hours", null];
    case "skipped_rate":
      return ["Skipped · hourly limit", null];
    case "skipped_class":
      return ["Skipped · that kind is off", null];
    case "expired":
      return ["Not sent", "it couldn’t be checked in time"];
    case "unconfirmed":
      return [
        "Not confirmed",
        "no clear answer came back; it is not sent again",
      ];
    case "failed":
      return ["Not sent", `nothing went out${code}`];
    case "held_unknown":
      return ["Waiting", "couldn’t check it yet"];
    case "delivering":
      return ["Sending", null];
    default:
      return [d.state, null];
  }
}

/** A dispatch's route state (#1841), short: "sent · ✓ resolved", "skipped · quiet hours". */
export function routeWords(r: {
  state: string;
  sent: boolean;
  resolved: boolean;
}): string {
  if (r.sent) return r.resolved ? "sent · ✓ resolved" : "sent";
  const skipped: Record<string, string> = {
    skipped_closed: "skipped · already handled",
    skipped_quiet: "skipped · quiet hours",
    skipped_rate: "skipped · hourly limit",
    skipped_class: "skipped · that kind is off",
    expired: "not sent · couldn’t check in time",
    failed: "not sent",
    unconfirmed: "not confirmed",
    held_unknown: "waiting",
    delivering: "sending",
    pending: "waiting",
  };
  return skipped[r.state] ?? r.state.replace(/_/g, " ");
}

export type ConnectorCatalog = {
  catalog: ConnectorManifest[];
  instances: ConnectorInstance[];
  disclosure: string;
};

export type ConnectorTestResult =
  | {
      status: "ok";
      login: string;
      repos: number;
      generation: number;
      /** Written by the adapter, e.g. a GitHub classic token that can also write (#1826). */
      warning?: string;
    }
  | { status: "failed"; code: string; detail: string; generation: number }
  | { status: "stale"; generation: number };

/** What a manifest's read capability names, as the page lists it. */
const READS: Record<string, string> = {
  repos: "repositories",
  issues: "issues",
  pulls: "pull requests",
  ci: "CI status",
  files: "files",
  folders: "folders",
  search: "search by name",
  documents: "text files",
};

export function readsLabel(reads: string[]): string {
  return reads.map((r) => READS[r] ?? r).join(" · ");
}

/** A failed test, said in words. The server's own `detail` wins when the page has it (it names the
 *  host and the HTTP status); this is for the stored `last_test`, which keeps only the code. */
const TEST_CODES: Record<string, string> = {
  rejected:
    "The forge refused the token. Replace it with a current one that has the scopes this connector asks for.",
  unreachable: "That address could not be reached.",
  timeout: "The forge did not answer in time.",
  not_found:
    "That address answered, but not with the API this connector reads.",
  bad_reply:
    "That address answered, but not with the API this connector reads.",
  too_large: "The forge's answer was larger than a connector accepts.",
  refused: "That address is not one a connector may reach.",
};

export function testFailure(
  code: string | null | undefined,
  detail?: string,
): string {
  if (detail) return detail;
  return (code && TEST_CODES[code]) || "The test did not pass.";
}

/** The last test failed AND it was a test of what is stored now. A failure from before an edit
 *  says nothing about the current address or token. */
export function failedNow(inst: ConnectorInstance): boolean {
  return (
    !!inst.last_test &&
    !inst.last_test.ok &&
    inst.last_test.generation === inst.generation
  );
}

export type ConnectorTone = "up" | "idle" | "draft" | "down";

/** The status line under a connector's name, with the `.hud-led` tone that means that state
 *  (`docs/design.md` §3: status colour is load-bearing). Only a connector HQ reads through is
 *  `up`; one still being set up is `draft` (steady, not the blinking `attention`: an unfinished
 *  setup is not something that needs you), a tested one with reading off is `idle`. */
export function statusLine(
  inst: ConnectorInstance,
  category = "versioning",
): { tone: ConnectorTone; text: string } {
  if (inst.status === "unavailable")
    return { tone: "down", text: "No longer in the catalog" };
  if (failedNow(inst)) return { tone: "down", text: "Test failed" };
  switch (inst.status) {
    case "needs_origin":
      return { tone: "draft", text: "Needs its address" };
    case "needs_token":
      return { tone: "draft", text: "Needs a token" };
    case "needs_test":
      return { tone: "draft", text: "Needs a test" };
    default:
      if (category === "messenger")
        return inst.send
          ? { tone: "up", text: "HQ may propose messages" }
          : { tone: "idle", text: "Tested · sending is off" };
      return inst.enabled.read
        ? { tone: "up", text: "HQ reads through it" }
        : { tone: "idle", text: "Tested · reading is off" };
  }
}

export type ConnectorRecipient = { id: string; label: string; kind: string };
export type RecipientCandidate = ConnectorRecipient & {
  allowed: boolean;
  /** Why it can't be allowed (an encrypted Matrix room), or absent. */
  unavailable?: string;
  /** How many people are in it (shown so the Commander sees who would read). */
  members?: number;
};

/** What HQ gets from a connector, in one line, from its manifest's capabilities: the setup
 *  wizard's Connect HQ tile (#1880). */
export function tileLine(m: ConnectorManifest): string {
  // `search` is how HQ finds things, not something it reads: it stays out of the sentence.
  const things = m.capabilities.read
    .filter((r) => r !== "search")
    .map((r) => readsLabel([r]));
  if (things.length) {
    const list =
      things.length === 1
        ? things[0]
        : `${things.slice(0, -1).join(", ")} and ${things[things.length - 1]}`;
    return `HQ reads your ${list}.`;
  }
  if (m.capabilities.read.length) return "HQ looks things up there for you.";
  return "HQ proposes a message; nothing is sent until you tap it.";
}

/** The instance a Connect HQ tile stands for (#1880): the one HQ works through if there is one,
 *  else the first. */
export function tileInstance(
  m: ConnectorManifest,
  instances: ConnectorInstance[],
): ConnectorInstance | undefined {
  const mine = instances.filter((i) => i.manifest_id === m.id);
  return mine.find((i) => statusLine(i, m.category).tone === "up") ?? mine[0];
}

/** Why sending was switched off by a change of bot, account or origin (#1832), in words. */
export function sendOffLine(inst: ConnectorInstance): string | null {
  const off = inst.send_off;
  if (!off) return null;
  return off.reason === "identity_changed"
    ? `Sending was switched off because this connector now signs in as ${off.now}. The recipients below were chosen for ${off.was}; check them and save again.`
    : `Sending was switched off because this connector now points at ${off.now}. Check the recipients and save them again.`;
}
