/** Format 5 (#1654): an agent step names a ROLE — required skills by exact id and an optional
 *  minimum grade — never an engine or model. The specialist that fills it is chosen per mission
 *  (HQ, from the pool). Format ≤4 steps show their recorded engine/model as a read-only hint and
 *  upgrade only by an explicit, per-reference confirmation: advisory skills are never promoted. */
import { useState } from "react";
import type { AuthoringSchema, Step } from "./playbookDraft";
import { MAX_ROLE_SKILLS, SKILL_ID, defaultRoleSkills } from "./playbookDraft";
import type { UpgradeStep } from "./playbookDraft";
import { Choice } from "./PlaybookStepEditor";
import { engineName } from "../../app/engineRoster";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbookEditor.module.css";

const GRADES = [
  { value: "", label: "Any grade" },
  { value: "fast", label: "Intern" },
  { value: "strong", label: "Specialist" },
  { value: "frontier", label: "Senior" },
] as const;
const GRADE_HINT: Record<string, string> = {
  "": "Any specialist with the skills can take this step.",
  fast: "Fast, low-cost models are enough: sorting, filing, renaming.",
  strong: "Strong models: implementing, matching, writing.",
  frontier: "Frontier models: planning, review, security-sensitive work.",
};

export type Role = { skills: string[]; min_grade?: string };
type LegacySkill = { id: string; revision?: string; required?: boolean };

