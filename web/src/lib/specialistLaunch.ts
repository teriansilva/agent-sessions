import type { Specialist, SpecialistList } from "../types/specialists";

/** "Start from a specialist" on the New session form (#1693 S4b, D7). Choosing one preselects its
 *  agent, model and effort; the server re-validates all three and takes a capacity slot, so the
 *  form only ever offers what could start — and says why the rest can't. */

/** What a launch from a specialist names: the version the form saw. */
export interface SpecialistBinding {
  id: string;
  version: number;
  title: string;
  engine: string;
  model: string;
  effort: string | null;
  skills: string[];
}

export interface SpecialistOption {
  id: string;
  text: string;
  /** Why it can't be chosen now, or null. */
  blocked: string | null;
  spec: Specialist;
}

/** One option per specialist that isn't archived: listed even when it can't start (at capacity, its
 *  agent can't start a session here, or its agent is a chat agent — a chat conversation carries no
 *  specialist binding), so the pool reads the same as on the Library page.
 *  `hosts`: the agents whose launch can carry a specialist (terminal and API clients). */
export function specialistOptions(
  list: SpecialistList | null,
  startable: readonly string[],
  hosts: readonly string[] = startable,
): SpecialistOption[] {
  if (!list || !Array.isArray(list.specialists)) return [];
  const poolFull =
    typeof list.max_instances === "number" &&
    typeof list.live_total === "number" &&
    list.live_total >= list.max_instances;
  return list.specialists
    .filter((s) => !s.archived)
    .map((s) => {
      const v = s.current;
      const blocked = !startable.includes(v.engine)
        ? "its agent can’t start here"
        : !hosts.includes(v.engine)
          ? "a chat agent can’t run a specialist"
          : s.live >= v.capacity
          ? "at capacity"
          : poolFull
            ? "the pool is full"
            : null;
      const base = `${v.title} · ${v.engine} · ${s.live}/${v.capacity} running`;
      return { id: s.id, text: blocked ? `${base} · ${blocked}` : base, blocked, spec: s };
    });
}

/** A choice carried through the New project detour, honoured only as it was: the same version, and
 *  still startable. Anything else is null (the form says the choice was dropped). */
export function restoreBinding(
  options: readonly SpecialistOption[],
  saved: { id: string; version: number },
): SpecialistBinding | null {
  const option = options.find((o) => o.id === saved.id);
  if (!option || option.blocked || option.spec.current.version !== saved.version) return null;
  return bindingOf(option.spec);
}

export function bindingOf(s: Specialist): SpecialistBinding {
  const v = s.current;
  return {
    id: s.id,
    version: v.version,
    title: v.title,
    engine: v.engine,
    model: v.model,
    effort: v.effort,
    skills: v.skills.map((k) => k.id),
  };
}

/** What binds, said before Start. */
export function bindingNote(b: SpecialistBinding): string {
  const what = b.skills.length
    ? `its skills (${b.skills.join(", ")}) load into this session, read-only, with its notes for this project`
    : "its notes for this project load into this session";
  return `${b.title} v${b.version}: ${what}. Changing the agent or model below starts a plain session instead.`;
}

/** The binding holds only while the form still shows the specialist's own agent, model and
 *  effort; any change makes the launch a plain session. */
export function bindingHolds(
  b: SpecialistBinding,
  form: { engine: string; model: string; effort: string; isApi: boolean },
): boolean {
  return (
    form.engine === b.engine &&
    form.model === b.model &&
    (!form.isApi || form.effort === (b.effort ?? "default"))
  );
}
