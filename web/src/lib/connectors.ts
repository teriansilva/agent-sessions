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
  created_at: number;
};

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
