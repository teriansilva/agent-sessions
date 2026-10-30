/** Pure rules of the share dialogs (#1714). The server decides at confirm; these mirror it so the
 *  picker and the preview show the same answer before the operator commits. */
import { engineName } from "../../app/engineRoster";
import type {
  ShareCatalog,
  ShareChoice,
  ShareCatalogItem,
  ShareImports,
  SharePreview,
  SharePreviewItem,
} from "../../types/share";

export const keyOf = (item: { kind: string; id: string }) => `${item.kind}:${item.id}`;

/** What a kind is called in a sentence ("needs skill tdd"). */
export const kindWord = (kind: string) => (kind === "playbook" ? "playbook" : kind === "skill" ? "skill" : kind);

/** The items the chosen ones pull in, each with the chosen items that need it. A chosen item is
 *  never listed as included. Transitive: a dependency's own dependencies come along too. */
export function closure(
  items: ShareCatalogItem[],
  chosen: ReadonlySet<string>,
): Map<string, string[]> {
  const byKey = new Map(items.map((i) => [keyOf(i), i]));
  const included = new Map<string, string[]>();
  for (const root of chosen) {
    const queue = [...(byKey.get(root)?.depends_on ?? [])];
    const seen = new Set<string>();
    while (queue.length) {
      const dep = queue.shift()!;
      if (seen.has(dep)) continue;
      seen.add(dep);
      if (!chosen.has(dep)) {
        const by = included.get(dep) ?? [];
        if (!by.includes(root)) by.push(root);
        included.set(dep, by);
      }
      queue.push(...(byKey.get(dep)?.depends_on ?? []));
    }
  }
  return included;
}

export type PreviewState =
  | { state: "import" }
  | { state: "reused" }
  | { state: "deselected" }
  | { state: "refused"; reason: string };

/** Each preview item's state for a selection, in dependency order: a deselected or refused
 *  dependency refuses its dependents (the server's rule at confirm). */
export function previewStates(
  items: SharePreviewItem[],
  selected: ReadonlySet<string>,
  /** Items an earlier confirm of this same file already created (key → local copy). */
  already: ReadonlyMap<string, string> = new Map(),
): Map<string, PreviewState> {
  const byKey = new Map(items.map((i) => [i.key, i]));
  const out = new Map<string, PreviewState>();
  const visit = (item: SharePreviewItem, trail: Set<string>): PreviewState => {
    const known = out.get(item.key);
    if (known) return known;
    let result: PreviewState;
    if (item.status === "refused") result = { state: "refused", reason: item.reason ?? "refused" };
    // A reused copy is used as it is: nothing to select, and it satisfies its dependents.
    else if (item.status === "reused") result = { state: "reused" };
    else if (!selected.has(item.key)) result = { state: "deselected" };
    else {
      result = { state: "import" };
      for (const depKey of item.depends_on) {
        const dep = byKey.get(depKey);
        if (!dep || trail.has(depKey)) continue;
        const depState = visit(dep, new Set([...trail, item.key]));
        if (depState.state === "deselected") {
          const copy = already.get(depKey);
          result = {
            state: "refused",
            reason: copy
              ? `Needs ${kindWord(dep.kind)} ${dep.id}, which was already imported as ${copy}. This ${kindWord(item.kind)} can only be imported together with its own new copy of it: tick it to import both (a second copy of ${dep.id}).`
              : `Needs ${kindWord(dep.kind)} ${dep.id}, which isn't being imported. Select it to import this ${kindWord(item.kind)}.`,
          };
          break;
        }
        if (depState.state === "refused") {
          result = {
            state: "refused",
            reason: `Needs ${kindWord(dep.kind)} ${dep.id}, which can't be imported.`,
          };
          break;
        }
      }
    }
    out.set(item.key, result);
    return result;
  };
  for (const item of items) visit(item, new Set());
  return out;
}

/** The selection that starts a preview: every item the server would import, except items an
 *  earlier confirm of this same file already created — importing those again makes a second copy,
 *  so the operator has to choose it. */
