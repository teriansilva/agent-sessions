/** Duplicate an MCP server (#2430): two logins for one service in one project. The dialog's pure
 *  logic — the copy's first free name, one row per DISTINCT credential variable (two settings naming
 *  one variable are remapped together), what the form checks before it sends anything, the request
 *  body, and the retry memory: a new credential is stored in Variables BEFORE the duplicate, and a
 *  stored one becomes an "existing" pick, so a retry (a name clash, a stale revision, a lost
 *  response) never stores it again. No React and no fetch here; `duplicateModel.test.ts` pins it. */
import type { McpSetting } from "../../types/mcp";
import { NAME_RE, SECRET_MIN, VARIABLE_RE, nameError } from "./mcpModel";

/** The registry's and the variables store's length caps (`NAME_RE`, `VARIABLE_RE`). */
const NAME_MAX = 32;

/** `<base>_2`, `_3`, … — the first one not taken that still fits `rule` (32 characters: the base is
 *  shortened, never the number). */
function nextFree(base: string, taken: ReadonlySet<string>, rule: RegExp): string {
  for (let n = 2; ; n++) {
    const tail = `_${n}`;
    const head = base.slice(0, NAME_MAX - tail.length).replace(/[-_]+$/, "");
    const candidate = `${head}${tail}`;
    if (!taken.has(candidate) && rule.test(candidate)) return candidate;
    if (n > 999) return candidate; // a registry holds at most 64 servers: never reached
  }
}

/** The copy's name: `<name>_2`, or the next number free among the servers listed. */
export const nextFreeName = (name: string, taken: Iterable<string>) => nextFree(name, new Set(taken), NAME_RE);

/** A new credential's suggested variable: `<variable>_2`, or the next free among the secrets. */
export const nextFreeVariable = (variable: string, taken: Iterable<string>) =>
  nextFree(variable, new Set(taken), VARIABLE_RE);

/** One credential variable the original names, with the settings that read it. */
export interface CredentialGroup {
  variable: string;
  keys: string[];
  deliver: "env" | "header";
}

/** One row per DISTINCT credential variable, in the order the settings first name them. */
export function credentialGroups(settings: McpSetting[]): CredentialGroup[] {
  const out: CredentialGroup[] = [];
  for (const s of settings) {
    if (s.kind !== "credential") continue;
    const g = out.find((x) => x.variable === s.variable);
    if (g) g.keys.push(s.key);
    else out.push({ variable: s.variable, keys: [s.key], deliver: s.deliver });
  }
  return out;
}

/** What the copy reads for one of the original's variables: an EXISTING library secret (`target`,
 *  the original's own by default), or a NEW one (`newName` + its write-only `secret`). */
export interface DupRow {
  mode: "existing" | "new";
  target: string;
  newName: string;
  secret: string;
}

/** A row nobody has touched yet: keep the original's variable. With an empty library there is
 *  nothing to pick, so it starts on New credential. */
export function defaultRow(group: CredentialGroup, secretNames: ReadonlySet<string>, libraryEmpty: boolean): DupRow {
  return {
    mode: libraryEmpty ? "new" : "existing",
    target: group.variable,
    newName: nextFreeVariable(group.variable, secretNames),
    secret: "",
  };
}

export interface DupErrors {
  name?: string;
  /** Keyed by the ORIGINAL variable. */
  rows: Record<string, { variable?: string; value?: string }>;
}

export const hasErrors = (e: DupErrors) => !!e.name || Object.keys(e.rows).length > 0;

/** What the form refuses before it sends anything. `takenNames` are the servers listed (plus any
 *  name the server already refused); `secretNames` the library's secrets (plus any this dialog
 *  stored). Keeping the original's variable is always allowed: the request omits it. */
