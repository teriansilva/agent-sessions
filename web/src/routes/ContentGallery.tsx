/** Library → Playbooks / Skills → Browse gallery (#1440): the public, signed gallery for ONE kind,
 *  in plain language. Review comes before adding (requirements, what's inside, every file's full
 *  content), adding is one explicit confirmation, and every digest/signature/source fact is folded
 *  into Technical details. The server contract is unchanged: `acknowledge` is still required,
 *  NOREPLACE still refuses an occupied ID, and every write is still an explicit click. */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  Link,
  Navigate,
  useLocation,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  ChevronRight,
  FileText as SkillIcon,
  GitCompareArrows,
  Plus,
  RefreshCw,
  Search,
  Workflow,
} from "lucide-react";
import { ApiError, api } from "../lib/api";
import {
  GALLERY_SKILLS_PATH,
  galleryEntryPath,
  galleryPath,
} from "../lib/routes";
import type {
  GalleryComparison,
  GalleryEntry,
  GalleryFiles,
  GalleryIndex,
  GalleryKind,
} from "../types/gallery";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { PlaybookDialog } from "../components/playbooks/PlaybookDialog";
import {
  Fact,
  FileRow,
  FileText,
  LeaveGuard,
  PendingOperation,
  TechnicalDetails,
  Verified,
} from "../components/gallery/Shared";
import { LandingNote, ViewTabs } from "../components/gallery/ViewTabs";
import { freeId, idRule, updateId } from "../components/gallery/galleryRules";
import { bindingRole, type PlaybookStep } from "../types/playbooks";
import {
  galleryWait,
  useGallery,
  type GalleryProblem,
} from "../components/gallery/useGallery";
import {
  beginOperation,
  errorText,
  finishOperation,
  localPath,
  ownValue,
  settleRefused,
  usePendingId,
} from "../components/gallery/operations";
import buttons from "../components/ui/actionButton.module.css";
import styles from "../components/gallery/gallery.module.css";

const plural = (n: number, one: string, many = `${one}s`) =>
  `${n} ${n === 1 ? one : many}`;
const NOUN = { playbook: "playbooks", skill: "skills" } as const;
const STALE =
  "This gallery listing is out of date, so adding is paused until it refreshes.";

/** #1654: a format 5 step's role in the preview, instead of an engine/model it no longer names. */
function roleText(step: PlaybookStep): string | null {
  const role = bindingRole(step);
  if (!role) return null;
  const grade =
    { fast: "Intern", strong: "Specialist", frontier: "Senior" }[
      role.min_grade ?? ""
    ] ?? "any grade";
  return `role: ${role.skills.length ? role.skills.join(", ") : "no skills"} · at least ${grade}`;
}

function KindIcon({ kind }: { kind: GalleryKind }) {
  return kind === "playbook" ? (
    <Workflow size={14} aria-hidden />
  ) : (
    <SkillIcon size={14} aria-hidden />
  );
}

/** IDs already used by this kind's library: playbook IDs are unique across bundled and local. */
function useTakenIds(kind: GalleryKind) {
  const read = usePlaybookRead(
    useCallback(
      async () =>
        new Set(
          kind === "playbook"
            ? (await api.playbooks()).playbooks.map((p) => p.id)
            : (await api.localSkills()).skills.map((s) => s.id),
        ),
      [kind],
    ),
  );
  return { taken: read.data, reloadTaken: read.reload };
}