export function initialSelection(
  preview: SharePreview,
  already: ReadonlyMap<string, string> = new Map(),
): Set<string> {
  return new Set(
    preview.items
      .filter((i) => i.status !== "refused" && i.status !== "reused" && !already.has(i.key))
      .map((i) => i.key),
  );
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** A catalog answer, or null when the shape is not one (an e2e stub answers `{}`). */
export function asCatalog(value: unknown): ShareCatalog | null {
  if (!isObj(value) || !Array.isArray(value.items)) return null;
  const items = value.items.filter(
    (i): i is ShareCatalogItem =>
      isObj(i) && typeof i.kind === "string" && typeof i.id === "string" && Array.isArray(i.depends_on),
  );
  return { items, imports: asImports(value.imports) };
}

export function asImports(value: unknown): ShareImports {
  if (!isObj(value)) return {};
  const out: ShareImports = {};
  for (const [k, v] of Object.entries(value)) {
    if (isObj(v)) out[k] = { bundle: typeof v.bundle === "string" ? v.bundle : null, revision: typeof v.revision === "string" ? v.revision : null };
  }
  return out;
}

export function asPreview(value: unknown): SharePreview | null {
  if (!isObj(value) || typeof value.token !== "string" || !Array.isArray(value.items) || !isObj(value.bundle)) return null;
  return value as unknown as SharePreview;
}

/** `research-kit.battlelab · 41 KB` */
export function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The format's upload bound (`sharebundle/format.py` MAX_BUNDLE_BYTES). */
export const MAX_BUNDLE_BYTES = 32 * 1024 * 1024;

/** What this page knows about earlier imports of each bundle (keyed by its digest), kept above the
 *  dialog so closing and reopening it, Back, or another file in between never forget it. */
export interface ImportMemory {
  /** digest → item key → the copy a confirm created and the preview token that created it. */
  created: Map<string, Map<string, { dest: string; token: string }>>;
  /** Digests whose confirm may have been applied without an answer (a lost or refused confirm). */
  unclear: Set<string>;
}

export function newImportMemory(): ImportMemory {
  return { created: new Map(), unclear: new Set() };
}

/** Files this item would import that the preview couldn't show (too large for the preview's text
 *  budget, or not text): they can't be reviewed before confirm. */
export function unshown(item: SharePreviewItem): number {
  return item.files.filter((f) => typeof f.text !== "string").length;
}

/** One memory for the whole page (Skills and Playbooks alike); a reload starts it empty, and the
 *  server's import record (`GET /api/share/imports`) covers what it can't remember. */
export const sharedImportMemory: ImportMemory = newImportMemory();

/** Tests only: forget everything (each test is a fresh page). */
export function resetImportMemory(): void {
  sharedImportMemory.created.clear();
  sharedImportMemory.unclear.clear();
}

// ---- specialists (#1714 P2) --------------------------------------------------------------------

/** A specialist with nothing chosen to run it yet. */
export const NO_CHOICE: ShareChoice = { engine: "", model: "default", effort: null };

/** What this page knows about an agent: whether it can start a new session here and whether it
 *  can load skills (a tool channel). `undefined` = not in the roster (yet). */
export type AgentFacts = (engine: string) => { starts: boolean; skills: boolean } | undefined;

/** Why `choice` can't be sent for this specialist, judged on what this page knows; `null` when it
 *  may be sent (the server checks it again, the way a save is checked). */
export function choiceProblem(item: SharePreviewItem, choice: ShareChoice, agent: AgentFacts): string | null {
  if (!choice.engine) return "Choose the agent it runs on.";
  const facts = agent(choice.engine);
  if (facts && !facts.starts) return `${engineName(choice.engine)} can't start a session here. Choose another agent.`;
  if ((item.specialist?.skills.length ?? 0) > 0 && facts && !facts.skills)
    return `${engineName(choice.engine)} cannot load skills, and this specialist pins skills. Choose an agent that can.`;
  return null;
}
