/** Library → Actions (#2248 P1): `GET /api/actions`, shaped like `actions/registry.gallery()`.
 *  A manifest is data only; nothing here runs anything. */

/** How far an action's effect reaches, least to most. A CLASS, never a state. */
export type ActionEffect = "read" | "notify" | "write-battlelab" | "write-external" | "run-local";

export interface ActionInput {
  name: string;
  type: string;
  label: string;
  required: boolean;
  choices?: string[];
  many?: boolean;
}

export interface ActionOutput {
  name: string;
  type: string;
  label: string;
}

/** A requirement and whether it holds now. `ok: null` = not checkable here (it depends on a choice
 *  made in the wizard: which project, session or reviewer), never "missing". */
export interface ActionRequirement {
  id: string;
  words: string;
  ok: boolean | null;
  detail: string;
  /** Where it is set up: a Settings section id (`settingsTabs.ts`), when there is one. */
  setup?: string;
}

export interface ActionEntry {
  id: string;
  label: string;
  category: string;
  description: string;
  effect: ActionEffect;
  executor: string;
  triggers: string[];
  inputs: ActionInput[];
  outputs: ActionOutput[];
  requires: ActionRequirement[];
  available: boolean;
  /** How many automations use it; `null` when the automations could not be read. */
  used_by: number | null;
}

export interface ActionGallery {
  actions: ActionEntry[];
  categories: { id: string; label: string }[];
  effects: { id: ActionEffect; level: number; label: string }[];
}