/** What the listing state means to the operator, in one callout (or none). */
function ListingState({
  gallery,
  hasEntries,
}: {
  gallery: ReturnType<typeof useGallery>;
  hasEntries: boolean;
}) {
  const { problem, data, error, checking, check } = gallery;
  const retry = (
    <button
      className={buttons.ghost}
      disabled={checking}
      onClick={() => void check()}
    >
      <RefreshCw size={14} aria-hidden />
      Try again
    </button>
  );
  const callout = (
    text: string,
    tone: "notice" | "error",
    p?: GalleryProblem | null,
    testid?: string,
  ) => (
    <div
      className={tone === "error" ? styles.error : styles.notice}
      role={tone === "error" ? "alert" : "status"}
      data-testid={testid}
    >
      <p>{text}</p>
      <div className={styles.actions}>{retry}</div>
      {p?.detail && (
        <TechnicalDetails>
          <Fact label="Server answer">{p.detail}</Fact>
        </TechnicalDetails>
      )}
    </div>
  );
  if (error)
    return callout(
      `The gallery couldn't be loaded. ${error}`,
      "error",
      null,
      "gallery-load-error",
    );
  if (data?.state === "unavailable")
    return callout(
      "The gallery listing saved on this device can't be read. Your playbooks and skills are not affected.",
      "error",
      { kind: "refused", detail: data.error ?? "" },
      "gallery-local-unreadable",
    );
  if (problem?.kind === "unreachable")
    return callout(
      hasEntries
        ? "The public gallery can't be reached right now, so this is the list from the last check. Your playbooks and skills are not affected."
        : "The public gallery can't be reached right now. Your playbooks and skills are not affected.",
      "notice",
      problem,
      "gallery-unreachable",
    );
  if (problem?.kind === "refused")
    return callout(
      "The public gallery sent a listing that couldn't be verified, so nothing changed. Your playbooks and skills are not affected.",
      "error",
      problem,
      "gallery-refused",
    );
  if (data?.state === "stale")
    return (
      <p
        className={`${styles.notice} ${styles.warn}`}
        data-testid="gallery-stale"
      >
        {STALE}
      </p>
    );
  return null;
}

export default function ContentGallery() {
  const { digest } = useParams();
  const [query] = useSearchParams();
  const location = useLocation();
  // One component for both views; the route says which kind it lists.
  const kind: GalleryKind =
    location.pathname === GALLERY_SKILLS_PATH ||
    location.pathname.startsWith(`${GALLERY_SKILLS_PATH}/`)
      ? "skill"
      : "playbook";
  const update = query.get("update");
  const target = update !== null && idRule(kind).re.test(update) ? update : null;
  const gallery = useGallery();
  const [search, setSearch] = useState("");
  const all = gallery.data?.entries ?? [];
  const mine = all.filter((e) => e.content.kind === kind);
  const needle = search.trim().toLowerCase();
  const matches = mine.filter((e) =>
    [e.content.title, e.content.author, e.content.publisher, e.content.summary]
      .join(" ")
      .toLowerCase()
      .includes(needle),
  );
  const entry = digest ? all.find((e) => e.digest === digest) : undefined;
  // The kind comes from the signed entry, never the URL: an old link opened in the wrong view moves.
  if (entry && entry.content.kind !== kind)
    return (
      <Navigate
        replace
        to={`${galleryEntryPath(entry.content.kind, entry.digest)}${location.search}`}
        state={location.state}
      />
    );
  const settled = !gallery.loading && !gallery.checking;
  return (
    <main className={styles.page} data-testid="content-gallery">
      <div className={`nj-kicker ${styles.kicker}`}>
        <span className="nj-notch" aria-hidden="true" />
        Library // {NOUN[kind]} // <b>gallery</b>
        {!digest && gallery.data ? ` // ${mine.length}` : ""}
      </div>
      {!digest && (
        <>
          <div className={styles.header}>
            <h1>{kind === "playbook" ? "Playbooks" : "Skills"}</h1>
            <button
              className={buttons.ghost}
              disabled={gallery.checking}
              onClick={() => void gallery.check()}
            >
              <RefreshCw size={14} aria-hidden />
              {gallery.checking ? "Checking…" : "Refresh"}
            </button>
          </div>
          <ViewTabs kind={kind} view="gallery" />
          <p className={styles.intro}>
            {kind === "playbook"
              ? "Ready-made playbooks from the public gallery. Look inside any of them, then add your own editable copy. Nothing runs until you use it."
              : "Ready-made skills from the public gallery. Look inside any of them, then add your own editable copy. Adding a skill doesn't turn it on in any agent."}
          </p>
        </>
      )}
      <LandingNote />
      <PendingOperation />
      <ListingState gallery={gallery} hasEntries={mine.length > 0} />
      {digest ? (
        entry ? (
          target ? (
            <Compare
              key={`${digest}:${target}`}
              entry={entry}
              id={target}
              wait={galleryWait(gallery)}
            />
          ) : (
            <Inspect
              key={digest}
              entry={entry}
              listing={gallery.data}
              wait={galleryWait(gallery)}
            />
          )
        ) : !settled ? (
          <p role="status">Loading…</p>
        ) : (
          <div className={styles.empty} data-testid="gallery-entry-missing">
            <h2>This version isn't in the gallery any more.</h2>
            <Link className={buttons.ghost} to={galleryPath(kind)}>
              <ArrowLeft size={16} aria-hidden />
              Browse gallery
            </Link>
          </div>
        )
      ) : (
        <>
          {!!mine.length && (
            <div className={styles.toolbar}>
              <label className={styles.search}>
                Search the gallery
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
            </div>
          )}
          {!mine.length && !settled && (
            <p role="status" data-testid="gallery-checking">
              Checking the public gallery…
            </p>
          )}
          {settled &&
            gallery.data &&
            !mine.length &&
            !gallery.problem &&
            gallery.data.state !== "unavailable" && (
              <section className={styles.empty} data-testid="gallery-empty">
                <h2>
                  {all.length
                    ? `No ${NOUN[kind]} have been published to the gallery yet.`
                    : "Nothing has been published to the gallery yet."}
                </h2>
              </section>
            )}
          {!!mine.length && !matches.length && (
            <div className={styles.empty}>
              <p>Nothing in the gallery matches this search.</p>
              <button className={buttons.ghost} onClick={() => setSearch("")}>
                Clear search
              </button>
            </div>
          )}
          <div className={styles.grid}>
            {matches.map((e) => (
              <article className={styles.card} key={e.digest}>
                <div className={`${styles.tag} ${styles.stamp}`}>
                  <KindIcon kind={e.content.kind} />
                  Gallery
                  <span className={styles.stampId}>v{e.content.version}</span>
                </div>
                <h2>{e.content.title}</h2>
                <p>{e.content.summary}</p>
                <p className={styles.meta}>
                  By {e.content.author} · {e.content.publisher}
                </p>
                {e.available ? (
                  <Verified />
                ) : (
                  <span className={`${styles.tag} ${styles.withdrawn}`}>
                    Withdrawn · view only
                  </span>
                )}
                <Link
                  className={`${buttons.ghost} ${styles.cardAction}`}
                  to={galleryEntryPath(kind, e.digest)}
                  aria-label={`View ${e.content.title}`}
                >
                  View
                  <ArrowRight size={16} aria-hidden />
                </Link>
              </article>
            ))}
          </div>
          <ListingFacts listing={gallery.data} />
        </>
      )}
    </main>
  );
}

