/** What an imported specialist runs on, settled in the import preview (#1714 P2, board B).
 *
 *  The file only SUGGESTS an agent, model and effort. The server checked that suggestion against
 *  this install the way a save is checked (`settled`, or why it doesn't hold); here the operator
 *  keeps it or chooses another. The options are this install's own: agents that can start a new
 *  session (and, when the specialist pins skills, can load them), the models each offers, and an
 *  API client's declared efforts. The grade is derived from this install's model grades. The
 *  server checks the choice again at confirm, before anything is created. */
import { useEffect, useId, useRef, useState } from "react";
import { eligibleFor, engineName, offeredModels, useEngineRoster } from "../../app/engineRoster";
import { api } from "../../lib/api";
import type { StructuredModel } from "../../types/api";
import type { ShareChoice, SharePreviewItem } from "../../types/share";
import { GRADE_LABEL, gradeOf, useModelGrades } from "../specialists/grades";
import { choiceProblem } from "./shareRules";
import styles from "./share.module.css";

const same = (a: ShareChoice | null | undefined, b: ShareChoice) =>
  !!a && a.engine === b.engine && a.model === b.model && (a.effort ?? null) === (b.effort ?? null);

export function SettleFields({
  item,
  choice,
  onChange,
  disabled,
  problem,
  focusProblem = false,
}: {
  item: SharePreviewItem;
  choice: ShareChoice;
  onChange: (next: ShareChoice) => void;
  disabled: boolean;
  /** The server's word on this choice (a 422 `settle`), shown until it changes. */
  problem?: string;
  /** The first row the server refused: focus moves to its agent field, so the fix is in reach. */
  focusProblem?: boolean;
}) {
  const first = useRef<HTMLSelectElement>(null);
  useEffect(() => {
    if (problem && focusProblem) first.current?.focus();
  }, [problem, focusProblem]);
  const roster = useEngineRoster();
  const grades = useModelGrades();
  const uid = useId();
  const view = item.specialist!;
  const hasSkills = view.skills.length > 0;
  const engines = roster.engines.filter((e) => e.kind === "agent" && eligibleFor(e, "new"));
  const engine = roster.engines.find((e) => e.id === choice.engine);
  const isApi = engine?.runtime === "api";
  // Keyed by the engine they were read for, so a stale list is never shown for another engine.
  const [apiModels, setApiModels] = useState<{ engine: string; models: StructuredModel[] } | null>(null);
  useEffect(() => {
    if (!isApi || !choice.engine) return;
    const id = choice.engine;
    let live = true;
    Promise.resolve()
      .then(() => api.structuredModels(id))
      .then((l) => live && setApiModels({ engine: id, models: l.models }))
      .catch(() => live && setApiModels({ engine: id, models: [] }));
    return () => {
      live = false;
    };
  }, [isApi, choice.engine]);
  const apiList = apiModels?.engine === choice.engine ? apiModels.models : [];
  const models = isApi
    ? apiList.map((m) => ({ id: m.id, label: m.label }))
    : offeredModels(choice.engine).map((m) => ({ id: m.id, label: m.id }));
  // A chosen model the list doesn't hold (the suggestion, before an API list arrives) stays shown.
  if (choice.model !== "default" && !models.some((m) => m.id === choice.model))
    models.unshift({ id: choice.model, label: choice.model });
  const efforts = isApi ? [...(apiList.find((m) => m.id === choice.model)?.efforts ?? [])] : [];
  if (choice.effort && !efforts.includes(choice.effort)) efforts.unshift(choice.effort);
  const grade = gradeOf(grades, choice.engine, choice.model);
  const local = choiceProblem(item, choice, (id) => {
    const e = roster.engines.find((x) => x.id === id);
    return e && { starts: e.kind === "agent" && eligibleFor(e, "new"), skills: !!e.tools?.mcp };
  });
  const hint = view.suggests;
  const suggested = [hint.engine, hint.model, hint.effort].filter(Boolean).join(" · ");
  const note = same(view.settled, choice)
    ? { tone: styles.ok, text: "The suggested agent and model work here." }
    : !choice.engine
      ? { tone: styles.warn, text: view.settle_reason ?? "Choose the agent it runs on." }
      : { tone: "", text: suggested ? `You chose ${engineName(choice.engine)}. The file suggested ${suggested}.` : `You chose ${engineName(choice.engine)}.` };
  const set = (patch: Partial<ShareChoice>) => onChange({ ...choice, ...patch });
  return (
    <div className={styles.runs} data-testid={`share-settle-${item.key}`}>
      <span className={styles.runsHead}>
        Runs on <span>// chosen here; the file only suggests</span>
      </span>
      <span className={`${styles.why} ${note.tone}`}>{note.text}</span>
      <div className={styles.fields}>
        <label className={styles.field} htmlFor={`${uid}-engine`}>
          Agent
          <select
            ref={first}
            className={styles.select}
            id={`${uid}-engine`}
            value={choice.engine}
            disabled={disabled}
            aria-invalid={!choice.engine || !!problem || !!local}
            aria-label={`Agent for ${item.title}`}
            onChange={(e) => set({ engine: e.target.value, model: "default", effort: null })}
          >
            <option value="">Choose…</option>
            {/* The suggestion stays visible even when it can't start here, so the choice is clear. */}
            {choice.engine && !engines.some((e) => e.id === choice.engine) && (
              <option value={choice.engine} disabled>
                {engineName(choice.engine)} · not available here
              </option>
            )}
            {engines.map((e) => (
              <option key={e.id} value={e.id} disabled={hasSkills && !e.tools?.mcp}>
                {engineName(e.id)}
                {hasSkills && !e.tools?.mcp ? " · cannot load skills" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field} htmlFor={`${uid}-model`}>
          Model
          <select
            className={styles.select}
            id={`${uid}-model`}
            value={choice.model}
            disabled={disabled || !choice.engine}
            aria-label={`Model for ${item.title}`}
            onChange={(e) => set({ model: e.target.value, effort: null })}
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
          <label className={styles.field} htmlFor={`${uid}-effort`}>
            Effort
            <select
              className={styles.select}
              id={`${uid}-effort`}
              value={choice.effort ?? ""}
              disabled={disabled || !efforts.length}
              aria-label={`Effort for ${item.title}`}
              onChange={(e) => set({ effort: e.target.value || null })}
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
      <span className={styles.gradeLine} data-testid={`share-grade-${item.key}`}>
        <span className={grade ? styles.grade : styles.ungraded}>{grade ? GRADE_LABEL[grade] : "Ungraded"}</span>
        <span>
          {grade
            ? "From your model grades."
            : choice.engine && choice.model !== "default"
              ? "This model has no grade yet: map it in Settings → Agents."
              : "Choose a model to see its grade. “Agent default” is never graded."}
        </span>
      </span>
      {(problem || (local && choice.engine)) && (
        <span className={`${styles.why} ${styles.down}`} role="alert">
          {problem ?? local}
        </span>
      )}
    </div>
  );
}
