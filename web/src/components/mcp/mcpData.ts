/** Library → MCP servers (#2163 P1c): the pages' data seams — the projects they name, the Variables
 *  secrets a credential can name, storing a NEW credential in Variables (write-only), and binding the
 *  shared credentials to projects. Credentials are Variables secrets: these call the existing
 *  Variables API and never hold a value longer than the one request that stores it. */
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { SharedVariable } from "../../types/variables";
import type { SettingRow } from "./mcpModel";

export const errorText = (e: unknown): string =>
  e instanceof ApiError || e instanceof Error ? e.message : "The request failed. Try again.";

export interface ProjectInfo {
  id: string;
  name: string;
  color: string;
}

/** The live projects (names + colours). `null` while loading; a failed read leaves the list empty
 *  and the pages fall back to the ids — the words still carry the meaning. */
export function useProjects() {
  const [projects, setProjects] = useState<ProjectInfo[] | null>(null);
  useEffect(() => {
    let live = true;
    api
      .projectEntities()
      .then((r) => live && setProjects(r.projects.map((p) => ({ id: p.id, name: p.name, color: p.color }))))
      .catch(() => live && setProjects([]));
    return () => {
      live = false;
    };
  }, []);
  const byId = useMemo(() => new Map((projects ?? []).map((p) => [p.id, p])), [projects]);
  const name = useCallback((id: string) => byId.get(id)?.name ?? id, [byId]);
  const color = useCallback((id: string) => byId.get(id)?.color, [byId]);
  return { projects, name, color };
}

/** The Variables library's secrets — what "Existing variable" picks from. `remember` adds names
 *  this page has just stored, at once: a save that stored a credential and then failed on a later
 *  request can be retried without the stored name reading as missing (Hermes on #2189). */
export function useSecrets() {
  const [secrets, setSecrets] = useState<SharedVariable[] | null>(null);
  const [stored, setStored] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState("");
  const load = useCallback(
    () =>
      api.variables().then(
        (r) => {
          setSecrets(r.variables.filter((v) => v.kind === "secret"));
          setError("");
        },
        (e: unknown) => setError(errorText(e)),
      ),
    [],
  );
  useEffect(() => {
    void load();
  }, [load]);
  const names = useMemo(
    () => new Set([...(secrets ?? []).map((s) => s.name), ...stored]),
    [secrets, stored],
  );
  const remember = useCallback(
    (created: string[]) => {
      if (!created.length) return;
      setStored((prev) => new Set([...prev, ...created]));
      void load(); // and the list itself, for the pickers; a failed read keeps the names above
    },
    [load],
  );
  return { secrets, names, error, reload: load, remember };
}

/** Store every NEW credential row as a Variables secret, one request each; a stored row becomes an
 *  "existing variable" row with its typed value dropped, so a retry never stores it twice. Stops at
 *  the first refusal and says which row. */
export async function storeNewCredentials(
  rows: SettingRow[],
): Promise<{ rows: SettingRow[]; created: string[]; failed?: { rid: string; message: string } }> {
  const out = [...rows];
  const created: string[] = [];
  for (let i = 0; i < out.length; i++) {
    const r = out[i];
    if (r.kind !== "credential" || r.credMode !== "new") continue;
    try {
      await api.createVariable({
        name: r.variable,
        kind: "secret",
        definition: { value_type: "text" },
        value: r.secret,
      });
    } catch (e) {
      return { rows: out, created, failed: { rid: r.rid, message: errorText(e) } };
    }
    out[i] = { ...r, credMode: "existing", secret: "" };
    created.push(r.variable);
  }
  return { rows: out, created };
}

/** Bind the shared (library) secrets to projects — `{variable: [project ids]}` — with the
 *  Variables API's own explicit `ref: global` binding. A binding lets the project RESOLVE the
 *  shared value; it gives agents no access to it (that is the secret's separate agent-access
 *  switch, off unless the operator turned it on). */
export async function bindShared(plan: Record<string, string[]>): Promise<void> {
  const names = Object.keys(plan);
  if (!names.length) return;
  const lib = await api.variables();
  for (const name of names) {
    const rec = lib.variables.find((v) => v.name === name && v.kind === "secret");
    if (!rec?.id) throw new Error(`No shared ${name} in Variables to use.`);
    const projects = [...new Set([...(rec.projects ?? []), ...plan[name]])];
    await api.setVariableProjects(rec.id, projects, rec.updated_at);
  }
}
