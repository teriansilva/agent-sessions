import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  Copy,
  FileText,
  Plus,
} from "lucide-react";
import { api } from "../lib/api";
import { GALLERY_SKILLS_PATH, SKILLS_PATH } from "../lib/routes";
import { LandingNote, ViewTabs } from "../components/gallery/ViewTabs";
import type { LocalSkill } from "../types/gallery";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { FileRow, LeaveGuard, Origin, PendingOperation } from "../components/gallery/Shared";
import { beginOperation, finishOperation, errorText, ownValue } from "../components/gallery/operations";
import buttons from "../components/ui/actionButton.module.css";
import styles from "../components/gallery/gallery.module.css";

export default function SkillsLibrary() {
  const { skillId } = useParams();
  return (
    <main className={styles.page}>
      <div className={`nj-kicker ${styles.kicker}`}><span className="nj-notch" aria-hidden="true" />Library // <b>Skills</b></div>
      {!skillId && (
        <>
          <div className={styles.header}>
            <h1>Skills</h1>
          </div>
          <ViewTabs kind="skill" view="local" />
          <p className={styles.intro}>
            Editable instruction files kept in your BattleLab library. Saving here
            does not activate a skill in an agent.
          </p>
        </>
      )}
      <LandingNote />
      <PendingOperation />
      {skillId ? <Detail key={skillId} id={skillId} /> : <List />}
    </main>
  );
}
function List() {
  const read = usePlaybookRead(useCallback(() => api.localSkills(), []));
  return (
    <>
      {read.error && (
        <p role="alert" className={styles.error}>
          {read.error}
        </p>
      )}
      {read.loading && !read.data && <p role="status">Loading local skills…</p>}
      {read.data && !read.data.skills.length && (
        <section className={styles.empty}>
          <h2>No skills yet</h2>
          <p>
            Skills you add from the <Link to={GALLERY_SKILLS_PATH}>gallery</Link> appear
            here.
          </p>
        </section>
      )}
      <div className={`${styles.grid} ${styles.listTop}`}>
        {read.data?.skills.map((skill) => (
          <article className={styles.card} key={skill.id}>
            <div className={`${styles.tag} ${styles.stamp}`}>
              <FileText size={14} aria-hidden />
              Skill
              <span className={styles.stampId}>
                {skill.gallery_origin ? `v${skill.gallery_origin.version}` : "local"}
              </span>
            </div>
            <h2>{skill.gallery_origin?.title ?? skill.id}</h2>
            <p>
              <code>{skill.id}</code>
            </p>
            {skill.error ? (
              <p className={styles.error}>{skill.error}</p>
            ) : (
              <Link
                className={`${buttons.ghost} ${styles.cardAction}`}
                to={`${SKILLS_PATH}/${encodeURIComponent(skill.id)}`}
              >
                Open skill
                <ArrowRight size={16} aria-hidden />
              </Link>
            )}
          </article>
        ))}
      </div>
    </>
  );
}
function Detail({ id }: { id: string }) {
  const read = usePlaybookRead(useCallback(() => api.localSkill(id), [id]));
  if (!read.data)
    return (
      <p
        role={read.error ? "alert" : "status"}
        className={read.error ? styles.error : undefined}
      >
        {read.error ?? "Loading skill…"}
      </p>
    );
  return <Editor initial={read.data} reload={read.reload} />;
}
function Editor({
  initial,
  reload,
}: {
  initial: LocalSkill;
  reload: () => Promise<LocalSkill | null>;
}) {
  const [base, setBase] = useState(initial);
  const [files, setFiles] = useState(initial.files);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [compared, setCompared] = useState(false);
  const [newId, setNewId] = useState(`${initial.id.slice(0, 41)}-edited`);
  const [done, setDone] = useState<string | null>(null);
  const hint = useId();
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const dirty = !done && JSON.stringify(files) !== JSON.stringify(base.files);
  const idOk = /^[a-z][a-z0-9-]{1,47}$/.test(newId);
  const why = done
    ? ""
    : base.gallery_origin?.state === "unavailable"
      ? "This copy cannot be saved while its source evidence is unavailable."
      : !dirty
        ? "Edit a file to save an edited copy."
        : !idOk
          ? "A copy ID is 2–48 lowercase letters, digits or dashes, starting with a letter."
          : newId === base.id
            ? "Choose an ID different from the original skill."
            : "";
  return (
    <>
      <LeaveGuard dirty={dirty || busy} />
      <Link className={`${buttons.ghost} ${styles.back}`} to={SKILLS_PATH}>
        <ArrowLeft size={16} aria-hidden />
        Back to skills
      </Link>
      <header className={styles.detailHead}>
        <div className={styles.tag}>
          <FileText size={14} aria-hidden />
          Skill // {base.id}
        </div>
        <h1>{base.gallery_origin?.title ?? base.id}</h1>
      </header>
      <Origin
        value={base.gallery_origin}
        kind="skill"
        id={base.id}
        revision={base.revision}
      />
      {error && (
        <p role="alert" className={styles.error}>
          {error} Your unsaved edits remain here.
        </p>
      )}
      {compared && (
        <p className={styles.notice}>
          Your draft edits are retained; unedited files now match the stored
          version. Review the files before saving against the new revision.
        </p>
      )}
      <section className={styles.section}>
        <h2>Files</h2>
        {Object.entries(files).map(([name, value]) => (
          <FileRow key={name} name={name} open={name === "SKILL.md"}>
            {compared && (
              <div>
                <h3 className={styles.kicker}>Current stored file</h3>
                <pre>{ownValue(base.files, name) ?? "File absent"}</pre>
              </div>
            )}
            <label className={styles.field}>
              Your draft: {name}
              <textarea
                disabled={busy || !!done}
                value={value}
                spellCheck={false}
                onChange={(e) => {
                  setFiles((old) => ({ ...old, [name]: e.target.value }));
                  setNotice("");
                }}
              />
            </label>
            {name !== "SKILL.md" && (
              <button
                className={buttons.ghost}
                disabled={busy || !!done}
                onClick={() =>
                  setFiles((old) => {
                    const next = { ...old };
                    delete next[name];
                    return next;
                  })
                }
              >
                Remove draft file
              </button>
            )}
          </FileRow>
        ))}
        {compared &&
          Object.entries(base.files)
            .filter(([name]) => !Object.hasOwn(files, name))
            .map(([name, value]) => (
              <FileRow key={name} name={name} state="absent from your draft">
                <pre>{value}</pre>
                <div className={styles.choice}>
                  <button
                    className={buttons.ghost}
                    disabled={busy || !!done}
                    onClick={() => setFiles((old) => ({ ...old, [name]: value }))}
                  >
                    Keep stored file
                  </button>
                </div>
              </FileRow>
            ))}
        <div className={styles.addFile}>
          <label className={styles.field}>
            New relative file path
            <input
              disabled={busy || !!done}
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="notes/example.md"
            />
          </label>
          <button
            className={buttons.ghost}
            disabled={busy || !!done || !path || Object.hasOwn(files, path)}
            onClick={() => {
              setFiles((old) => ({ ...old, [path]: "" }));
              setPath("");
            }}
          >
            <Plus size={14} aria-hidden />
            Add text file
          </button>
        </div>
      </section>
      <section className={styles.section} aria-label="Save as a new copy">
        <h2>Save as a new copy</h2>
        <p>Saving creates a new local copy. Your original skill stays unchanged.</p>
        <div className={styles.saveRow}>
          <label className={styles.field}>
            New copy ID
            <input
              disabled={busy || !!done}
              value={newId}
              pattern="[a-z][a-z0-9-]{1,47}"
              onChange={(e) => setNewId(e.target.value)}
            />
          </label>
        </div>
        {notice && (
          <p role="status" className={styles.done}>
            {notice}{" "}
            {done && <Link to={`${SKILLS_PATH}/${encodeURIComponent(done)}`}>Open new copy</Link>}
          </p>
        )}
      </section>
      <div className={styles.savebar} data-testid="gallery-savebar">
        <p className={`${styles.why} ${why ? "" : styles.ready}`} id={hint}>
          {why || (done ? "" : `Ready to save ${newId}.`)}
        </p>
          <div className={styles.actions}>
            <button
              className={buttons.ghost}
              disabled={busy || !!done}
              onClick={async () => {
                setBusy(true);
                try {
                  const value = await reload();
                  if (!value && live.current)
                    setError(
                      "The current revision could not be loaded. Try comparing again.",
                    );
                  if (value && live.current) {
                    // Adopt stored changes only where this draft has no local edit.
                    // Own entries and computed keys keep every valid filename literal.
                    const names = new Set([
                      ...Object.keys(base.files),
                      ...Object.keys(files),
                      ...Object.keys(value.files),
                    ]);
                    setFiles(Object.fromEntries(
                      [...names].flatMap((name) => {
                        const draft = ownValue(files, name);
                        const result = draft === ownValue(base.files, name)
                          ? ownValue(value.files, name) : draft;
                        return result === undefined ? [] : [[name, result]];
                      }),
                    ));
                    setBase(value);
                    setCompared(true);
                    setError("");
                  }
                } finally {
                  if (live.current) setBusy(false);
                }
              }}
            >
              Compare stored version
            </button>
            <button
              className={buttons.primary}
              aria-describedby={why ? hint : undefined}
              disabled={
                busy || !dirty || !!done || newId === base.id ||
                !idOk ||
                base.gallery_origin?.state === "unavailable"
              }
              onClick={async () => {
                setBusy(true);
                setError("");
                try {
                  const operation = beginOperation();
                  const value = await api.saveLocalSkill(
                    base.id, newId, base.revision, base.gallery_origin?.pointer ?? null, files, operation,
                  );
                  finishOperation(value);
                  if (live.current) {
                    if (value.state === "complete" && value.result) {
                      setDone(value.result.id);
                      setCompared(false);
                      setNotice(value.result.durable === false
                        ? `Saved an edited copy. ${value.result.durable_reason}`
                        : "Saved an edited copy. Your original is unchanged.");
                    } else setError(value.message ?? "Check status above before retrying.");
                  }
                } catch (e) {
                  if (live.current) setError(errorText(e));
                } finally {
                  if (live.current) setBusy(false);
                }
              }}
            >
              <Copy size={16} aria-hidden />
              Save edited copy
            </button>
          </div>
      </div>
    </>
  );
}
