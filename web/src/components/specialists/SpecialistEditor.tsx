/** Create or edit a specialist (#1693). Saving an existing one makes a new version (D5); missions
 *  that pinned the old one keep it. The grade is shown, derived from the model through the
 *  operator's map — never chosen here. An agent that cannot carry the skills tool (no MCP channel)
 *  cannot hold a specialist with skills, and says so before Save. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  eligibleFor,
  engineName,
  offeredModels,
  useEngineRoster,
} from "../../app/engineRoster";
import { api } from "../../lib/api";
import { SPECIALISTS_PATH, specialistPath } from "../../lib/routes";
import { agentPath } from "../../routes/settingsTabs";
import type { StructuredModel } from "../../types/api";
import type {
  SpecialistDetail,
  SpecialistDraft,
  SpecialistTemplate,
} from "../../types/specialists";
import { GRADE_HINT, GRADE_LABEL, gradeOf, useModelGrades } from "./grades";
import buttons from "../ui/actionButton.module.css";
import pb from "../playbooks/playbooks.module.css";
import styles from "./specialists.module.css";

const EMPTY: SpecialistDraft = {
  title: "",
  summary: "",
  skills: [],
  engine: "",
  model: "default",
  effort: null,
  capacity: 2,
};

/** The editor's state from a server record: what is current, and the revision the next write is
 *  fenced on. Every response is adopted, so a second save never sends a stale revision. */
function useAdopt(
  setDraft: (d: SpecialistDraft) => void,
  setRevision: (r: number) => void,
  setVersion: (v: number) => void,
  setArchived: (a: boolean) => void,
  setCapacityText: (t: string) => void,
) {
  return useCallback(
    (s: SpecialistDetail) => {
      const { version: v, created_at: _c, ...rest } = s.current;
      void _c;
      setDraft(rest);
      setRevision(s.revision);
      setVersion(v);
      setArchived(s.archived);
      setCapacityText(String(rest.capacity));
    },
    [setDraft, setRevision, setVersion, setArchived, setCapacityText],
  );
}