export function validateDuplicate(input: {
  name: string;
  takenNames: ReadonlySet<string>;
  groups: CredentialGroup[];
  rows: Record<string, DupRow>;
  secretNames: ReadonlySet<string>;
}): DupErrors {
  const out: DupErrors = { rows: {} };
  const n = nameError(input.name);
  if (n) out.name = n;
  else if (input.takenNames.has(input.name)) out.name = `A server named ${input.name} exists. Pick another name.`;
  const newNames = new Set<string>();
  for (const g of input.groups) {
    const r = input.rows[g.variable];
    const e: { variable?: string; value?: string } = {};
    if (r.mode === "existing") {
      if (!r.target) e.variable = "Pick a secret.";
      else if (r.target !== g.variable && !input.secretNames.has(r.target))
        e.variable = "No secret of that name in Variables.";
    } else {
      if (!VARIABLE_RE.test(r.newName)) e.variable = "Lowercase letters, digits and _ (a letter first).";
      else if (input.secretNames.has(r.newName))
        e.variable = "A variable of that name exists: pick it under Existing variable.";
      else if (newNames.has(r.newName)) e.variable = "Two new credentials share this name.";
      newNames.add(r.newName);
      if (r.secret.length < SECRET_MIN) e.value = `At least ${SECRET_MIN} characters.`;
      else if (r.secret !== r.secret.trim()) e.value = "No spaces at the start or end.";
    }
    if (Object.keys(e).length) out.rows[g.variable] = e;
  }
  return out;
}

/** Store every NEW credential as a Variables secret, one request each, exactly as the wizard does.
 *  A stored row becomes an EXISTING pick of the name it was stored under, its typed value dropped:
 *  a retry then sends that name and never stores it twice. Stops at the first refusal. */
export async function storeNewSecrets(
  groups: CredentialGroup[],
  rows: Record<string, DupRow>,
  create: (name: string, value: string) => Promise<unknown>,
  errorText: (e: unknown) => string,
): Promise<{ rows: Record<string, DupRow>; created: string[]; failed?: { variable: string; message: string } }> {
  const out = { ...rows };
  const created: string[] = [];
  for (const g of groups) {
    const r = out[g.variable];
    if (r.mode !== "new") continue;
    try {
      await create(r.newName, r.secret);
    } catch (e) {
      return { rows: out, created, failed: { variable: g.variable, message: errorText(e) } };
    }
    out[g.variable] = { ...r, mode: "existing", target: r.newName, secret: "" };
    created.push(r.newName);
  }
  return { rows: out, created };
}

/** The remap the request sends: only variables whose pick differs from the original's. */
export function remap(groups: CredentialGroup[], rows: Record<string, DupRow>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const g of groups) {
    const r = rows[g.variable];
    if (r.mode === "existing" && r.target && r.target !== g.variable) out[g.variable] = r.target;
  }
  return out;
}

/** `POST …/duplicate`'s body. One `requestId` per dialog: a retry after a lost response gets the
 *  copy that request already made, never a second one. */
export function duplicateBody(input: {
  name: string;
  label: string;
  groups: CredentialGroup[];
  rows: Record<string, DupRow>;
  expected: number;
  requestId: string;
}) {
  const variables = remap(input.groups, input.rows);
  return {
    name: input.name.trim(),
    ...(input.label.trim() ? { label: input.label.trim() } : {}),
    ...(Object.keys(variables).length ? { variables } : {}),
    expected_updated_at: input.expected,
    request_id: input.requestId,
  };
}

/** Where a refusal from the route belongs: on the name field, on the row whose pick it names, or
 *  under the form. */
export function refusalPlace(
  message: string,
  groups: CredentialGroup[],
  rows: Record<string, DupRow>,
): { field: "name" } | { field: "row"; variable: string } | { field: "form" } {
  if (/server named|^a name is|^names starting with/.test(message)) return { field: "name" };
  const m = message.match(/^no secret named (\S+) in Variables$/) ?? message.match(/^(\S+) in Variables is not a secret$/);
  if (m) {
    const g = groups.find((x) => rows[x.variable]?.target === m[1]);
    if (g) return { field: "row", variable: g.variable };
  }
  return { field: "form" };
}

/** `nc_pass_2 stays in Variables.` / `a and b stay in Variables.` */
export function staysWords(created: string[]): string {
  if (!created.length) return "";
  const list = created.length === 1 ? created[0] : `${created.slice(0, -1).join(", ")} and ${created[created.length - 1]}`;
  return `${list} ${created.length === 1 ? "stays" : "stay"} in Variables.`;
}

/** The copy's page says what it is and what it still needs (it is enabled nowhere). */
export function duplicatedWords(original: string, copy: string, variables: Record<string, string>): string {
  const reads = Object.values(variables);
  const reading = reads.length ? `, reading ${reads.join(", ")}` : "";
  return `Duplicated ${original} as ${copy}${reading}. It is enabled in no project: where this login belongs, use the shared credentials or the project’s own, run its check, then switch it on.`;
}
