import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  ChevronRight,
  Copy,
  Download,
  FileText as SkillIcon,
  GitCompareArrows,
  RefreshCw,
  Search,
  Workflow,
} from "lucide-react";
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
  FileRow,
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

const KINDS = [
  ["", "All"],
  ["playbook", "Playbooks"],
  ["skill", "Skills"],
] as const;
const COPY_ID = /^[a-z][a-z0-9-]{1,47}$/;
const COPY_ID_HINT =
  "A copy ID is 2–48 lowercase letters, digits or dashes, starting with a letter.";
const plural = (n: number, one: string, many = `${one}s`) =>
  `${n} ${n === 1 ? one : many}`;

function KindIcon({ kind }: { kind: GalleryKind }) {
  return kind === "playbook" ? (
    <Workflow size={14} aria-hidden />
  ) : (
    <SkillIcon size={14} aria-hidden />
  );
}

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
  const total = read.data?.entries.length ?? 0;
  const refresh = async () => {
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
  };
  return (
    <main className={styles.page}>
      <HudFrame hero />
      <div className={styles.kicker}>
        Library // Gallery{!digest && read.data ? ` // ${total}` : ""}
      </div>
      {!digest && (
        <>
          <div className={styles.header}>
            <h1>{target ? "Choose a version" : "Gallery"}</h1>
            <button className={buttons.ghost} disabled={busy} onClick={refresh}>
              <RefreshCw size={14} aria-hidden />
              Check public gallery
            </button>
          </div>
          <p className={styles.intro}>
            Signed playbooks and skills to start from. Inspect everything, then
            import your own editable copy. Copies land in{" "}
            <Link to={PLAYBOOKS_PATH}>Local playbooks</Link> and{" "}
            <Link to={SKILLS_PATH}>Local skills</Link>, and nothing runs until
            you use it.
          </p>
        </>
      )}
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
      {target && !digest && (
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
          <div className={styles.toolbar}>
            <label className={styles.search}>
              Search
              <span className={styles.searchBox}>
                <Search size={16} aria-hidden />
                <input
                  type="search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Title, author, publisher…"
                />
              </span>
            </label>
            <div className={styles.segmented} role="group" aria-label="Kind">
              {KINDS.map(([value, label]) => (
                <button
                  key={value}
                  className={buttons.ghost}
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            {!!total && (
              <span className={`${styles.tag} ${styles.count}`}>
                {matches.length} of {total}
              </span>
            )}
          </div>
          {read.loading && !read.data && <p role="status">Loading gallery…</p>}
          {read.data && !read.data.entries.length && (
            <section className={styles.empty}>
              <h2>No accepted gallery content yet</h2>
              <p>
                Check the public gallery to load its signed publication. Your
                local library remains available.
              </p>
            </section>
          )}
          {!!read.data?.entries.length && !matches.length && (
            <div className={styles.empty}>
              <p>No gallery content matches this search.</p>
              <button
                className={buttons.ghost}
                onClick={() => {
                  setSearch("");
                  setFilter("");
                }}
              >
                Clear search
              </button>
            </div>
          )}
          <div className={styles.grid}>
            {matches.map((e) => (
              <article className={styles.card} key={e.digest}>
                <div className={styles.tag}>
                  <KindIcon kind={e.content.kind} />
                  {e.content.kind} // v{e.content.version}
                </div>
                <h2>{e.content.title}</h2>
                <p>{e.content.summary}</p>
                <p className={styles.meta}>
                  By {e.content.author} · {e.content.publisher}
                </p>
                {!e.available && (
                  <span className={`${styles.tag} ${styles.withdrawn}`}>
                    Withdrawn · inspect only
                  </span>
                )}
                <Link
                  className={`${buttons.ghost} ${styles.cardAction}`}
                  to={`${GALLERY_PATH}/${e.digest}${target ? `?kind=${target.kind}&id=${encodeURIComponent(target.id)}` : ""}`}
                >
                  {target ? "Compare this version" : "Inspect complete content"}
                  <ArrowRight size={16} aria-hidden />
                </Link>
              </article>
            ))}
          </div>
        </>
      )}
      {!digest && (
        <details className={`${styles.file} ${styles.evidence}`}>
          <summary>
            <ChevronRight size={16} aria-hidden />
            <span>Publication evidence</span>
          </summary>
          <div className={styles.fileBody}>
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
          </div>
        </details>
      )}
    </main>
  );
}
function Inspect({ entry }: { entry: GalleryEntry }) {
  const [id, setId] = useState(entry.content.id.slice(0, 42) + "-copy");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const hint = useId();
  const read = usePlaybookRead(
    useCallback(() => api.inspectGallery(entry.digest, id), [entry.digest, id]),
  );
  const c = entry.content;
  const why = done
    ? ""
    : read.loading
      ? "Checking this copy ID…"
      : read.error || !read.data?.importable
        ? ""
        : !ack
          ? "Tick the review box to import."
          : "";
  return (
    <>
      <Link className={`${buttons.ghost} ${styles.back}`} to={GALLERY_PATH}>
        <ArrowLeft size={16} aria-hidden />
        Back to gallery
      </Link>
      <div className={styles.detail}>
        <article>
          <header className={styles.detailHead}>
            <div className={styles.tag}>
              <KindIcon kind={c.kind} />
              {c.kind} // v{c.version}
            </div>
            <h1>{c.title}</h1>
            <p>{c.summary}</p>
            <p>
              By {c.author} · published by {c.publisher} ·{" "}
              <a href={c.source} target="_blank" rel="noreferrer">
                Source
              </a>
            </p>
          </header>
          <section className={styles.section}>
            <h2>Requirements</h2>
            {c.requirements.length ? (
              <ul>
                {c.requirements.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            ) : (
              <p>No additional requirements declared.</p>
            )}
          </section>
          {read.data?.preview && (
            <section className={styles.section} aria-label="Inputs and workflow">
              <h2>Inputs & workflow</h2>
              <ul>
                {read.data.preview.inputs.map((input) => (
                  <li key={input.name}>
                    {input.label ?? input.name} ({input.name}) ·{" "}
                    {input.required ? "required" : "optional"}
                  </li>
                ))}
              </ul>
              {read.data.preview.flows?.map((flow) => (
                <div key={flow.id}>
                  <h3>{flow.title}</h3>
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
          <section className={styles.section}>
            <h2>Complete files</h2>
            {Object.entries(c.files).map(([name, value]) => (
              <FileRow key={name} name={name}>
                <FileText value={value} />
              </FileRow>
            ))}
          </section>
        </article>
        <aside className={styles.side} aria-label="Import">
          <h2>Import a copy</h2>
          <p>
            {c.kind === "playbook"
              ? "Only the local playbook ID is rewritten. Flows and all other files retain their identities."
              : "This creates inert files in your BattleLab library. You decide when and how to use the skill."}
          </p>
          <LeaveGuard dirty={busy} />
          <label className={styles.field}>
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
            <p role="alert" className={styles.error}>
              {read.data?.reason || read.error}
            </p>
          )}
          {error && (
            <p className={styles.error} role="alert">
              {error}
            </p>
          )}
          <button
            className={`${buttons.primary} ${styles.wide}`}
            aria-describedby={why ? hint : undefined}
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
            <Download size={16} aria-hidden />
            Import editable copy
          </button>
          {why && (
            <p className={styles.why} id={hint}>
              {why}
            </p>
          )}
          {done && (
            <p role="status" className={styles.done}>
              {done} <Link to={localPath(c.kind, id)}>Open local copy</Link>
            </p>
          )}
        </aside>
      </div>
    </>
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
      <p
        role={read.error ? "alert" : "status"}
        className={read.error ? styles.error : styles.notice}
      >
        {read.error ??
          "Comparing imported base, local copy and incoming version…"}
      </p>
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
  const hint = useId();
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
  const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
  const changes = reviewedFiles.length;
  const why = done
    ? ""
    : unresolved.size
      ? `Choose a result for ${plural(unresolved.size, "file")} first.`
      : !COPY_ID.test(newId)
        ? COPY_ID_HINT
        : newId === view.id
          ? "Choose an ID different from the original copy."
          : !ack
            ? "Tick the review box to create the copy."
            : "";
  return (
    <>
      <LeaveGuard dirty={dirty || busy} />
      <Link className={`${buttons.ghost} ${styles.back}`} to={GALLERY_PATH}>
        <ArrowLeft size={16} aria-hidden />
        Back to gallery
      </Link>
      <header className={styles.detailHead}>
        <div className={styles.tag}>
          <GitCompareArrows size={14} aria-hidden />
          Update // {view.kind} // v{view.incoming_version}
        </div>
        <h1>Compare update to {view.incoming_version}</h1>
        <p>
          Updating your copy{" "}
          <Link to={localPath(view.kind, view.id)}>{view.id}</Link>. Review the
          imported base, your current copy and the incoming files. Choose the
          result for each conflicting file.
        </p>
      </header>
      <div className={styles.summary}>
        <span className={styles.tag}>{plural(changes, "file")} changed</span>
        {unresolved.size ? (
          <span className={styles.tag}>
            <span className="hud-led attention" aria-hidden />
            {plural(unresolved.size, "file")}{" "}
            {unresolved.size === 1 ? "needs" : "need"} a choice
          </span>
        ) : changes ? (
          <span className={styles.tag}>Every file has a result</span>
        ) : null}
      </div>
      {error && (
        <p role="alert" className={styles.error}>
          {error} Your draft is retained.
        </p>
      )}
      {reviewedFiles.map((name) => {
        const result = ownValue(files, name);
        const local = ownValue(view.local, name);
        const incoming = ownValue(view.incoming, name);
        const using = unresolved.has(name)
          ? null
          : same(result, local)
            ? "local"
            : same(result, incoming)
              ? "incoming"
              : "retained";
        return (
          <FileRow
            key={name}
            name={name}
            open
            state={
              using === null ? (
                <>
                  <span className="hud-led attention" aria-hidden />
                  choose a result
                </>
              ) : using === "retained" ? (
                "using the result below"
              ) : (
                `using the ${using} file`
              )
            }
          >
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
            <div className={styles.choice}>
              <span>Result</span>
              <div
                className={styles.segmented}
                role="group"
                aria-label={`Versions of ${name}`}
              >
                <button
                  className={buttons.ghost}
                  aria-label="Keep retained result"
                  aria-pressed={using === "retained"}
                  disabled={busy}
                  onClick={() => choose(name, result)}
                >
                  Retained result
                </button>
                <button
                  className={buttons.ghost}
                  aria-label="Keep local file"
                  aria-pressed={using === "local"}
                  disabled={busy}
                  onClick={() => choose(name, local)}
                >
                  Local file
                </button>
                <button
                  className={buttons.ghost}
                  aria-label="Use incoming file"
                  aria-pressed={using === "incoming"}
                  disabled={busy}
                  onClick={() => choose(name, incoming)}
                >
                  Incoming file
                </button>
              </div>
            </div>
            {typeof result === "string" && (
              <label className={styles.field}>
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
          </FileRow>
        );
      })}
      {!reviewedFiles.length && (
        <p>The file contents match the imported base.</p>
      )}
      <section className={styles.section} aria-label="Save as a new copy">
        <h2>Save as a new copy</h2>
        <p>
          Creates a new copy. Your original and existing project references stay
          unchanged. Choose the new copy explicitly when you are ready to use it.
          {view.kind === "playbook" &&
            " Only the playbook ID is rewritten; workflow identities are preserved."}
        </p>
        <div className={styles.saveRow}>
          <label className={styles.field}>
            New copy ID
            <input
              disabled={busy || !!done}
              value={newId}
              pattern="[a-z][a-z0-9-]{1,47}"
              onChange={(e) => { setNewId(e.target.value); setAck(false); setDirty(true); }}
            />
          </label>
          <label className={styles.check}>
            <input
              type="checkbox"
              disabled={busy || !!done}
              checked={ack}
              onChange={(e) => setAck(e.target.checked)}
            />
            I reviewed these results and want to create a new copy.
          </label>
        </div>
        {done && (
          <p role="status" className={styles.done}>
            Created an updated copy. Your original is unchanged.{" "}
            <Link to={localPath(view.kind, done)}>Open new copy</Link>
          </p>
        )}
      </section>
      <div className={styles.savebar} data-testid="gallery-savebar">
        <p className={`${styles.why} ${why ? "" : styles.ready}`} id={hint}>
          {why || (done ? "" : `Ready to create ${newId}.`)}
        </p>
        <div className={styles.actions}>
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
          <button
            className={buttons.primary}
            aria-describedby={why ? hint : undefined}
            disabled={busy || !ack || unresolved.size > 0 || !!done || newId === view.id || !COPY_ID.test(newId)}
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
            <Copy size={16} aria-hidden />
            Create updated copy
          </button>
        </div>
      </div>
    </>
  );
}