export function SpecialistEditor({ id }: { id?: string }) {
  const roster = useEngineRoster();
  const grades = useModelGrades();
  const navigate = useNavigate();
  const [draft, setDraft] = useState<SpecialistDraft>(EMPTY);
  const [revision, setRevision] = useState<number | null>(null);
  const [version, setVersion] = useState<number | null>(null);
  const [archived, setArchived] = useState(false);
  // The capacity field's own text: an empty field mid-edit is not "1".
  const [capacityText, setCapacityText] = useState(String(EMPTY.capacity));
  const [loadError, setLoadError] = useState("");
  const [templates, setTemplates] = useState<SpecialistTemplate[]>([]);
  const [library, setLibrary] = useState<
    { id: string; revision: string; label: string }[]
  >([]);
  // Keyed by the engine they were read for, so a stale list is never shown for another engine.
  const [apiModels, setApiModels] = useState<{
    engine: string;
    models: StructuredModel[];
  } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const adopt = useAdopt(setDraft, setRevision, setVersion, setArchived, setCapacityText);

  useEffect(() => {
    void api
      .specialists()
      .then((l) => setTemplates(l.templates))
      .catch(() => {});
    void api
      .localSkills()
      .then((l) =>
        setLibrary(
          l.skills
            .filter((s) => !s.error)
            .map((s) => ({
              id: s.id,
              revision: s.revision,
              label:
                /^name:\s*(.+)$/m
                  .exec(s.files["SKILL.md"] ?? "")?.[1]
                  ?.trim() || s.id,
            })),
        ),
      )
      .catch(() => {});
    if (id)
      void api
        .specialist(id)
        .then(adopt)
        .catch((e: unknown) =>
          setLoadError(e instanceof Error ? e.message : "Couldn’t load this specialist."),
        );
  }, [id, adopt]);

  // A template prefills the job, never the agent or model: those are the operator's.
  const [templateId, setTemplateId] = useState<string | null>(null);
  const applyTemplate = (t: SpecialistTemplate) => {
    setTemplateId(t.id);
    setDraft((d) => ({
      ...d,
      title: t.title,
      summary: t.summary,
      skills: t.skills
        .map((sid) => library.find((l) => l.id === sid))
        .filter(
          (l): l is { id: string; revision: string; label: string } => !!l,
        )
        .map((l) => ({ id: l.id, revision: l.revision })),
    }));
  };

  // Only agents that can start a NEW session here (the roster's mirror of the server's
  // `can_start`): a retiring or unavailable one would always end in a refused save.
  const engines = roster.engines.filter((e) => e.kind === "agent" && eligibleFor(e, "new"));
  const engine = roster.engines.find((e) => e.id === draft.engine);
  const isApi = engine?.runtime === "api";
  const canLoadSkills = !!engine?.tools?.mcp;
  // Which agents CAN load skills is asked of the roster, never listed here.
  const capable = engines
    .filter((e) => e.tools?.mcp)
    .map((e) => engineName(e.id));
  useEffect(() => {
    if (!isApi || !draft.engine) return;
    const engineId = draft.engine;
    void api
      .structuredModels(engineId)
      .then((l) => setApiModels({ engine: engineId, models: l.models }))
      .catch(() => setApiModels({ engine: engineId, models: [] }));
  }, [isApi, draft.engine]);
  const apiList = apiModels?.engine === draft.engine ? apiModels.models : [];

  const models = isApi
    ? apiList.map((m) => ({ id: m.id, label: m.label }))
    : offeredModels(draft.engine).map((m) => ({ id: m.id, label: m.id }));
  const efforts = isApi
    ? (apiList.find((m) => m.id === draft.model)?.efforts ?? [])
    : [];
  const grade = gradeOf(grades, draft.engine, draft.model);
  const skillsRefused = draft.skills.length > 0 && !!engine && !canLoadSkills;
  const problems = useMemo(
    () =>
      [
        !draft.title.trim() && "Give the job a title.",
        !draft.engine && "Choose the agent it runs on.",
        !/^[1-8]$/.test(capacityText.trim()) && "Capacity is a whole number from 1 to 8.",
        skillsRefused &&
          `${engineName(draft.engine)} cannot load skills: remove the skills, or choose an agent that can.`,
      ].filter(Boolean) as string[],
    [draft, skillsRefused, capacityText],
  );
  const set = (patch: Partial<SpecialistDraft>) =>
    setDraft((d) => ({ ...d, ...patch }));

  const save = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      // Saving pins each skill at the library's CURRENT revision (the server refuses any other).
      const sent = {
        ...draft,
        skills: draft.skills.map((sk) => ({
          id: sk.id,
          revision:
            library.find((l) => l.id === sk.id)?.revision ?? sk.revision,
        })),
      };
      if (id) {
        if (revision === null) return; // never loaded: an edit is never turned into a create
        adopt(await api.updateSpecialist(id, revision, sent));
      } else {
        navigate(specialistPath((await api.createSpecialist(sent)).id));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }, [id, revision, draft, library, navigate, adopt]);

  const toggleArchived = useCallback(async () => {
    if (!id || revision === null) return;
    setBusy(true);
    setError("");
    try {
      // Archiving changes the record's state, not the job: only the revision and the flag are
      // taken from the response, so unsaved edits in the form survive it.
      const out = await api.archiveSpecialist(id, revision, !archived);
      setRevision(out.revision);
      setArchived(out.archived);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change it.");
    } finally {
      setBusy(false);
    }
  }, [id, revision, archived]);
  // Editing an id whose record never loaded (or is archived) can't save.
  const blocked = !!id && (revision === null || archived);

  return (
    <div className={styles.editor} data-testid="specialist-editor">
      <div className={`nj-kicker ${pb.kicker}`}>
        <span className="nj-notch" aria-hidden="true" />
        {/* One flow item: the kicker is a flex row, and loose text nodes would wrap apart. */}
        <span>
          Library // <Link to={SPECIALISTS_PATH}>specialists</Link> //{" "}
          <b>{id ? `${draft.title || id}${version ? ` · v${version}` : ""}` : "new"}</b>
        </span>
      </div>
      <h1>{id ? "Edit specialist" : "New specialist"}</h1>
      {loadError && (
        <p className={pb.error} role="alert" data-testid="specialist-load-error">
          {loadError} Nothing can be saved until it loads.
        </p>
      )}
      {id && archived && (
        <p className={styles.refused} data-testid="specialist-archived">
          Archived: hidden from new use. Its versions stay readable. Unarchive it to edit.
        </p>
      )}
      {id && version !== null && !archived && (
        <p className={styles.hint}>
          Saving makes version {version + 1}. Missions approved with v{version} keep it.
        </p>
      )}
      {/* Locked while a write is in flight (its response replaces the draft) and until an edited
          specialist has loaded (the load would replace anything typed before it). */}
      <fieldset className={styles.fieldset} disabled={busy || (!!id && revision === null)}>
      {!id && templates.length > 0 && (
        <div
          className={styles.templates}
          role="group"
          aria-label="Start from a template"
        >
          {templates.map((t) => (
            <button
              type="button"
              key={t.id}
              onClick={() => applyTemplate(t)}
              aria-pressed={t.id === templateId}
              className={
                t.id === templateId ? styles.templateOn : styles.template
              }
            >
              <b>{t.title}</b>
              <span>
                {GRADE_LABEL[t.grade]} · {t.summary}
              </span>
            </button>
          ))}
        </div>
      )}

      <section className={styles.section} aria-labelledby="sp-job">
        <h2 id="sp-job">The job</h2>
        <label className={styles.field}>
          Title
          <input
            value={draft.title}
            onChange={(e) => set({ title: e.target.value })}
          />
        </label>
        <label className={styles.field}>
          What it does
          <textarea
            rows={2}
            value={draft.summary}
            onChange={(e) => set({ summary: e.target.value })}
          />
        </label>
      </section>

      <section className={styles.section} aria-labelledby="sp-runs">
        <h2 id="sp-runs">What it runs on</h2>
        <div className={styles.row}>
          <label className={styles.field}>
            Agent
            <select
              value={draft.engine}
              onChange={(e) =>
                set({ engine: e.target.value, model: "default", effort: null })
              }
            >
              <option value="">Choose…</option>
              {engines.map((e) => (
                <option key={e.id} value={e.id}>
                  {engineName(e.id)}
                  {e.tools?.mcp ? "" : " · cannot load skills"}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            Model
            <select
              value={draft.model}
              onChange={(e) => set({ model: e.target.value, effort: null })}
              disabled={!draft.engine}
            >
              <option value="default">Agent default</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          {isApi && (
            <label className={styles.field}>
              Effort
              <select
                value={draft.effort ?? ""}
                onChange={(e) => set({ effort: e.target.value || null })}
                disabled={!efforts.length}
              >
                <option value="">Model default</option>
                {efforts.map((x) => (
                  <option key={x} value={x}>
                    {x}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <div className={styles.gradeLine} data-testid="specialist-grade-line">
          <span className={grade ? styles.grade : styles.ungraded}>
            {grade ? GRADE_LABEL[grade] : "Ungraded"}
          </span>
          {grade ? (
            <span>{GRADE_HINT[grade]} From your model grades.</span>
          ) : (
            <span>
              {draft.engine && draft.model !== "default" ? (
                <>
                  This model has no grade yet: a role that needs a grade cannot
                  use this specialist.{" "}
                  <Link to={agentPath(draft.engine)}>
                    Map it in Settings → Agents
                  </Link>
                  .
                </>
              ) : (
                "Choose a model to see its grade. “Agent default” is never graded."
              )}
            </span>
          )}
        </div>
      </section>

      <section className={styles.section} aria-labelledby="sp-skills">
        <h2 id="sp-skills">Skills</h2>
        <p className={styles.hint}>
          Loaded into each session of this specialist, read-only, at the version
          saved here.
        </p>
        <ul className={styles.skillList}>
          {draft.skills.map((s) => {
            const lib = library.find((l) => l.id === s.id);
            const outdated = !!lib && lib.revision !== s.revision;
            return (
              <li
                key={s.id}
                className={lib && !outdated ? styles.skill : styles.skillMissing}
                data-testid={`specialist-skill-${s.id}`}
              >
                <span>
                  <code>{s.id}</code> ·{" "}
                  {!lib
                    ? "Not in your skills library"
                    : outdated
                      ? `${lib.label}: changed in your library, saving pins the new version`
                      : lib.label}
                </span>
                <button
                  type="button"
                  className={buttons.ghost}
                  aria-label={`Remove skill ${s.id}`}
                  onClick={() =>
                    set({ skills: draft.skills.filter((x) => x.id !== s.id) })
                  }
                >
                  Remove
                </button>
              </li>
            );
          })}
        </ul>
        {skillsRefused && (
          <p
            className={styles.refused}
            role="alert"
            data-testid="skills-refused"
          >
            {engineName(draft.engine)} cannot load skills: it has no tool
            channel. Remove the skills
            {capable.length > 0 ? `, or choose ${capable.join(", ")}` : ""}.
          </p>
        )}
        <label className={styles.field}>
          Add a skill
          <select
            value=""
            onChange={(e) => {
              const lib = library.find((l) => l.id === e.target.value);
              if (lib)
                set({
                  skills: [
                    ...draft.skills,
                    { id: lib.id, revision: lib.revision },
                  ],
                });
            }}
          >
            <option value="">Choose…</option>
            {library
              .filter((l) => !draft.skills.some((s) => s.id === l.id))
              .map((l) => (
                <option key={l.id} value={l.id}>
                  {l.label} ({l.id})
                </option>
              ))}
          </select>
        </label>
      </section>

      <section className={styles.section} aria-labelledby="sp-cap">
        <h2 id="sp-cap">Capacity</h2>
        <label className={styles.field}>
          At most this many at once
          <input
            type="number"
            min={1}
            max={8}
            value={capacityText}
            onChange={(e) => {
              setCapacityText(e.target.value);
              const n = Number(e.target.value);
              if (Number.isInteger(n)) set({ capacity: n });
            }}
          />
        </label>
      </section>
      </fieldset>

      {problems.length > 0 && (
        <ul className={styles.problems} data-testid="specialist-problems">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {error && (
        <p className={pb.error} role="alert">
          {error}
        </p>
      )}
      <div className={styles.actions}>
        <Link className={buttons.ghost} to={SPECIALISTS_PATH}>
          {id ? "Back" : "Cancel"}
        </Link>
        {id && revision !== null && (
          <button
            type="button"
            className={buttons.ghost}
            disabled={busy}
            onClick={() => void toggleArchived()}
          >
            {archived ? "Unarchive" : "Archive"}
          </button>
        )}
        <button
          type="button"
          className={buttons.primary}
          disabled={busy || blocked || problems.length > 0}
          onClick={() => void save()}
        >
          {id ? `Save as v${(version ?? 0) + 1}` : "Create specialist"}
        </button>
      </div>
    </div>
  );
}