export function RoleSection({
  step,
  schema,
  onChange,
  field,
}: {
  step: Step & { role?: Role };
  schema: AuthoringSchema & { skills?: { id: string; label: string }[] };
  onChange: (role: Role) => void;
  field: string;
}) {
  const role: Role = step.role ?? { skills: [] };
  const library = schema.skills ?? [];
  const known = (id: string) => library.some((s) => s.id === id);
  const grade = role.min_grade ?? "";
  return (
    <div className={styles.role} data-testid="step-role">
      <p className={styles.hint}>
        The specialist that fills this role is chosen for each mission by HQ,
        from your pool. A step never names an agent or a model.
      </p>
      <fieldset
        className={styles.roleSkills}
        data-field={`${field}.role.skills`}
      >
        <legend>Required skills</legend>
        {schema.skills_unavailable && (
          <p className={styles.warning}>{schema.skills_unavailable}</p>
        )}
        {role.skills.length === 0 && (
          <p className={styles.hint}>
            No skills required: any specialist of the grade below can take it.
          </p>
        )}
        <ul className={styles.skillList}>
          {role.skills.map((id) => (
            <li
              key={id}
              className={known(id) ? styles.skill : styles.skillMissing}
            >
              <span className={styles.skillId}>{id}</span>
              {known(id) ? (
                <span className={styles.skillLabel}>
                  {library.find((s) => s.id === id)?.label}
                </span>
              ) : (
                <span className={styles.warning}>
                  Not in your skills library. Add it there, or remove it here.
                </span>
              )}
              <button
                type="button"
                className={buttons.ghost}
                aria-label={`Remove skill ${id}`}
                onClick={() =>
                  onChange({
                    ...role,
                    skills: role.skills.filter((s) => s !== id),
                  })
                }
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
        {role.skills.length < MAX_ROLE_SKILLS ? (
          <Choice
            label="Add a skill"
            value=""
            choices={library
              .filter((s) => !role.skills.includes(s.id))
              .map((s) => ({ value: s.id, label: `${s.label} (${s.id})` }))}
            onChange={(id) =>
              id && onChange({ ...role, skills: [...role.skills, id] })
            }
            field={`${field}.role.add`}
          />
        ) : (
          <p className={styles.hint}>
            A role lists at most {MAX_ROLE_SKILLS} skills.
          </p>
        )}
      </fieldset>
      <fieldset
        className={styles.grades}
        data-field={`${field}.role.min_grade`}
      >
        <legend>Minimum grade</legend>
        <div
          className={styles.gradeRow}
          role="radiogroup"
          aria-label="Minimum grade"
        >
          {GRADES.map((g) => (
            <button
              key={g.value}
              type="button"
              role="radio"
              aria-checked={grade === g.value}
              className={grade === g.value ? styles.gradeOn : styles.grade}
              onClick={() =>
                onChange({ ...role, min_grade: g.value || undefined })
              }
            >
              {g.label}
            </button>
          ))}
        </div>
        <p className={styles.hint}>{GRADE_HINT[grade]}</p>
      </fieldset>
    </div>
  );
}

/** A format ≤4 agent step: the recorded engine/model is shown read-only, never used to fill a
 *  role. "Upgrade" is explicit and whole-document (`upgradeToRoles`), so the confirm lists EVERY
 *  agent step's references: nothing becomes a required skill that the operator did not see. */
export function LegacyActor({
  step,
  upgradeSteps = [],
  onUpgrade,
}: {
  step: Step & { skills?: LegacySkill[] };
  /** Every agent step the upgrade changes, in document order. */
  upgradeSteps?: UpgradeStep[];
  onUpgrade?: (picks: Record<string, string[]>) => void;
}) {
  const [open, setOpen] = useState(false);
  // References on non-agent steps cannot migrate: the operator acknowledges their removal.
  const dropped = upgradeSteps.filter(
    (s) => !s.agent && (s.step.skills ?? []).length,
  );
  const [acknowledged, setAcknowledged] = useState(false);
  const problem = (r: LegacySkill) =>
    !SKILL_ID.test(r.id)
      ? `Rename “${r.id}” to “${r.id.toLowerCase().replace(/[^a-z0-9-]/g, "-")}” in your library, or leave it out.`
      : r.revision && !/^[0-9a-f]{64}$/.test(r.revision)
        ? `Pinned to version ${r.revision}. Pin a library revision, or leave the pin out.`
        : "";
  const [picked, setPicked] = useState<Record<string, string[]>>(() =>
    Object.fromEntries(
      upgradeSteps
        .filter((s) => s.agent)
        .map((s) => [s.key, defaultRoleSkills(s.step)]),
    ),
  );
  const toggle = (key: string, id: string, on: boolean) =>
    setPicked((all) => ({
      ...all,
      [key]: on
        ? [...(all[key] ?? []), id]
        : (all[key] ?? []).filter((x) => x !== id),
    }));
  return (
    <div className={styles.legacy} data-testid="legacy-actor">
      {step.actor.engine ? (
        <p>
          <strong>Format 4 step.</strong> Recorded agent:{" "}
          <span className={styles.skillId}>
            {engineName(step.actor.engine)}
          </span>{" "}
          · model{" "}
          <span className={styles.skillId}>
            {step.actor.model ?? "default"}
          </span>
          . Shown for reference only; in format 5 a role replaces it.
        </p>
      ) : (
        <p>
          <strong>Format 4 playbook.</strong> An agent step here needs a
          recorded agent. Upgrade the playbook to format 5 to give this step a
          role instead.
        </p>
      )}
      {!onUpgrade ? null : !open ? (
        <button
          type="button"
          className={buttons.ghost}
          onClick={() => setOpen(true)}
        >
          Upgrade this playbook to format 5…
        </button>
      ) : (
        <div
          className={styles.upgrade}
          role="group"
          aria-label="Upgrade to format 5"
        >
          <p className={styles.hint}>
            The upgrade changes every step below. Choose which skill references
            become each agent step's required skills; nothing is added without
            your choice. The current format 4 file is kept, and{" "}
            <em>Revert to format 4</em> restores it byte for byte.
          </p>
          {upgradeSteps.map((s) => {
            const refs: LegacySkill[] = s.step.skills ?? [];
            const where = (
              <legend>
                {s.flow} · {s.step.title || s.step.id}{" "}
                <span className={styles.skillId}>
                  {s.path.slice(6, -5)}:{s.step.id}
                </span>
              </legend>
            );
            if (!s.agent)
              return (
                <fieldset
                  key={s.key}
                  className={styles.upgradeStep}
                  data-testid="upgrade-step"
                >
                  {where}
                  <p className={styles.warning}>
                    Format 5 keeps skills only in agent roles. This step's
                    references are removed:{" "}
                    {refs.map((r, i) => (
                      <span key={r.id}>
                        {i ? ", " : ""}
                        <span className={styles.skillId}>{r.id}</span>
                      </span>
                    ))}
                    .
                  </p>
                </fieldset>
              );
            return (
              <fieldset
                key={s.key}
                className={styles.upgradeStep}
                data-testid="upgrade-step"
              >
                {where}
                {refs.length === 0 && (
                  <p className={styles.hint}>
                    This step lists no skills: its role starts with none.
                  </p>
                )}
                {refs.map((r) => {
                  const blocked = problem(r);
                  return (
                    <label
                      key={r.id}
                      className={blocked ? styles.checkBlocked : styles.check}
                    >
                      <input
                        type="checkbox"
                        disabled={!!blocked}
                        checked={(picked[s.key] ?? []).includes(r.id)}
                        onChange={(e) => toggle(s.key, r.id, e.target.checked)}
                      />
                      <span>
                        <span className={styles.skillId}>{r.id}</span>{" "}
                        {r.required
                          ? "· required in format 4"
                          : "· suggested (advisory)"}
                        {blocked && (
                          <span className={styles.warning}> {blocked}</span>
                        )}
                      </span>
                    </label>
                  );
                })}
              </fieldset>
            );
          })}
          {dropped.length > 0 && (
            <label className={styles.check}>
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              <span>
                Remove the skill references on{" "}
                {dropped.length === 1
                  ? "the step"
                  : `the ${dropped.length} steps`}{" "}
                that are not agent steps.
              </span>
            </label>
          )}
          <div className={styles.upgradeActions}>
            <button
              type="button"
              className={buttons.primary}
              disabled={dropped.length > 0 && !acknowledged}
              onClick={() => onUpgrade(picked)}
            >
              Upgrade to format 5
            </button>
            <button
              type="button"
              className={buttons.ghost}
              onClick={() => setOpen(false)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
