/** What a playbook input will resolve to in the New project wizard, and whether a required one is
 *  DEFINITELY missing (#1508) — so the card can say so before the review does.
 *
 *  It mirrors the server's resolution order (`playbooks/binding.resolve`): a text variable is
 *  project → global → the playbook default; a secret has no implicit global fallback. Before the
 *  project exists there is no project binding, so "inherit" is exactly global-then-default for text
 *  and nothing for a secret. The server's review stays the authority: whenever this cannot be sure
 *  (the globals could not be read, or the project already exists and may hold a binding) it says
 *  `unknown` and never holds the wizard. */
import type { TemplateVariable } from "../../types/api";
import type { Variable } from "./playbookDraft";
import type { InputChoice } from "./useProjectPlaybook";

/** The globals the wizard read, by name; `null` = not read (failed or still loading). */
export type Globals = ReadonlyMap<string, TemplateVariable> | null;

export type InputState = "ok" | "missing" | "unknown";

const SHOWN_MAX = 28;

/** A value as a chip shows it: one line, cut with an ellipsis. */
export function shown(v: unknown): string {
  const s = String(v).replace(/\s+/g, " ").trim();
  return s.length > SHOWN_MAX ? `${s.slice(0, SHOWN_MAX - 1)}…` : s;
}

function playbookDefault(variable: Variable): string | null {
  if (variable.default == null) return null;
  const s = String(variable.default);
  return s === "" ? null : s;
}

/** The global a variable of this kind would read: a set text value, or a stored secret that still
 *  decrypts. `undefined` = there is none; `null` = the globals are not known. */
function usableGlobal(variable: Variable, globals: Globals): TemplateVariable | null | undefined {
  if (!globals) return null;
  const g = globals.get(variable.name);
  const secret = variable.kind === "secret";
  if (!g || (g.kind === "secret") !== secret) return undefined;
  if (secret) return g.set && !g.needs_reentry ? g : undefined;
  return g.value ? g : undefined;
}

/** The three choices as the segmented control labels them, with the value each one means. */
export function sourceLabels(
  variable: Variable,
  globals: Globals,
  projectExists: boolean,
): { value: string; global: string; inherit: string; inheritHint: string } {
  const secret = variable.kind === "secret";
  const g = usableGlobal(variable, globals);
  const global =
    g === null
      ? "Global"
      : g === undefined
        ? "Global · none saved"
        : secret
          ? "Global · set"
          : `Global · ${shown(g.value)}`;
  if (projectExists)
    return {
      value: "Value",
      global,
      inherit: "Saved",
      inheritHint: "The project’s saved binding, if it has one.",
    };
  if (secret)
    return {
      value: "Value",
      global,
      inherit: "No default",
      inheritHint: "A secret has no default and never falls back to a global on its own.",
    };
  if (g)
    return {
      value: "Value",
      global,
      inherit: `Default · ${shown(g.value)}`,
      inheritHint: `From the global variable ${variable.name}. Pick Value to override it for this project.`,
    };
  const d = playbookDefault(variable);
  if (d !== null)
    return {
      value: "Value",
      global,
      inherit: `Default · ${shown(d)}`,
      inheritHint: "The playbook’s default. Pick Value to override it for this project.",
    };
  return {
    value: "Value",
    global,
    inherit: g === null ? "Default" : "No default",
    inheritHint:
      g === null
        ? "A global variable with this name, else the playbook’s default."
        : "This playbook declares no default.",
  };
}

/** Whether this input resolves, as far as the wizard can know before the review. */
export function inputState(
  variable: Variable,
  choice: InputChoice,
  globals: Globals,
  projectExists: boolean,
): InputState {
  const secret = variable.kind === "secret";
  if (choice.mode === "value") return choice.value.trim() === "" ? "missing" : "ok";
  if (choice.mode === "global") {
    const g = usableGlobal(variable, globals);
    return g === null ? "unknown" : g === undefined ? "missing" : "ok";
  }
  // inherit
  if (projectExists) return "unknown";
  if (secret) return "missing";
  if (playbookDefault(variable) !== null) return "ok";
  const g = usableGlobal(variable, globals);
  return g === null ? "unknown" : g === undefined ? "missing" : "ok";
}

/** Required inputs that are definitely missing — what the card marks and what holds Next. */
export function missingRequired(
  variables: readonly Variable[],
  choices: Readonly<Record<string, InputChoice>>,
  globals: Globals,
  projectExists: boolean,
): string[] {
  return variables
    .filter(
      (v) =>
        v.required &&
        inputState(v, choices[v.name] ?? { mode: "inherit", value: "" }, globals, projectExists) ===
          "missing",
    )
    .map((v) => v.name);
}
