import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { GALLERY_PATH, PLAYBOOKS_PATH, SKILLS_PATH } from "../lib/routes";
import type {
  GalleryComparison,
  GalleryEntry,
  GalleryFiles,
  GalleryKind,
} from "../types/gallery";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { HudFrame } from "../components/hud/HudFrame";
import {
  FileText,
  LeaveGuard,
  PendingOperation,
} from "../components/gallery/Shared";
import {
  beginOperation,
  errorText,
  finishOperation,
  localPath,
  ownValue,
} from "../components/gallery/operations";
import buttons from "../components/ui/actionButton.module.css";
import styles from "../components/gallery/gallery.module.css";

export default function ContentGallery() {
  const { digest } = useParams();
  const [query] = useSearchParams();
  const kind = query.get("kind");
  const id = query.get("id");
  const target: { kind: GalleryKind; id: string } | null =
    id && (kind === "playbook" || kind === "skill") ? { kind, id } : null;
  const read = usePlaybookRead(useCallback(() => api.publicGallery(), []));
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const matches =
    read.data?.entries.filter(
      (e) =>
        (!filter || e.content.kind === filter) &&
        [
          e.content.title,
          e.content.author,
          e.content.publisher,
          e.content.summary,
        ]
          .join(" ")
          .toLowerCase()
          .includes(search.toLowerCase()),
    ) ?? [];
  const entry = read.data?.entries.find((e) => e.digest === digest);
  return (
    <main className={styles.page}>
      <HudFrame hero />
      <div className={styles.badge}>Library // Gallery</div>
      <div className={styles.bar}>
        <h1>Playbooks & skills</h1>
        <div className={styles.actions}>
          <Link className={buttons.ghost} to={PLAYBOOKS_PATH}>
            Local playbooks
          </Link>
          <Link className={buttons.ghost} to={SKILLS_PATH}>
            Local skills
          </Link>
          <button
            className={buttons.ghost}
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError("");
              try {
                await api.refreshGallery();
                await read.reload();
              } catch (e) {
                setError(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            Check public gallery
          </button>
        </div>
      </div>
      <p>Inspect complete content, then import an editable local copy.</p>
      <PendingOperation />
      {(error || read.error || read.data?.error) && (
        <p role="alert" className={styles.error}>
          {error || read.error || read.data?.error} Previous local content is
          retained.
        </p>
      )}
      {read.data?.state === "stale" && (
        <p className={styles.notice}>
          This publication has expired. Inspection remains available; new
          imports and updates need a fresh signed publication.
        </p>
      )}
      {target && (
        <p className={styles.notice}>
          Choose a version to compare with{" "}
          <Link to={localPath(target.kind, target.id)}>{target.id}</Link>.
          Comparisons only accept the same upstream content.
        </p>
      )}
      {digest ? (
        entry ? (
          target ? (
            <Compare
              key={`${digest}:${target.kind}:${target.id}`}
              entry={entry}
              target={target}
            />
          ) : (
            <Inspect key={digest} entry={entry} />
          )
        ) : (
          <p>
            {read.loading
              ? "Loading content…"
              : "This version is not in the accepted gallery. Check the public gallery and try again."}
          </p>
        )
      ) : (
        <>
          <div className={styles.filters}>
            <label>
              Search
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Title, author, publisher…"
              />
            </label>
            <label>
              Kind
              <select
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              >
                <option value="">All content</option>
                <option value="playbook">Playbooks</option>
                <option value="skill">Skills</option>
              </select>
            </label>
          </div>
          {read.loading && !read.data && <p role="status">Loading gallery…</p>}
          {read.data && !read.data.entries.length && (
            <section className={styles.panel}>
              <h2>No accepted gallery content yet</h2>
              <p>
                Check the public gallery to load its signed publication. Your
                local library remains available.
              </p>
            </section>
          )}
          {!!read.data?.entries.length && !matches.length && (
            <p>No gallery content matches this search.</p>
          )}
          <div className={styles.grid}>
            {matches.map((e) => (
              <article className={styles.card} key={e.digest}>
                <div className={styles.badge}>
                  {e.content.kind} · {e.content.version}
                </div>
                <h2>{e.content.title}</h2>
                <p>{e.content.summary}</p>
                <p>
                  {e.content.author} · {e.content.publisher}
                </p>
                {!e.available && <p>Withdrawn · inspect only</p>}
                <Link
                  className={buttons.ghost}
                  to={`${GALLERY_PATH}/${e.digest}${target ? `?kind=${target.kind}&id=${encodeURIComponent(target.id)}` : ""}`}
                >
                  {target ? "Compare this version" : "Inspect complete content"}
                </Link>
              </article>
            ))}
          </div>
        </>
      )}
      <details>
        <summary>Publication evidence</summary>
        <p>
          Sequence {read.data?.sequence ?? "none"} ·{" "}
          {read.data?.expires_at
            ? `expires ${new Date(read.data.expires_at * 1000).toLocaleString()}`
            : "not checked"}
        </p>
        <code>{read.data?.digest}</code>
        <p>
          <a
            href="https://relay.battlelab.superstatus.io/catalogs/gallery"
            target="_blank"
            rel="noreferrer"
          >
            Open public gallery
          </a>
        </p>
      </details>
    </main>
  );
}
function Inspect({ entry }: { entry: GalleryEntry }) {
  const [id, setId] = useState(entry.content.id.slice(0, 42) + "-copy");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const read = usePlaybookRead(
    useCallback(() => api.inspectGallery(entry.digest, id), [entry.digest, id]),
  );
  const c = entry.content;
  return (
    <section className={styles.panel}>
      <div className={styles.bar}>
        <h2>
          {c.title} · {c.version}
        </h2>
        <Link className={buttons.ghost} to={GALLERY_PATH}>
          Back to gallery
        </Link>
      </div>
      <p>{c.summary}</p>
      <p>
        By {c.author} · published by {c.publisher}
      </p>
      <a href={c.source} target="_blank" rel="noreferrer">
        Source
      </a>
      <h3>Requirements</h3>
      {c.requirements.length ? (
        c.requirements.map((r, i) => <p key={i}>{r}</p>)
      ) : (
        <p>No additional requirements declared.</p>
      )}
      {read.data?.preview && (
        <section aria-label="Inputs and workflow">
          <h3>Inputs & workflow</h3>
          {read.data.preview.inputs.map((input) => (
            <p key={input.name}>
              {input.label ?? input.name} ({input.name}) ·{" "}
              {input.required ? "required" : "optional"}
            </p>
          ))}
          {read.data.preview.flows?.map((flow) => (
            <div key={flow.id}>
              <h4>{flow.title}</h4>
              <ol>
                {flow.steps.map((step) => (
                  <li key={step.id}>
                    {step.title} ·{" "}
                    {step.actor.engine ?? step.actor.label ?? step.actor.kind}
                    {step.after.length
                      ? ` · after ${step.after.join(", ")}`
                      : ""}
                  </li>
                ))}
              </ol>
            </div>
          ))}
        </section>
      )}
      <h3>Complete files</h3>
      {Object.entries(c.files).map(([name, value]) => (
        <details key={name}>
          <summary>{name}</summary>
          <FileText value={value} />
        </details>
      ))}
      <p>
        {c.kind === "playbook"
          ? "Only the local playbook ID is rewritten. Flows and all other files retain their identities."
          : "This creates inert files in your BattleLab library. You decide when and how to use the skill."}
      </p>
      <LeaveGuard dirty={busy} />
      <label>
        Local copy ID
        <input
          disabled={busy}
          value={id}
          pattern="[a-z][a-z0-9-]{1,47}"
          onChange={(e) => {
            setId(e.target.value);
            setAck(false);
            setDone(null);
          }}
        />
      </label>
      <label className={styles.check}>
        <input
          type="checkbox"
          disabled={busy}
          checked={ack}
          onChange={(e) => setAck(e.target.checked)}
        />
        I reviewed the complete content and requirements.
      </label>
      {(read.data?.reason || read.error) && (
        <p role="alert">{read.data?.reason || read.error}</p>
      )}
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <button
        className={buttons.primary}
        disabled={
          busy ||
          read.loading ||
          !!read.error ||
          !read.data?.importable ||
          !ack ||
          !!done
        }
        onClick={async () => {
          setBusy(true);
          setError("");
          try {
            const op = beginOperation();
            const value = await api.importGallery(c.kind, id, entry.digest, op);
            finishOperation(value);
            if (value.state === "complete")
              setDone(
                value.result?.durable === false
                  ? `Imported. ${value.result.durable_reason}`
                  : "Imported an editable local copy.",
              );
            else
              setError(value.message ?? "Check the operation before retrying.");
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        Import editable copy
      </button>
      {done && (
        <p role="status">
          {done} <Link to={localPath(c.kind, id)}>Open local copy</Link>
        </p>
      )}
    </section>
  );
}
function Compare({
  entry,
  target,
}: {
  entry: GalleryEntry;
  target: { kind: GalleryKind; id: string };
}) {
  const read = usePlaybookRead(
    useCallback(
      () => api.compareGallery(target.kind, target.id, entry.digest),
      [target.kind, target.id, entry.digest],
    ),
  );
  if (!read.data)
    return (
      <section className={styles.panel}>
        <p role={read.error ? "alert" : "status"}>
          {read.error ??
            "Comparing imported base, local copy and incoming version…"}
        </p>
      </section>
    );
  return <Merge initial={read.data} reload={read.reload} />;
}
function Merge({
  initial,
  reload,
}: {
  initial: GalleryComparison;
  reload: () => Promise<GalleryComparison | null>;
}) {
  const [view, setView] = useState(initial);
  const [files, setFiles] = useState<GalleryFiles>(() =>
    structuredClone(initial.proposed),
  );
  const [unresolved, setUnresolved] = useState(new Set(initial.conflicts));
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const [newId, setNewId] = useState(`${initial.id.slice(0, 40)}-updated`);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  function choose(name: string, value: GalleryFiles[string] | undefined) {
    setFiles((old) => {
      if (value !== undefined) return { ...old, [name]: value };
      const next = { ...old };
      delete next[name];
      return next;
    });
    setUnresolved((old) => {
      const next = new Set(old);
      next.delete(name);
      return next;
    });
    setDirty(true);
    setAck(false);
  }
  const retainedDifferences = (proposal: GalleryFiles) =>
    [...new Set([...Object.keys(files), ...Object.keys(proposal)])].filter(
      (name) =>
        JSON.stringify(ownValue(files, name)) !==
        JSON.stringify(ownValue(proposal, name)),
    );
  const reviewedFiles = [...new Set([
    ...view.changed_files,
    ...retainedDifferences(view.proposed),
  ])].sort();
  return (
    <section className={styles.panel}>
      <LeaveGuard dirty={dirty || busy} />
      <h2>Compare update to {view.incoming_version}</h2>
      <p>
        Review the imported base, your current copy and the incoming files.
        Choose the result for each conflicting file.
      </p>
      {error && (
        <p role="alert" className={styles.error}>
          {error} Your draft is retained.
        </p>
      )}
      {reviewedFiles.map((name) => {
        const result = ownValue(files, name);
        return (
          <details key={name} open>
            <summary>
              {name}
              {unresolved.has(name) ? " · choose a result" : ""}
            </summary>
            <div className={styles.compare}>
              {(
                [
                  ["Imported base", view.base],
                  ["Local copy", view.local],
                  ["Incoming", view.incoming],
                ] as const
              ).map(([label, source]) => (
                <div key={label}>
                  <h3>{label}</h3>
                  <FileText value={ownValue(source, name)} />
                </div>
              ))}
            </div>
            <div className={styles.actions}>
              <button
                className={buttons.ghost}
                disabled={busy}
                onClick={() => choose(name, result)}
              >
                Keep retained result
              </button>
              <button
                className={buttons.ghost}
                disabled={busy}
                onClick={() => choose(name, ownValue(view.local, name))}
              >
                Keep local file
              </button>
              <button
                className={buttons.ghost}
                disabled={busy}
                onClick={() => choose(name, ownValue(view.incoming, name))}
              >
                Use incoming file
              </button>
            </div>
            {typeof result === "string" && (
              <label>
                Result for {name}
                <textarea
                  disabled={busy}
                  value={result}
                  spellCheck={false}
                  onChange={(e) => choose(name, e.target.value)}
                />
              </label>
            )}
            {result === undefined && <p>This file will be absent.</p>}
            {typeof result === "object" && <FileText value={result} />}
          </details>
        );
      })}
      {!reviewedFiles.length && (
        <p>The file contents match the imported base.</p>
      )}
      <p className={styles.notice}>
        This creates a new local copy. Your original and existing project references stay unchanged.
        Choose the new copy explicitly when you are ready to use it.
      </p>
      <label>
        New copy ID
        <input
          disabled={busy || !!done}
          value={newId}
          pattern="[a-z][a-z0-9-]{1,47}"
          onChange={(e) => { setNewId(e.target.value); setAck(false); setDirty(true); }}
        />
      </label>
      {view.kind === "playbook" && <p>Only the playbook ID is rewritten; workflow identities are preserved.</p>}
      <label className={styles.check}>
        <input
          type="checkbox"
          disabled={busy || !!done}
          checked={ack}
          onChange={(e) => setAck(e.target.checked)}
        />
        I reviewed these results and want to create a new copy.
      </label>
      <div className={styles.actions}>
        <button
          className={buttons.primary}
          disabled={busy || !ack || unresolved.size > 0 || !!done || newId === view.id || !/^[a-z][a-z0-9-]{1,47}$/.test(newId)}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              const operation = beginOperation();
              const result = await api.updateGallery(
                view.kind,
                view.id,
                newId,
                view.incoming_digest,
                view.comparison,
                files,
                operation,
              );
              finishOperation(result);
              if (live.current) {
                if (result.state === "complete") {
                  setDone(result.result?.id ?? newId);
                  setDirty(false);
                  if (result.result?.durable === false)
                    setError(
                      result.result.durable_reason ??
                        "Inspect durable storage before further changes.",
                    );
                } else setError(result.message ?? "Check operation status.");
              }
            } catch (e) {
              if (live.current) setError(errorText(e));
            } finally {
              if (live.current) setBusy(false);
            }
          }}
        >
          Create updated copy
        </button>
        <button
          className={buttons.ghost}
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              const next = await reload();
              if (!next && live.current)
                setError(
                  "The current revision could not be loaded. Try comparing again.",
                );
              if (next && live.current) {
                setView(next);
                setUnresolved(new Set([...next.conflicts, ...retainedDifferences(next.proposed)]));
                setAck(false);
                setError("");
                setDone(null);
              }
            } finally {
              if (live.current) setBusy(false);
            }
          }}
        >
          Compare current revision again
        </button>
      </div>
      {done && (
        <p role="status">
          Created an updated copy. Your original is unchanged.{" "}
          <Link to={localPath(view.kind, done)}>Open new copy</Link>
        </p>
      )}
    </section>
  );
}
