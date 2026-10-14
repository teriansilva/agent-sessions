/** Shared gallery cards and the existing deploy contract inside the New project wizard. */
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  engineName,
  offeredModels,
  useEngineRoster,
} from "../../app/engineRoster";
import { settingsPath } from "../../routes/settingsTabs";
import { ProjectPlaybookRemove } from "./ProjectPlaybookRemove";
import { PlaybookCard } from "./PlaybookCard";
import type { useProjectPlaybook } from "./useProjectPlaybook";
import buttons from "../ui/actionButton.module.css";
import styles from "./ProjectPlaybook.module.css";

export type ProjectPlaybookState = ReturnType<typeof useProjectPlaybook>;
export function ProjectPlaybookGallery({
  book,
}: {
  book: ProjectPlaybookState;
}) {
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("");
  const shown = (book.gallery?.playbooks ?? []).filter(
    (p) =>
      (!source || p.source === source) &&
      [
        p.id,
        p.name,
        p.summary,
        ...(p.flows ?? []).flatMap((f) => f.steps.map((s) => s.title)),
      ]
        .join(" ")
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  return (
    <div className={styles.root}>
      <p>
        Give this project a reviewed set of files, inputs and workflow
        instructions. Assignment does not start agents or run probes.
      </p>
      {book.defaultNotice && (
        <p role="status" className={styles.warning}>
          {book.defaultNotice}
        </p>
      )}
      {book.galleryError && (
        <div role="alert">
          {book.galleryError}{" "}
          <button
            className={buttons.ghost}
            onClick={() => void book.reloadGallery()}
          >
            Reload playbooks
          </button>
        </div>
      )}
      <article className={styles.card} aria-label="No playbook">
        <h2>No playbook</h2>
        <p>
          Create the project without assigning files or workflow instructions.
        </p>
        <button
          className={buttons.ghost}
          aria-pressed={!book.id}
          onClick={() => book.select(null)}
        >
          {!book.id ? "Selected: No playbook" : "Select No playbook"}
        </button>
      </article>
      <div className={styles.fields}>
        <label>
          Search playbooks
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <label>
          Source
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">All sources</option>
            <option value="local">Local</option>
            <option value="bundled">Bundled</option>
            <option value="catalog">Catalog</option>
          </select>
        </label>
      </div>
      {!book.gallery && !book.galleryError && (
        <p role="status">Loading playbooks…</p>
      )}
      {shown.map((card) => (
        <div
          key={`${card.source}:${card.id}`}
          className={card.id === book.id ? styles.selected : undefined}
        >
          <PlaybookCard card={card} onSelect={(p) => book.select(p.id)} />
          {card.id === book.id && (
            <p role="status">Selected: {card.name || card.id}</p>
          )}
        </div>
      ))}
      {book.gallery && !shown.length && (
        <p>No matching playbooks. No playbook remains available.</p>
      )}
      {book.loading && <p role="status">Loading selected playbook…</p>}
      {book.loadError && (
        <p role="alert" className={styles.warning}>
          {book.loadError}
        </p>
      )}
      {book.detail?.source === "local" && !book.loadError && (
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={book.trust}
            onChange={(e) => book.setTrust(e.target.checked)}
          />
          I have reviewed this unsigned local playbook and trust its
          instructions and proposed files.
        </label>
      )}
    </div>
  );
}

export function ProjectPlaybookInputs({
  book,
}: {
  book: ProjectPlaybookState;
}) {
  const roster = useEngineRoster();
  const steps = (book.detail?.flows ?? []).flatMap((f) =>
    f.steps
      .filter((s) => s.actor.kind === "agent")
      .map((s) => ({ ...s, key: `${f.id}:${s.id}`, flow: f.title })),
  );
  return (
    <div className={styles.root}>
      <p>
        Inputs stay in this wizard until you create the project and bind them.
        Secret values are never shown in the review.
      </p>
      {book.variables.map((variable) => {
        const choice = book.choices[variable.name] ?? {
          mode: "inherit" as const,
          value: "",
        };
        const update = (patch: Partial<typeof choice>) =>
          book.setChoices((all) => ({
            ...all,
            [variable.name]: { ...choice, ...patch },
          }));
        const secret = variable.kind === "secret";
        return (
          <fieldset
            key={variable.name}
            className={styles.card}
            disabled={book.busy}
          >
            <legend>
              {variable.label || variable.name}
              {variable.required ? " · required" : ""}
            </legend>
            <p>
              <code>{variable.name}</code> ·{" "}
              {secret ? "secret" : variable.type || "text"}
            </p>
            {variable.help && <p>{variable.help}</p>}
            <label>
              Source for {variable.name}
              <select
                aria-label={`Source for ${variable.name}`}
                value={choice.mode}
                onChange={(e) =>
                  update({
                    mode: e.target.value as typeof choice.mode,
                    value: "",
                  })
                }
              >
                <option value="inherit">
                  {secret
                    ? "Use saved project binding, if any"
                    : "Saved value / default"}
                </option>
                <option value="value">Enter a project value</option>
                <option value="global">
                  Use the global variable with this name
                </option>
              </select>
            </label>
            {choice.mode === "global" && (
              <p>
                The server checks the global variable’s name, kind, value and
                revision before binding.
              </p>
            )}
            {choice.mode === "inherit" &&
              !secret &&
              variable.default != null && (
                <p>
                  Playbook default: <code>{String(variable.default)}</code>
                </p>
              )}
            {choice.mode === "value" && (
              <label>
                Value for {variable.name}
                {!secret &&
                (variable.type === "bool" || variable.type === "enum") ? (
                  <select
                    aria-label={`Value for ${variable.name}`}
                    value={choice.value}
                    onChange={(e) => update({ value: e.target.value })}
                  >
                    <option value="">Choose a value</option>
                    {(variable.type === "bool"
                      ? ["true", "false"]
                      : (variable.choices ?? [])
                    ).map((v) => (
                      <option key={v}>{v}</option>
                    ))}
                  </select>
                ) : secret ? (
                  <input
                    type="password"
                    autoComplete="new-password"
                    aria-label={`Value for ${variable.name}`}
                    value={choice.value}
                    onChange={(e) => update({ value: e.target.value })}
                  />
                ) : (
                  <textarea
                    aria-label={`Value for ${variable.name}`}
                    value={choice.value}
                    onChange={(e) => update({ value: e.target.value })}
                    rows={3}
                  />
                )}
              </label>
            )}
          </fieldset>
        );
      })}
      {!book.variables.length && (
        <p>This playbook declares no variable inputs.</p>
      )}
      <h2>Step assignments</h2>
      <p>
        Assignments are saved configuration. Starting a workflow is a separate
        action, subject to live runtime availability.
      </p>
      {!steps.length && <p>No agent steps need an assignment.</p>}
      {steps.map((step) => {
        const assignment =
          step.key in book.assignments
            ? book.assignments[step.key]
            : step.actor.engine
              ? {
                  engine: step.actor.engine,
                  model: step.actor.model || "default",
                }
              : null;
        const set = (value: { engine: string; model: string } | null) =>
          book.setAssignments((all) => ({ ...all, [step.key]: value }));
        const available = roster.engines.filter(
          (e) =>
            e.present && e.status !== "retiring" && book.agents.includes(e.id),
        );
        const known = available.some((e) => e.id === assignment?.engine);
        const models = assignment ? offeredModels(assignment.engine) : [];
        return (
          <fieldset key={step.key} className={styles.card} disabled={book.busy}>
            <legend>
              {step.flow} · {step.title}
            </legend>
            <label>
              Agent for {step.title}
              <select
                aria-label={`Agent for ${step.title}`}
                value={assignment?.engine || ""}
                onChange={(e) =>
                  set(
                    e.target.value
                      ? { engine: e.target.value, model: "default" }
                      : null,
                  )
                }
              >
                <option value="">Unassigned</option>
                {assignment && !known && (
                  <option value={assignment.engine}>
                    {engineName(assignment.engine)} · unavailable
                  </option>
                )}
                {available.map((engine) => (
                  <option key={engine.id} value={engine.id}>
                    {engineName(engine.id)}
                  </option>
                ))}
              </select>
            </label>
            {assignment && (
              <label>
                Model for {step.title}
                <select
                  aria-label={`Model for ${step.title}`}
                  value={assignment.model}
                  onChange={(e) =>
                    set({ ...assignment, model: e.target.value })
                  }
                >
                  <option value="default">Agent default</option>
                  {assignment.model !== "default" &&
                    !models.some((m) => m.id === assignment.model) && (
                      <option value={assignment.model}>
                        {assignment.model} · unavailable
                      </option>
                    )}
                  {models
                    .filter((m) => m.id !== "default")
                    .map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.id}
                      </option>
                    ))}
                </select>
              </label>
            )}
            {(!known || roster.status !== "ready") && (
              <p className={styles.warning}>
                This assignment is unresolved. The review preserves the reason.{" "}
                <Link to={settingsPath("agents")}>Settings → Agents</Link>
              </p>
            )}
          </fieldset>
        );
      })}
      <h2>Requirements</h2>
      <ul>
        {Object.entries(book.detail?.requires_present?.binaries ?? {}).map(
          ([binary, present]) => (
            <li key={binary}>
              {binary}: {present ? "available" : "missing"}
            </li>
          ),
        )}
      </ul>
    </div>
  );
}