function ListingFacts({ listing }: { listing: GalleryIndex | null }) {
  return (
    <TechnicalDetails>
      <Fact label="Signed publication">
        {listing?.sequence ? `Sequence ${listing.sequence}` : "None loaded yet"}
      </Fact>
      {listing?.expires_at && (
        <Fact label="Valid until">
          {new Date(listing.expires_at * 1000).toLocaleString()}
        </Fact>
      )}
      {listing?.digest && (
        <Fact label="Index SHA-256">
          <code>{listing.digest}</code>
        </Fact>
      )}
      <Fact label="Public page">
        <a
          href="https://relay.battlelab.superstatus.io/catalogs/gallery"
          target="_blank"
          rel="noreferrer"
        >
          Open the public gallery
        </a>
      </Fact>
    </TechnicalDetails>
  );
}

/** One file, its complete content one click away in a viewer that scrolls by itself. */
function ReviewFile({
  name,
  value,
  open,
  onToggle,
}: {
  name: string;
  value: GalleryFiles[string];
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  const lines = typeof value === "string" ? value.split("\n").length : null;
  return (
    <details
      className={styles.file}
      open={open}
      onToggle={(e) => onToggle((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>
        <ChevronRight size={16} aria-hidden />
        <span>{name}</span>
        <span className={styles.state}>
          {lines !== null && (
            <span className={styles.lines}>{plural(lines, "line")}</span>
          )}
          {open ? "Hide file" : "Show file"}
        </span>
      </summary>
      <div className={styles.fileBody}>
        <FileText value={value} />
      </div>
    </details>
  );
}

/** Ask for one explicit, reviewed click; re-propose a free ID when the server says it was taken,
 *  never retrying by itself. Shared by Add and Add updated copy. */
function useConfirmedWrite({
  kind,
  suggested,
  label,
  write,
}: {
  kind: GalleryKind;
  suggested: (taken: ReadonlySet<string>) => string;
  label: string;
  write: (
    id: string,
    operation: string,
  ) => ReturnType<typeof api.importGallery>;
}) {
  const { taken, reloadTaken } = useTakenIds(kind);
  const [custom, setCustom] = useState<string | null>(null);
  const [taking, setTaking] = useState<{ was: string; next: string } | null>(
    null,
  );
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState<{ id: string; durable: boolean } | null>(
    null,
  );
  const [pending] = usePendingId();
  const id = taking?.next ?? custom ?? (taken ? suggested(taken) : null);
  const rule = idRule(kind);
  const idProblem =
    custom !== null && !rule.re.test(custom)
      ? `A local ID is ${rule.hint}`
      : custom !== null && taken?.has(custom)
        ? `“${custom}” is already used. Choose another.`
        : "";
  async function confirm() {
    if (!id) return;
    setBusy(true);
    setError("");
    let operation: string;
    try {
      operation = beginOperation(label);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
      return;
    }
    try {
      const value = await write(id, operation);
      finishOperation(value);
      if (value.state === "complete") {
        setDone({
          id: value.result?.id ?? id,
          durable: value.result?.durable !== false,
        });
        setAsking(false);
        setTaking(null);
      } else {
        // No recorded outcome: the banner above takes over, and writes stay disabled.
        setAsking(false);
      }
    } catch (e) {
      if (e instanceof ApiError && (await settleRefused(operation))) {
        // The server answered and never recorded this attempt. If the ID is now taken, propose
        // the next free one; the operator has to click again.
        const now = await reloadTaken();
        if (e.status === 409 && now?.has(id)) {
          setTaking({ was: id, next: freeId(id.replace(/-\d+$/, ""), now, kind) });
          setCustom(null);
        } else setError(errorText(e));
      } else {
        setAsking(false);
        if (!(e instanceof ApiError)) setError("");
        else setError(errorText(e));
      }
    } finally {
      setBusy(false);
    }
  }
  return {
    id,
    custom,
    setCustom: (value: string | null) => {
      setCustom(value);
      setTaking(null);
    },
    idProblem,
    rule,
    taking,
    asking,
    setAsking,
    busy,
    error,
    setError,
    done,
    pending,
    confirm,
  };
}

function Inspect({
  entry,
  listing,
  wait,
}: {
  entry: GalleryEntry;
  listing: GalleryIndex | null;
  /** Non-empty while the current check is running or failed: a kept listing is never acted on. */
  wait: string;
}) {
  const c = entry.content;
  const names = Object.keys(c.files);
  const [open, setOpen] = useState<Record<string, boolean>>(() =>
    names.length ? { [names[0]]: true } : {},
  );
  const write = useConfirmedWrite({
    kind: c.kind,
    suggested: (taken) => freeId(c.id, taken, c.kind),
    label: c.title,
    write: (id, operation) =>
      api.importGallery(c.kind, id, entry.digest, operation),
  });
  const inspectId = write.id ?? c.id;
  const read = usePlaybookRead(
    useCallback(
      () => api.inspectGallery(entry.digest, inspectId),
      [entry.digest, inspectId],
    ),
  );
  const hint = useId();
  const idHint = useId();
  const stale = listing?.state === "stale";
  const why = write.done
    ? ""
    : write.pending
      ? "Check the earlier attempt above first."
      : wait
        ? wait
        : !entry.available
          ? "Withdrawn from the gallery — view only."
          : stale
            ? STALE
            : write.idProblem ||
              (read.data && !read.data.importable
                ? `This version can't be added: ${read.data.reason ?? "it did not validate."}`
                : read.error
                  ? `This version can't be checked right now: ${read.error}`
                  : "");
  const blocked =
    !!why || !write.id || read.loading || !read.data?.importable || write.busy;
  const allOpen = names.every((n) => ownValue(open, n) === true);
  const where = NOUN[c.kind];
  return (
    <>
      <Link
        className={`${buttons.ghost} ${styles.back}`}
        to={galleryPath(c.kind)}
      >
        <ArrowLeft size={16} aria-hidden />
        Back to gallery
      </Link>
      <div className={styles.detail}>
        <article className={styles.main}>
          <header className={styles.detailHead}>
            <div className={styles.tag}>
              <KindIcon kind={c.kind} />
              {c.kind} · v{c.version}
            </div>
            <h1>{c.title}</h1>
            <p>{c.summary}</p>
            <p>
              By {c.author} · published by {c.publisher}
            </p>
            {entry.available && <Verified />}
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
              <p>Nothing extra is needed.</p>
            )}
          </section>
          {read.data?.preview && (
            <section className={styles.section} aria-label="What's inside">
              <h2>What's inside</h2>
              {!!read.data.preview.inputs.length && (
                <>
                  <h3>Inputs</h3>
                  <ul>
                    {read.data.preview.inputs.map((input) => (
                      <li key={input.name}>
                        {input.label ?? input.name} ·{" "}
                        {input.required ? "required" : "optional"}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {read.data.preview.flows?.map((flow) => (
                <div key={flow.id}>
                  <h3>{flow.title}</h3>
                  <ol>
                    {flow.steps.map((step) => (
                      <li key={step.id}>
                        {step.title} ·{" "}
                        {roleText(step) ??
                          step.actor.engine ??
                          step.actor.label ??
                          step.actor.kind}
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
          <section className={styles.section} aria-label="Files">
            <div className={styles.sectionHead}>
              <h2>Files</h2>
              {names.length > 1 && (
                <button
                  className={buttons.ghost}
                  onClick={() =>
                    setOpen(Object.fromEntries(names.map((n) => [n, !allOpen])))
                  }
                >
                  {allOpen ? "Collapse all" : "Expand all"}
                </button>
              )}
            </div>
            {names.map((name) => (
              <ReviewFile
                key={name}
                name={name}
                value={c.files[name]}
                open={ownValue(open, name) === true}
                onToggle={(value) =>
                  setOpen((old) => ({ ...old, [name]: value }))
                }
              />
            ))}
          </section>
        </article>
        <aside className={styles.side} aria-label="Add">
          <LeaveGuard dirty={write.busy} />
          {write.done ? (
            <p role="status" className={styles.done}>
              Added — <Link to={localPath(c.kind, write.done.id)}>Open</Link>
              {!write.done.durable &&
                " The disk didn't confirm the save, so check it before relying on it."}
            </p>
          ) : (
            <>
              <button
                className={`${buttons.primary} ${styles.wide}`}
                aria-describedby={why ? hint : undefined}
                disabled={blocked}
                data-testid="gallery-add"
                onClick={() => {
                  write.setError("");
                  write.setAsking(true);
                }}
              >
                <Plus size={16} aria-hidden />
                Add to my {where}
              </button>
              {why ? (
                <p className={styles.why} id={hint}>
                  {why}
                </p>
              ) : (
                write.id && (
                  <p className={styles.why}>
                    Adds as <code>{write.id}</code>.
                  </p>
                )
              )}
              {write.error && !write.asking && (
                <p className={styles.error} role="alert">
                  {write.error}
                </p>
              )}
            </>
          )}
        </aside>
        <div className={styles.tech}>
          <TechnicalDetails>
            <Fact label="Content SHA-256">
              <code>{entry.digest}</code>
            </Fact>
            {listing?.sequence && (
              <Fact label="Signed publication">
                Sequence {listing.sequence}
                {listing.expires_at
                  ? ` · valid until ${new Date(listing.expires_at * 1000).toLocaleString()}`
                  : ""}
              </Fact>
            )}
            <Fact label="Source">
              <a href={c.source} target="_blank" rel="noreferrer">
                {c.source}
              </a>
            </Fact>
            {c.kind === "playbook" && (
              <Fact label="Playbook ID">
                Only the local playbook ID is rewritten; workflows keep their
                identities.
              </Fact>
            )}
            {!write.done && (
              <>
                <label className={styles.field}>
                  Local ID
                  <input
                    disabled={write.busy}
                    value={write.custom ?? write.id ?? ""}
                    pattern={write.rule.pattern}
                    aria-describedby={idHint}
                    onChange={(e) => write.setCustom(e.target.value)}
                  />
                </label>
                <p className={styles.why} id={idHint}>
                  {write.rule.hint}
                </p>
              </>
            )}
          </TechnicalDetails>
        </div>
      </div>
      {write.asking && write.id && (
        <PlaybookDialog
          tag={`Gallery // add`}
          title={`Add ${c.title} to your ${where}?`}
          confirmLabel={
            write.taking
              ? `I've reviewed it — Add as ${write.id}`
              : "I've reviewed it — Add"
          }
          busy={write.busy}
          // The gate is checked again here: the listing can go stale while the dialog is open.
          confirmDisabled={!!wait}
          onCancel={() => write.setAsking(false)}
          onConfirm={() => {
            if (!wait) void write.confirm();
          }}
        >
          {wait && (
            <p
              className={`${styles.notice} ${styles.warn}`}
              role="status"
              data-testid="gallery-confirm-wait"
            >
              {wait}
            </p>
          )}
          <p>
            Only add it if you've checked its requirements and files. It's
            copied into your library as editable files; nothing runs until you
            use it.
          </p>
          {write.taking ? (
            <p
              className={`${styles.notice} ${styles.warn}`}
              data-testid="gallery-id-taken"
            >
              <code>{write.taking.was}</code> was just taken. It will be added
              as <code>{write.taking.next}</code> instead.
            </p>
          ) : (
            <p>
              Adds as <code>{write.id}</code>.
            </p>
          )}
          {write.error && (
            <p className={styles.error} role="alert">
              {write.error}
            </p>
          )}
        </PlaybookDialog>
      )}
    </>
  );
}

function Compare({
  entry,
  id,
  wait,
}: {
  entry: GalleryEntry;
  id: string;
  wait: string;
}) {
  const kind = entry.content.kind;
  const read = usePlaybookRead(
    useCallback(
      () => api.compareGallery(kind, id, entry.digest),
      [kind, id, entry.digest],
    ),
  );
  if (!read.data)
    return (
      <p
        role={read.error ? "alert" : "status"}
        className={read.error ? styles.error : styles.notice}
      >
        {read.error ?? "Comparing your copy with the gallery version…"}
      </p>
    );
  return (
    <Merge
      initial={read.data}
      title={entry.content.title}
      reload={read.reload}
      wait={wait}
    />
  );
}

const CHOICES = [
  ["local", "Your edit"],
  ["incoming", "Gallery version"],
  ["base", "Original"],
] as const;

function Merge({
  initial,
  title,
  reload,
  wait,
}: {
  initial: GalleryComparison;
  title: string;
  reload: () => Promise<GalleryComparison | null>;
  wait: string;
}) {
  const [view, setView] = useState(initial);
  const [files, setFiles] = useState<GalleryFiles>(() =>
    structuredClone(initial.proposed),
  );
  const [unresolved, setUnresolved] = useState(new Set(initial.conflicts));
  /** Files that need the operator: real conflicts, plus any retained draft difference after a
   *  recompare. It never shrinks when a choice is made, so a choice can be changed. */
  const [contested, setContested] = useState(new Set(initial.conflicts));
  const [dirty, setDirty] = useState(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const write = useConfirmedWrite({
    kind: view.kind,
    suggested: (taken) =>
      updateId(view.id, view.incoming_version, taken, view.kind),
    label: `the updated ${title}`,
    write: (newId, operation) =>
      api.updateGallery(
        view.kind,
        view.id,
        newId,
        view.incoming_digest,
        view.comparison,
        files,
        operation,
      ),
  });
  const hint = useId();
  const idHint = useId();
  /** While "Compare again" runs, the draft is frozen: the new comparison is measured against it. */
  const [comparing, setComparing] = useState(false);
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
  }
  const retainedDifferences = (proposal: GalleryFiles) =>
    [...new Set([...Object.keys(files), ...Object.keys(proposal)])].filter(
      (name) =>
        JSON.stringify(ownValue(files, name)) !==
        JSON.stringify(ownValue(proposal, name)),
    );
  const reviewedFiles = [
    ...new Set([
      ...view.changed_files,
      ...retainedDifferences(view.proposed),
      ...contested,
    ]),
  ].sort();
  const quiet = reviewedFiles.filter((name) => !contested.has(name));
  const loud = reviewedFiles.filter((name) => contested.has(name));
  const same = (a: unknown, b: unknown) =>
    JSON.stringify(a) === JSON.stringify(b);
  const firstUnresolved = [...unresolved].sort()[0];
  const why = write.done
    ? ""
    : write.pending
      ? "Check the earlier attempt above first."
      : wait
        ? wait
        : unresolved.size === 1
          ? `Choose a result for ${firstUnresolved} first.`
          : unresolved.size
            ? `Choose a result for ${plural(unresolved.size, "file")} first.`
            : write.idProblem ||
              (write.id === view.id
                ? "Choose an ID different from your current copy."
                : "");
  const where = NOUN[view.kind];
  return (
    <>
      <LeaveGuard dirty={(dirty && !write.done) || write.busy} />
      <Link
        className={`${buttons.ghost} ${styles.back}`}
        to={localPath(view.kind, view.id)}
      >
        <ArrowLeft size={16} aria-hidden />
        Back to {view.id}
      </Link>
      <header className={styles.detailHead}>
        <div className={styles.tag}>
          <GitCompareArrows size={14} aria-hidden />
          Update
        </div>
        <h1>Review update</h1>
        <p className={styles.versions}>
          {view.base_version ? `v${view.base_version} → ` : ""}v
          {view.incoming_version}
        </p>
        <p>
          For your copy{" "}
          <Link to={localPath(view.kind, view.id)}>{view.id}</Link>. The update
          is added as a new copy; your current copy stays as it is.
        </p>
      </header>
      <p className={styles.summary} data-testid="gallery-update-summary">
        {loud.length ? (
          <>
            <span className="hud-led attention" aria-hidden />
            {loud.length === 1
              ? "1 file conflicts with your edits — choose a result"
              : `${loud.length} files conflict with your edits — choose a result for each`}
          </>
        ) : reviewedFiles.length ? (
          `${plural(reviewedFiles.length, "file")} change${reviewedFiles.length === 1 ? "s" : ""}, none conflict with your edits`
        ) : (
          "Nothing changes in the files."
        )}
      </p>
      {write.error && (
        <p role="alert" className={styles.error}>
          {write.error} Your choices are kept.
        </p>
      )}
      {loud.map((name) => {
        const result = ownValue(files, name);
        const sources = {
          local: ownValue(view.local, name),
          incoming: ownValue(view.incoming, name),
          base: ownValue(view.base, name),
        };
        const using = unresolved.has(name)
          ? null
          : (CHOICES.find(([key]) => same(result, sources[key]))?.[0] ??
            "edited");
        // A draft retained across "Compare again" can match none of the three versions; keeping
        // it must stay one explicit click, as typing into the result does.
        const ownResult =
          result !== undefined &&
          !CHOICES.some(([key]) => same(result, sources[key]));
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
              ) : using === "edited" ? (
                "using your edited result"
              ) : (
                `using ${CHOICES.find(([key]) => key === using)![1].toLowerCase()}`
              )
            }
          >
            <div className={styles.compare}>
              {CHOICES.map(([key, label]) => (
                <div key={key}>
                  <h3>{label}</h3>
                  <FileText value={sources[key]} />
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
                {CHOICES.map(([key, label]) => (
                  <button
                    key={key}
                    className={buttons.ghost}
                    aria-pressed={using === key}
                    disabled={write.busy || comparing || !!write.done}
                    onClick={() => choose(name, sources[key])}
                  >
                    {label}
                  </button>
                ))}
                {ownResult && (
                  <button
                    className={buttons.ghost}
                    aria-pressed={using === "edited"}
                    disabled={write.busy || comparing || !!write.done}
                    onClick={() => choose(name, result)}
                  >
                    Keep my result
                  </button>
                )}
              </div>
            </div>
            {typeof result === "string" && (
              <label className={styles.field}>
                Result for {name} (you can edit it)
                <textarea
                  disabled={write.busy || comparing || !!write.done}
                  value={result}
                  spellCheck={false}
                  onChange={(e) => choose(name, e.target.value)}
                />
              </label>
            )}
            {using !== null && result === undefined && (
              <p>This file will be removed.</p>
            )}
          </FileRow>
        );
      })}
      {!!quiet.length && (
        <details
          className={`${styles.file} ${styles.quiet}`}
          data-testid="gallery-quiet-files"
        >
          <summary>
            <ChevronRight size={16} aria-hidden />
            <span>
              {loud.length
                ? `${plural(quiet.length, "other file")} change${quiet.length === 1 ? "s" : ""} — no conflicts`
                : `Show the ${plural(quiet.length, "changed file")}`}
            </span>
          </summary>
          <div className={styles.fileBody}>
            {quiet.map((name) => {
              const result = ownValue(files, name);
              return (
                <FileRow
                  key={name}
                  name={name}
                  state={result === undefined ? "removed" : undefined}
                >
                  <FileText value={result} />
                </FileRow>
              );
            })}
          </div>
        </details>
      )}
      <TechnicalDetails>
        <Fact label="Comparison">
          <code>{view.comparison}</code>
        </Fact>
        <Fact label="Incoming content SHA-256">
          <code>{view.incoming_digest}</code>
        </Fact>
        <Fact label="Your copy's revision">
          <code>{view.revision}</code>
        </Fact>
        {!write.done && (
          <>
            <label className={styles.field}>
              New copy ID
              <input
                disabled={write.busy}
                value={write.custom ?? write.id ?? ""}
                pattern={write.rule.pattern}
                aria-describedby={idHint}
                onChange={(e) => write.setCustom(e.target.value)}
              />
            </label>
            <p className={styles.why} id={idHint}>
              {write.rule.hint}
            </p>
          </>
        )}
      </TechnicalDetails>
      <div className={styles.savebar} data-testid="gallery-savebar">
        {write.done ? (
          <p role="status" className={`${styles.why} ${styles.ready}`}>
            Added the updated copy —{" "}
            <Link to={localPath(view.kind, write.done.id)}>Open</Link>. Your
            original is unchanged.
            {!write.done.durable &&
              " The disk didn't confirm the save, so check it before relying on it."}
          </p>
        ) : (
          <p className={`${styles.why} ${why ? "" : styles.ready}`} id={hint}>
            {why || (write.id ? `Adds as ${write.id}.` : "")}
          </p>
        )}
        <div className={styles.actions}>
          <button
            className={buttons.ghost}
            disabled={write.busy || comparing}
            onClick={async () => {
              setComparing(true);
              const next = await reload();
              if (!live.current) return;
              setComparing(false);
              if (!next) {
                write.setError(
                  "Your copy couldn't be compared again. Try again.",
                );
                return;
              }
              const differing = retainedDifferences(next.proposed);
              setView(next);
              setUnresolved(new Set([...next.conflicts, ...differing]));
              setContested(new Set([...next.conflicts, ...differing]));
              write.setError("");
            }}
          >
            {comparing ? "Comparing…" : "Compare again"}
          </button>
          {!write.done && (
            <button
              className={buttons.primary}
              aria-describedby={why ? hint : undefined}
              disabled={!!why || !write.id || write.busy || comparing}
              onClick={() => {
                write.setError("");
                write.setAsking(true);
              }}
            >
              <Plus size={16} aria-hidden />
              Add updated copy
            </button>
          )}
        </div>
      </div>
      {write.asking && write.id && (
        <PlaybookDialog
          tag="Gallery // update"
          title="Add the updated copy?"
          confirmLabel={
            write.taking
              ? `I've reviewed it — Add as ${write.id}`
              : "I've reviewed it — Add"
          }
          busy={write.busy}
          // The gate is checked again here: the listing can go stale while the dialog is open.
          confirmDisabled={!!wait}
          onCancel={() => write.setAsking(false)}
          onConfirm={() => {
            if (!wait) void write.confirm();
          }}
        >
          {wait && (
            <p
              className={`${styles.notice} ${styles.warn}`}
              role="status"
              data-testid="gallery-confirm-wait"
            >
              {wait}
            </p>
          )}
          <p>
            Only add it if you've checked the changes above. It's added to your{" "}
            {where} as a new copy; <code>{view.id}</code> stays as it is.
          </p>
          {write.taking ? (
            <p
              className={`${styles.notice} ${styles.warn}`}
              data-testid="gallery-id-taken"
            >
              <code>{write.taking.was}</code> was just taken. It will be added
              as <code>{write.taking.next}</code> instead.
            </p>
          ) : (
            <p>
              Adds as <code>{write.id}</code>.
            </p>
          )}
          {write.error && (
            <p className={styles.error} role="alert">
              {write.error}
            </p>
          )}
        </PlaybookDialog>
      )}
    </>
  );
}
