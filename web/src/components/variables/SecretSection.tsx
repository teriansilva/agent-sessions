import { useId, useRef, useState } from "react";
import { FileKey } from "lucide-react";
import type { SharedVariable } from "../../types/variables";
import type { SecretMode, VariableDraft } from "./variableDraft";
import { AccessLog } from "./AccessLog";
import { formatSize, SECRET_FILE_MAX } from "./secretFile";
import buttons from "../ui/actionButton.module.css";
import styles from "../../routes/Variables.module.css";

const MODES: { mode: SecretMode; label: string }[] = [
  { mode: "single", label: "Single line" },
  { mode: "multi", label: "Multi-line · PEM, key, JSON" },
  { mode: "file", label: "File · upload" },
];

/** A secret's value, file, agent access, bound projects and access log (#1786). Every value
 *  stays write-only: a stored secret or file is never shown, and nothing here downloads one. */
export function SecretSection({ draft, original, editing, disabled, projects, onChange, onFile }: {
  draft: VariableDraft;
  original?: SharedVariable;
  editing: boolean;
  disabled: boolean;
  projects: { id: string; name: string }[];
  onChange: (patch: Partial<VariableDraft>) => void;
  onFile: (file: File | null) => void;
}) {
  const valueId = useId();
  const projectsId = useId();
  const fileInput = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [refusal, setRefusal] = useState("");
  const names = new Map(projects.map(p => [p.id, p.name]));
  const choose = (file: File | undefined) => {
    setRefusal("");
    if (!file) return;
    // A refused choice also drops any file chosen before it: Save never uploads a file the
    // latest choice did not confirm (Hermes on #1803).
    const refuse = (why: string) => { onFile(null); onChange({ fileName: "", fileSize: 0 }); setRefusal(why); };
    if (file.size === 0) { refuse("That file is empty. Nothing is queued to store."); return; }
    if (file.size > SECRET_FILE_MAX) {
      refuse(`This file is ${formatSize(file.size)}; a secret file can be at most ${formatSize(SECRET_FILE_MAX)}. Nothing is queued to store.`);
      return;
    }
    onFile(file);
    onChange({ fileName: file.name, fileSize: file.size });
  };
  // A binding to a project the picker no longer lists (deleted or archived) still counts and must
  // stay removable, so it is offered too, named for what it is. Re-ticking one is refused.
  const unlisted = (original?.projects ?? []).filter(id => !names.has(id));
  const choices = [...projects, ...unlisted.map(id => ({ id, name: `${id} · deleted or archived project` }))];
  const scopeNames = draft.projects.map(id => names.get(id) ?? id);
  const stored = original?.kind === "secret_file" ? original.file : undefined;
  return <div className={styles.valueSection}>
    {!editing && <div className={styles.field} role="group" aria-labelledby={`${valueId}-type`}>
      <span id={`${valueId}-type`}>Secret type</span>
      <div className={styles.seg}>
        {MODES.map(m => <button key={m.mode} type="button" className={styles.segOption} aria-pressed={draft.secretMode === m.mode} disabled={disabled}
          onClick={() => { onFile(null); onChange({ secretMode: m.mode, kind: m.mode === "file" ? "secret_file" : "secret", value: "", fileName: "", fileSize: 0 }); }}>{m.label}</button>)}
      </div>
    </div>}

    {draft.kind === "secret" && <label className={styles.check}><input type="checkbox" disabled={disabled} checked={draft.storeValue} onChange={e => onChange({ storeValue: e.target.checked })} /> Store a global value</label>}
    {draft.kind === "secret" && !draft.storeValue && <p className={styles.help}>{editing && original?.set ? "Saving removes the stored secret and keeps this definition." : "Save the definition without a value; a project binding supplies its own."}</p>}

    {draft.kind === "secret_file" ? <div className={styles.field}>
      <span id={`${valueId}-file`}>File</span>
      <div className={`${styles.drop} ${dragging ? styles.dropActive : ""} ${original?.needs_reentry ? styles.dropBroken : ""}`}
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={e => { e.preventDefault(); setDragging(false); if (!disabled) choose(e.dataTransfer.files?.[0]); }}>
        <FileKey size={16} aria-hidden="true" />
        <div className={styles.dropText}>
          {draft.fileName ? <>
            <div className={styles.fileName}>{draft.fileName}</div>
            <div className={styles.fileMeta}>{formatSize(draft.fileSize)} · ready to store</div>
          </> : stored ? <>
            <div className={styles.fileName}>{stored.name}</div>
            <div className={styles.fileMeta}>{original?.needs_reentry
              ? "Needs re-entry · the stored file could not be decrypted. Agents are refused and model calls pause until you upload it again or delete it."
              : `Stored · hidden · ${formatSize(stored.size)}`}</div>
          </> : <>
            <div className={styles.fileName}>Drop a file here</div>
            <div className={styles.fileMeta}>or choose one · max {formatSize(SECRET_FILE_MAX)} · any type</div>
          </>}
        </div>
        <button type="button" className={buttons.ghost} disabled={disabled} onClick={() => fileInput.current?.click()}>
          {stored || draft.fileName ? (original?.needs_reentry ? "Upload again…" : "Replace file…") : "Choose file…"}
        </button>
        <input ref={fileInput} type="file" hidden aria-labelledby={`${valueId}-file`} onChange={e => { choose(e.target.files?.[0]); e.target.value = ""; }} />
      </div>
      {refusal && <div className={styles.error} role="alert">{refusal}</div>}
      <small>Stored encrypted; it is never shown or downloaded again. A binary file cannot be redacted if an agent prints it.</small>
    </div> : draft.storeValue && <label className={styles.field}>
      <span id={`${valueId}-value`}>{editing ? "Replacement secret" : "Global value"}</span>
      {draft.secretMode === "multi"
        ? <textarea aria-labelledby={`${valueId}-value`} rows={6} spellCheck={false} autoComplete="off" value={draft.value} onChange={e => onChange({ value: e.target.value })} />
        : <input aria-labelledby={`${valueId}-value`} type="password" autoComplete="new-password" value={draft.value} onChange={e => onChange({ value: e.target.value })} />}
      <small>{editing ? "Leave blank to keep the stored secret. The current value is never displayed." : draft.secretMode === "multi" ? "Paste the whole key or document. Leading and trailing blank lines are removed." : "Write-only: it is never displayed again."}</small>
    </label>}

    <div className={styles.access}>
      <button type="button" role="switch" aria-checked={draft.agentAccess} aria-labelledby={`${valueId}-access`} aria-describedby={`${valueId}-access-help`}
        className={styles.switch} disabled={disabled} onClick={() => onChange({ agentAccess: !draft.agentAccess })} />
      <div>
        <b id={`${valueId}-access`} className={styles.accessTitle}>Agent access · {draft.agentAccess ? "on" : "off"}</b>
        <p id={`${valueId}-access-help`} className={styles.help}>
          {draft.agentAccess
            ? scopeNames.length
              ? <>Agents in {scopeNames.join(", ")} can ask the secrets tool for a private copy of this secret (a file path, not the content). Turning this off refuses every later request at once and deletes the copies BattleLab placed. It cannot recall a copy an agent already read, copied or sent.</>
              : <>No project binds this secret yet, so no agent can request it. Choose projects below.</>
            : <>No agent can request this secret. Turn on to let agents in the projects that bind it request a private copy.</>}
        </p>
        {draft.agentAccess && <div className={styles.warn}>An agent that runs with permission bypass can read the copy and send it anywhere. Give access only to secrets agents need.</div>}
      </div>
    </div>

    {choices.length > 0 && <fieldset className={styles.projects} aria-labelledby={projectsId}>
      <legend id={projectsId}>Projects that can use it</legend>
      {choices.map(p => <label key={p.id} className={styles.check}>
        <input type="checkbox" disabled={disabled} checked={draft.projects.includes(p.id)}
          onChange={e => onChange({ projects: e.target.checked ? [...draft.projects, p.id].sort() : draft.projects.filter(id => id !== p.id) })} /> {p.name}
      </label>)}
    </fieldset>}

    {editing && original?.id && <AccessLog vid={original.id} projectNames={names} />}
  </div>;
}