export function ProjectPlaybookReview({
  book,
}: {
  book: ProjectPlaybookState;
}) {
  const plan = book.reviewed;
  return (
    <div className={styles.root}>
      <h2>Playbook changes</h2>
      <p>
        {book.detail?.name || book.id} · no workflow starts during assignment.
      </p>
      {book.error && (
        <p role="alert" className={styles.warning}>
          {book.error}
        </p>
      )}
      <button
        className={buttons.ghost}
        disabled={book.busy}
        onClick={() => void book.review(true)}
      >
        {book.busy
          ? "Reviewing…"
          : plan
            ? "Refresh review"
            : "Review playbook changes"}
      </button>
      {book.reviewingCreatedTarget && <p role="status">
        This wizard already created the folder. Its identity has been checked again.
        Review its current contents and confirm the new plan before creating the project.
      </p>}
      {plan && (
        <>
          <p>
            Review <code className={styles.digest}>{plan.digest}</code>
          </p>
          {!!plan.conflicts.length && (
            <div role="alert" className={styles.warning}>
              <strong>
                Resolve these conflicts before creating the project.
              </strong>
              <ul>
                {plan.conflicts.map((c) => (
                  <li key={c.path}>
                    {c.path}: {c.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <h3>Resolved inputs</h3>
          <dl>
            {plan.variables.map((v) => (
              <div key={v.name}>
                <dt>{v.name}</dt>
                <dd>
                  {v.kind === "secret"
                    ? v.set
                      ? "Secret set · hidden"
                      : "Secret unset"
                    : v.state === "missing"
                      ? "Unset"
                      : String(v.value)}{" "}
                  · {v.source || "no source"}
                  {v.explicit_global ? " · explicit global reference" : ""}
                </dd>
              </div>
            ))}
          </dl>
          <h3>Files</h3>
          {!plan.changes.length && <p>No material changes.</p>}
          {plan.changes.map((change) => (
            <details key={change.path} className={styles.card}>
              <summary>
                {change.action} · {change.path}
              </summary>
              <pre>
                {change.diff ||
                  JSON.stringify(
                    { before: change.before, after: change.after },
                    null,
                    2,
                  )}
              </pre>
            </details>
          ))}
          <h3>Rendered instructions and materials</h3>
          {plan.materials.map((material) => (
            <details key={material.path} className={styles.card}>
              <summary>
                {material.path} · {material.disposition}
              </summary>
              <pre>
                {material.after.text ??
                  material.after.target ??
                  "Binary file; inspect its proposed change above."}
              </pre>
            </details>
          ))}
          <h3>Resolved targets</h3>
          {!plan.targets.length && (
            <p>No connection or completion probe targets.</p>
          )}
          {plan.targets.map((target) => (
            <div key={target.id} className={styles.card}>
              <strong>{target.id}</strong>
              <pre>{JSON.stringify(target.args, null, 2)}</pre>
              {!!Object.keys(target.pending).length && (
                <p>
                  Still unresolved: {Object.keys(target.pending).join(", ")}.
                  Assignment does not probe this target.
                </p>
              )}
              {target.requires_confirmation && (
                <label className={styles.check}>
                  <input
                    type="checkbox"
                    disabled={book.busy}
                    checked={book.confirmed.includes(target.id)}
                    onChange={(e) =>
                      book.setConfirmed((all) =>
                        e.target.checked
                          ? [...all, target.id]
                          : all.filter((id) => id !== target.id),
                      )
                    }
                  />
                  Confirm this target, derived from defaults:{" "}
                  {target.default_variables.join(", ")}
                </label>
              )}
            </div>
          ))}
          <h3>Assignments</h3>
          <ul>
            {plan.assignments.map((a) => (
              <li key={a.step}>
                {a.step}:{" "}
                {a.assignment
                  ? `${engineName(a.assignment.engine)} / ${a.assignment.model}`
                  : `Unassigned — ${a.reason}`}
              </li>
            ))}
          </ul>
          <h3>Capability requests</h3>
          <p>
            {plan.capability_requests.join(", ") || "None"}. Assignment grants
            no additional capabilities.
          </p>
          {!!plan.rituals.length && (
            <p>{plan.rituals.length} rituals declared; none scheduled.</p>
          )}
        </>
      )}
    </div>
  );
}

export function ProjectPlaybookOutcome({
  book,
  project,
}: {
  book: ProjectPlaybookState;
  project: string;
}) {
  const applied = book.status?.state === "applied";
  return (
    <section className={styles.root} aria-label="Playbook assignment result">
      <h2>Playbook assignment</h2>
      <p>
        Project: created.{" "}
        {book.busy
          ? `${book.stage === "bind" ? "Binding inputs" : book.stage === "apply" ? "Applying files" : "Verifying"}…`
          : ""}
      </p>
      <p>
        Deployment:{" "}
        {book.status
          ? book.status.state
          : book.stage === "done"
            ? "applied"
            : "status unavailable"}
        .
      </p>
      {book.status?.files?.map((f) => (
        <p key={f.path}>
          <code>{f.path}</code>: {f.state}
        </p>
      ))}
      {book.error && (
        <p role="alert" className={styles.warning}>
          {book.error}
          {applied
            ? " Files and bindings were applied; verification can be retried."
            : " The project is retained. Retry uses its existing ID and the same operation."}
        </p>
      )}
      {book.verification && (
        <>
          <h3>
            Verification: {book.verification.ok ? "passed" : "needs attention"}
          </h3>
          <ul>
            {Object.entries(book.verification.checks).map(([name, check]) => (
              <li key={name}>
                {name}:{" "}
                {check.ok === true
                  ? "passed"
                  : check.ok === false
                    ? "needs attention"
                    : "reported"}
                {check.error ? ` · ${check.error}` : ""}
                {check.drift?.length
                  ? ` · drift: ${check.drift.join(", ")}`
                  : ""}
                {check.missing?.length
                  ? ` · missing: ${check.missing.join(", ")}`
                  : ""}
              </li>
            ))}
          </ul>
        </>
      )}
      {!book.recovering &&
        !book.busy &&
        (book.error || book.verification?.ok === false) && (
          <button
            className={buttons.primary}
            onClick={() => void book.deploy(project)}
          >
            {applied
              ? "Re-verify"
              : book.stage === "bind"
                ? "Retry binding"
                : "Retry apply"}
          </button>
        )}
      {!book.recovering &&
        book.error &&
        book.stage !== "verify" &&
        book.status &&
        book.status.state !== "interrupted" && (
          <button
            className={buttons.ghost}
            disabled={book.busy}
            onClick={() => void book.recoverReview()}
          >
            Review current changes for this project
          </button>
        )}
      {(book.status?.state === "applied" ||
        book.status?.state === "bound" ||
        (book.status?.state === "interrupted" &&
          book.status.operation === "apply")) && (
        <ProjectPlaybookRemove
          project={project}
          disabled={book.busy}
          onBusy={book.setRemoving}
          onRemoved={book.removed}
        />
      )}
      {book.recovering && (
        <>
          <ProjectPlaybookInputs book={book} />
          <ProjectPlaybookReview book={book} />
          <button
            className={buttons.primary}
            disabled={book.busy || !book.canCreate}
            onClick={async () => {
              if (await book.prepare()) await book.deploy(project);
            }}
          >
            Apply reviewed changes to this project
          </button>
        </>
      )}
    </section>
  );
}
