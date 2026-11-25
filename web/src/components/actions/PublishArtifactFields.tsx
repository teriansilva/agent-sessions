/** "Publish an artifact" in the wizard's What step (#2248 P2): a project, a file or folder inside
 *  its folder (a relative path, checked by the server at save and before every run), and the
 *  artifact's title and why line — each fixed or, on a webhook from the list, a short text from the
 *  delivery. A delivery never chooses the project or the path. */
import { MAPPED_TYPE_WORDS, type EditorForm, type MappingDraft } from "../../lib/automations";
import type { ShapeField } from "../../types/webhooks";
import styles from "../automations/automations.module.css";
import { Seg } from "../automations/wizard/bits";
import w from "../automations/wizard/wizard.module.css";

export function PublishArtifactFields({
  form,
  update,
  f,
  listHook,
  shape,
  projects,
}: {
  form: EditorForm;
  update: (fn: (f: EditorForm) => EditorForm) => void;
  f: (name: string) => string;
  listHook: boolean;
  shape: ShapeField[];
  projects: { id: string; name: string; default_folder?: string | null }[];
}) {
  const project = projects.find((p) => p.id === form.projectId);
  return (
    <div className={styles.fields} data-testid="publish-artifact-fields">
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("art-project")}>
          Project
        </label>
        <select
          id={f("art-project")}
          className={styles.control}
          value={form.projectId}
          onChange={(e) => update((x) => ({ ...x, projectId: e.target.value }))}
          data-testid="artifact-project"
        >
          <option value="">Choose a project…</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
              {p.default_folder ? ` · ${p.default_folder}` : ""}
            </option>
          ))}
          {form.projectId && !project && <option value={form.projectId}>{form.projectId}</option>}
        </select>
      </div>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("art-path")}>
          File or folder
        </label>
        <input
          id={f("art-path")}
          className={`${styles.control} ${styles.mono}`}
          value={form.artifactPath}
          placeholder="reports/nightly.md"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          maxLength={1024}
          onChange={(e) => update((x) => ({ ...x, artifactPath: e.target.value }))}
          data-testid="artifact-path"
        />
        <p className={styles.hint}>
          Relative to {project?.default_folder ? <code>{project.default_folder}</code> : "the project’s folder"}. A
          folder is published as a site (its index.html is the page).
        </p>
      </div>
      <LineField
        form={form}
        update={update}
        f={f}
        listHook={listHook}
        shape={shape}
        name="title"
        label="Title"
        value={form.artifactTitle}
        onValue={(v) => update((x) => ({ ...x, artifactTitle: v }))}
        max={120}
      />
      <LineField
        form={form}
        update={update}
        f={f}
        listHook={listHook}
        shape={shape}
        name="why"
        label="Why (one line)"
        value={form.artifactWhy}
        onValue={(v) => update((x) => ({ ...x, artifactWhy: v }))}
        max={300}
      />
      <p className={`${styles.hint} ${styles.full}`} data-testid="publish-artifact-what">
        It is published PRIVATE: only you can open it until you share it. A file with a secret in it,
        a hidden or key file, or a symbolic link refuses the publish. Each run adds a revision behind
        the same link while it is private.
      </p>
    </div>
  );
}

/** One line (the title or the why) that is a fixed value or, on a webhook, a short text from the
 *  delivery. Its mapping lives in `lineMaps[name]`. */
function LineField({
  form,
  update,
  f,
  listHook,
  shape,
  name,
  label,
  value,
  onValue,
  max,
}: {
  form: EditorForm;
  update: (fn: (f: EditorForm) => EditorForm) => void;
  f: (name: string) => string;
  listHook: boolean;
  shape: ShapeField[];
  name: "title" | "why";
  label: string;
  value: string;
  onValue: (v: string) => void;
  max: number;
}) {
  const map = listHook ? form.lineMaps[name] : null;
  const setMap = (m: MappingDraft | null) =>
    update((x) => ({ ...x, lineMaps: { ...x.lineMaps, [name]: m } }));
  return (
    <div className={`${w.valueRow} ${styles.full}`} data-testid={`artifact-${name}`}>
      <div className={w.valueHead}>
        <label className={styles.label} htmlFor={f(`art-${name}`)}>
          {label}
        </label>
        {listHook && (
          <Seg<"fixed" | "delivery">
            label={`Where ${label.toLowerCase()} comes from`}
            value={map ? "delivery" : "fixed"}
            onChange={(v) => setMap(v === "delivery" ? { path: "", type: "text", choices: [] } : null)}
            testId={`artifact-${name}-source`}
            options={[
              { value: "fixed", label: "Fixed value" },
              { value: "delivery", label: "From the delivery" },
            ]}
          />
        )}
      </div>
      {map ? (
        <div className={w.mapFields}>
          <input
            id={f(`art-${name}`)}
            className={`${styles.control} ${styles.mono}`}
            list={f(`art-${name}-shape`)}
            value={map.path}
            maxLength={160}
            placeholder="pull_request.title"
            aria-label={`${label}: field of the delivery`}
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            onChange={(e) => setMap({ ...map, path: e.target.value.trim() })}
            data-testid={`artifact-${name}-path`}
          />
          <span className={styles.fieldSource}>read as {MAPPED_TYPE_WORDS.text.toLowerCase()}</span>
          <datalist id={f(`art-${name}-shape`)}>
            {shape
              .filter((s) => s.type === "text" && !s.path.includes("[]"))
              .map((s) => (
                <option key={s.path} value={s.path} />
              ))}
          </datalist>
        </div>
      ) : (
        <input
          id={f(`art-${name}`)}
          className={styles.control}
          value={value}
          maxLength={max}
          onChange={(e) => onValue(e.target.value)}
          data-testid={`artifact-${name}-value`}
        />
      )}
    </div>
  );
}
