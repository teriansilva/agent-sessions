import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import { GALLERY_PATH, SKILLS_PATH } from "../lib/routes";
import type { LocalSkill } from "../types/gallery";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { HudFrame } from "../components/hud/HudFrame";
import { LeaveGuard, Origin, PendingOperation } from "../components/gallery/Shared";
import { beginOperation, finishOperation, errorText, ownValue } from "../components/gallery/operations";
import buttons from "../components/ui/actionButton.module.css";
import styles from "../components/gallery/gallery.module.css";

export default function SkillsLibrary() {
  const { skillId } = useParams();
  return (
    <main className={styles.page}>
      <HudFrame hero />
      <div className={styles.badge}>Library // Skills</div>
      <div className={styles.bar}>
        <h1>Local skills</h1>
        <Link className={buttons.ghost} to={GALLERY_PATH}>
          Browse public gallery
        </Link>
      </div>
      <p>
        Editable instruction files kept in your BattleLab library. Saving here
        does not activate a skill in an agent.
      </p>
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
        <section className={styles.panel}>
          <h2>No local skills yet</h2>
          <p>
            Inspect a skill in the public gallery, then import an editable copy.
          </p>
        </section>
      )}
      <div className={styles.grid}>
        {read.data?.skills.map((skill) => (
          <article className={styles.card} key={skill.id}>
            <h2>{skill.gallery_origin?.title ?? skill.id}</h2>
            <p>
              {skill.id} · {skill.gallery_origin?.version ?? "local"}
            </p>
            {skill.error ? (
              <p className={styles.error}>{skill.error}</p>
            ) : (
              <Link
                className={buttons.ghost}
                to={`${SKILLS_PATH}/${encodeURIComponent(skill.id)}`}
              >
                Open skill
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
      <p role={read.error ? "alert" : "status"}>
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
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const dirty = !done && JSON.stringify(files) !== JSON.stringify(base.files);
  return (
    <section className={styles.panel}>
      <LeaveGuard dirty={dirty || busy} />
      <div className={styles.bar}>
        <h2>{base.gallery_origin?.title ?? base.id}</h2>
        <Link className={buttons.ghost} to={SKILLS_PATH}>
          Back to skills
        </Link>
      </div>
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
      {notice && (
        <p role="status">
          {notice}{" "}
          {done && <Link to={`${SKILLS_PATH}/${encodeURIComponent(done)}`}>Open new copy</Link>}
        </p>
      )}
      {compared && (
        <p className={styles.notice}>
          Your draft edits are retained; unedited files now match the stored
          version. Review the files before saving against the new revision.
        </p>
      )}
      {Object.entries(files).map(([name, value]) => (
        <details key={name} open={name === "SKILL.md"}>
          <summary>{name}</summary>
          {compared && (
            <div>
              <h3>Current stored file</h3>
              <pre>{ownValue(base.files, name) ?? "File absent"}</pre>
            </div>
          )}
          <label>
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
        </details>
      ))}
      {compared &&
        Object.entries(base.files)
          .filter(([name]) => !Object.hasOwn(files, name))
          .map(([name, value]) => (
            <details key={name}>
              <summary>{name} · absent from your draft</summary>
              <pre>{value}</pre>
              <button
                className={buttons.ghost}
                disabled={busy || !!done}
                onClick={() => setFiles((old) => ({ ...old, [name]: value }))}
              >
                Keep stored file
              </button>
            </details>
          ))}
      <label>
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
        Add text file
      </button>
      <p className={styles.notice}>
        Saving creates a new local copy. Your original skill stays unchanged.
      </p>
      <label>
        New copy ID
        <input
          disabled={busy || !!done}
          value={newId}
          pattern="[a-z][a-z0-9-]{1,47}"
          onChange={(e) => setNewId(e.target.value)}
        />
      </label>
      <div className={styles.actions}>
        <button
          className={buttons.primary}
          disabled={
            busy || !dirty || !!done || newId === base.id ||
            !/^[a-z][a-z0-9-]{1,47}$/.test(newId) ||
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
                } else setError(value.message ?? "Check operation status before retrying.");
              }
            } catch (e) {
              if (live.current) setError(errorText(e));
            } finally {
              if (live.current) setBusy(false);
            }
          }}
        >
          Save edited copy
        </button>
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
      </div>
    </section>
  );
}
