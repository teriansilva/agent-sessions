/** The New project wizard's pure logic (#1187): the steps, each step's validity, the folder the
 *  draft names, and the warnings the FOLDER and NAME steps raise. No React here, so every rule has
 *  a unit test of its own (`newProjectSteps.test.ts`).
 *
 *  The server stays the authority on everything it enforces — the name, the colour, `$HOME`
 *  containment and the one-project-per-folder rule (409). These checks only say it earlier. */
import { COLOR_PRESETS } from "../lib/projectColors";
import type { ProjectEntity } from "../types/api";

export const STEPS = [
  { id: "name", label: "Name" },
  { id: "folder", label: "Folder" },
  { id: "colour", label: "Colour" },
  { id: "review", label: "Review" },
  { id: "done", label: "Done" },
] as const;

export type StepId = (typeof STEPS)[number]["id"];

export const stepIndex = (id: StepId): number => STEPS.findIndex((s) => s.id === id);

export interface ProjectDraft {
  name: string;
  folderMode: "new" | "existing";
  /** New folder: the parent directory. `""` until the home directory has loaded. */
  parent: string;
  /** New folder: the folder's name under `parent`. */
  folderName: string;
  /** Existing folder: the picked path. */
  existingPath: string;
  color: string;
  makeDefault: boolean;
  /** What the operator changed themselves — the dirty test reads these rather than comparing
   *  values that the wizard also fills in on its own (the home parent, the name-derived folder
   *  name, the preselected colour). */
  touched: { parent: boolean; folderName: boolean; color: boolean };
}

export const initialDraft = (color = ""): ProjectDraft => ({
  name: "",
  folderMode: "new",
  parent: "",
  folderName: "",
  existingPath: "",
  color,
  makeDefault: false,
  touched: { parent: false, folderName: false, color: false },
});

/** Mirrors `fsbrowse._valid_name`: ONE path component — not empty, not `.`/`..`, no separator,
 *  no control character, at most 255 characters. */
export function validFolderName(name: string): boolean {
  const n = name.trim();
  if (!n || n === "." || n === ".." || n.length > 255) return false;
  if (n.includes("/") || n.includes("\\")) return false;
  for (const ch of n) if (ch.charCodeAt(0) < 32) return false;
  return true;
}

/** A folder name suggested from the project name while the operator has not typed one: lower
 *  case, runs of anything but letters, digits, `.`, `_` and `-` become one dash. */
export function suggestFolderName(projectName: string): string {
  return projectName
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

const join = (parent: string, name: string) =>
  `${parent.replace(/\/+$/, "")}/${name.trim()}`;

/** The folder the draft names, or `""` while it names none. */
export function folderPath(d: ProjectDraft): string {
  if (d.folderMode === "existing") return d.existingPath;
  if (!d.parent || !validFolderName(d.folderName)) return "";
  return join(d.parent, d.folderName);
}

export function stepValid(step: StepId, d: ProjectDraft): boolean {
  switch (step) {
    case "name":
      return d.name.trim() !== "";
    case "folder":
      return folderPath(d) !== "";
    case "colour":
      return true;
    case "review":
      return stepValid("name", d) && stepValid("folder", d);
    case "done":
      return false;
  }
}

/** Steps up to and including the first invalid one are reachable from the rail; later ones are
 *  not (you cannot review a project without a name). */
export function reachable(index: number, d: ProjectDraft): boolean {
  for (let i = 0; i < index; i++) if (!stepValid(STEPS[i].id, d)) return false;
  return index < stepIndex("done");
}

/** An active project already called this — a WARNING, never a block: the store allows it. */
export function nameClash(name: string, projects: ProjectEntity[]): ProjectEntity | null {
  const n = name.trim().toLowerCase();
  if (!n) return null;
  return projects.find((p) => !p.archived && p.name.trim().toLowerCase() === n) ?? null;
}

const nested = (a: string, b: string) =>
  a === b || a.startsWith(`${b.replace(/\/+$/, "")}/`) || b.startsWith(`${a.replace(/\/+$/, "")}/`);

/** The project (archived ones included, like the server's check) that already owns `path`, the
 *  folder above it or one below it — the create would 409. The server's answer stays the authority. */
export function folderOwner(path: string, projects: ProjectEntity[]): ProjectEntity | null {
  if (!path) return null;
  return projects.find((p) => p.folders.some((f) => nested(path, f))) ?? null;
}

/** The first preset no active project uses, else none. */
export function preselectColor(projects: ProjectEntity[]): string {
  const used = new Set(
    projects.filter((p) => !p.archived).map((p) => p.color.toLowerCase()),
  );
  return COLOR_PRESETS.find((c) => !used.has(c.toLowerCase())) ?? "";
}

/** Anything the operator entered that leaving would lose. */
export function isDirty(d: ProjectDraft): boolean {
  return (
    d.name.trim() !== "" ||
    d.folderMode !== "new" ||
    d.existingPath !== "" ||
    d.makeDefault ||
    d.touched.parent ||
    d.touched.folderName ||
    d.touched.color
  );
}
