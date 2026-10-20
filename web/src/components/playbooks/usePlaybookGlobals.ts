import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import type { TemplateVariable } from "../../types/api";
import { missingRequired, type Globals } from "./playbookInputState";
import type { useProjectPlaybook } from "./useProjectPlaybook";

/** The global variables, read once a playbook is selected: the inputs step labels what "Global"
 *  and "Default" resolve to from them. `null` until read, and on a failed read — the wizard then
 *  stops guessing and leaves those cases to the server's review. Names and text values only; a
 *  secret's value never crosses the wire (`set` / `needs_reentry`). */
export function usePlaybookGlobals(enabled: boolean): Globals {
  const [globals, setGlobals] = useState<Globals>(null);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    api
      .templateVariables()
      .then((r) => {
        if (live)
          setGlobals(new Map(r.variables.map((v: TemplateVariable) => [v.name, v])));
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [enabled]);
  return globals;
}

/** The names of the required inputs the wizard knows are missing — what holds Next. */
export function playbookMissing(book: ReturnType<typeof useProjectPlaybook>, globals: Globals): string[] {
  return missingRequired(book.variables, book.choices, globals, book.projectExists);
}
