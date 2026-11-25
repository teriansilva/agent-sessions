import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Copy, GalleryHorizontal, Pencil, Plus } from "lucide-react";
import { api } from "../lib/api";
import { SKILL_LOCAL_ID } from "../components/gallery/galleryRules";
import { GALLERY_SKILLS_PATH, SKILLS_PATH } from "../lib/routes";
import { LandingNote, ViewTabs } from "../components/gallery/ViewTabs";
import type { LocalSkill } from "../types/gallery";
import { usePlaybookRead } from "../components/playbooks/usePlaybooks";
import { ImportedMark } from "../components/share/ShareDialogs";
import { useExportEntry, useImportEntry } from "../components/share/useLibraryShare";
import {
  CardExport,
  Count,
  Dot,
  LibraryCard,
  type LibrarySource,
} from "../components/library/LibraryCard";
import {
  ActionBar,
  Fact,
  LibraryDetail,
  MoreMenu,
  SourceFact,
} from "../components/library/LibraryDetail";
import { LibraryGrid, LibraryHead, LibraryToolbar, type Facet } from "../components/library/LibraryGrid";
import lib from "../components/library/library.module.css";
import { useShareImports } from "../components/share/useShareImports";
import {
  FileRow,
  LeaveGuard,
  Origin,
  PendingOperation,
  RepoOrigin,
} from "../components/gallery/Shared";
import { beginOperation, finishOperation, errorText, ownValue } from "../components/gallery/operations";
import buttons from "../components/ui/actionButton.module.css";
import styles from "../components/gallery/gallery.module.css";

export default function SkillsLibrary() {
  const { skillId } = useParams();
  return (
    <main className={styles.page} data-testid="skills-page">
      <LandingNote />
      <PendingOperation />
      {skillId ? <Detail key={skillId} id={skillId} /> : <List />}
    </main>
  );
}

type Show = "all" | LibrarySource;
const FACETS: { value: Show; label: string }[] = [
  { value: "all", label: "All" },
  { value: "local", label: "Local" },
  { value: "imported", label: "Imported" },
  { value: "gallery", label: "Gallery" },
];

/** What a local skill copy IS: from the public gallery (its recorded origin), a file (the
 *  share-import ledger) or this host. */
function skillSource(skill: LocalSkill, fromFile: boolean): LibrarySource {
  if (skill.gallery_origin) return "gallery";
  return fromFile ? "imported" : "local";
}

/** Size of a skill's files as text. */
function sizeText(files: Record<string, string>): string {
  const bytes = Object.values(files).reduce((n, v) => n + new Blob([v]).size, 0);
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

function skillProvenance(skill: LocalSkill, source: LibrarySource) {
  if (source === "gallery")
    return (
      <>
        <GalleryHorizontal size={14} aria-hidden />
        From the gallery
        {skill.gallery_origin?.publisher && (
          <>
            {" · "}
            <b>{skill.gallery_origin.publisher}</b>
          </>
        )}
      </>
    );
  if (source === "imported") return <ImportedMark />;
  // Copied by HQ from a repository link (#2145): display-only, never verified.
  if (skill.repo_origin) return "From a repository (unverified)";
  return "Local copy · no gallery origin";
}

/** One local skill on the Library grid (#2144). The list carries no summary or version of its
 *  own: the title is the gallery origin's (else the ID), and the meta row is what the list has. */
function SkillCard({ skill, source }: { skill: LocalSkill; source: LibrarySource }) {
  const title = skill.gallery_origin?.title ?? skill.id;
  const files = Object.keys(skill.files ?? {}).length;
  return (
    <LibraryCard
      testId={`skill-card-${skill.id}`}
      to={skill.error ? undefined : `${SKILLS_PATH}/${encodeURIComponent(skill.id)}`}
      title={title}
      kind="Skill"
      version={skill.gallery_origin?.version}
      source={source}
      invalid={!!skill.error}
      meta={
        <>
          {title !== skill.id && (
            <>
              <span>{skill.id}</span>
              <Dot />
            </>
          )}
          <Count n={files} one="file" />
          <Dot />
          <b>{sizeText(skill.files ?? {})}</b>
          {skill.revision && (
            <>
              <Dot />
              <span>rev {skill.revision.slice(0, 7)}</span>
            </>
          )}
        </>
      }
      provenance={skillProvenance(skill, source)}
      actions={skill.error ? undefined : <CardExport kind="skill" id={skill.id} title={title} />}
    >
      {skill.error && (
        <p role="note" className={lib.note}>
          {skill.error}
        </p>
      )}
    </LibraryCard>
  );
}

function List() {
  const read = usePlaybookRead(useCallback(() => api.localSkills(), []));
  const [round, setRound] = useState(0);
  const imported = useShareImports(round);
  const [search, setSearch] = useState("");
  const [show, setShow] = useState<Show>("all");
  const importer = useImportEntry(() => {
    setRound((n) => n + 1);
    void read.reload();
  });
  const rows = (read.data?.skills ?? []).map((skill) => ({
    skill,
    source: skillSource(skill, imported.has(`skill:${skill.id}`)),
  }));
  const needle = search.trim().toLowerCase();
  const matching = rows.filter(({ skill }) =>
    [skill.id, skill.gallery_origin?.title, ...Object.keys(skill.files ?? {})]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(needle),
  );
  const shown = matching.filter((r) => show === "all" || r.source === show);
  const facets: Facet<Show>[] = FACETS.map((f) => ({
    ...f,
    n: f.value === "all" ? matching.length : matching.filter((r) => r.source === f.value).length,
  })).filter((f) => f.value === "all" || f.value === show || f.n > 0);
  return (
    <>
      {/* No header primary and no New tile: creating a skill is HQ's (#1611), not a UI editor. */}
      <LibraryHead section="skills" title="Skills" count={read.data ? rows.length : undefined} />
      <ViewTabs kind="skill" view="local" />
      <p className={lib.intro}>
        Editable instruction files kept in your BattleLab library. Saving here does not activate a
        skill in an agent.
      </p>
      <LibraryToolbar
        label="Search skills"
        placeholder="Name or file…"
        search={search}
        onSearch={setSearch}
        facets={read.data ? facets : undefined}
        facet={show}
        onFacet={setShow}
        count={read.data ? `${shown.length} ${shown.length === 1 ? "skill" : "skills"}` : undefined}
      />
      {read.error && (
        <p role="alert" className={styles.error}>
          {read.error}
        </p>
      )}
      {read.loading && !read.data && <p role="status">Loading local skills…</p>}
      {read.data && !rows.length && (
        <section className={styles.empty}>
          <h2>No skills yet</h2>
          <p>
            Skills you add from the <Link to={GALLERY_SKILLS_PATH}>public gallery</Link>, import
            from a file or draft with HQ appear here.
          </p>
        </section>
      )}
      {read.data && !!rows.length && !shown.length && (
        <section className={styles.empty}>
          <p>No skills match.</p>
          <button
            className={buttons.ghost}
            onClick={() => {
              setSearch("");
              setShow("all");
            }}
          >
            Clear filters
          </button>
        </section>
      )}
      {(read.data || read.error) && (
        <LibraryGrid
          testId="skill-grid"
          onImport={importer.open}
          importHint="A .battlelab file someone shared with you. Skills also arrive from the public gallery and from HQ."
        >
          {shown.map(({ skill, source }) => (
            <SkillCard key={skill.id} skill={skill} source={source} />
          ))}
        </LibraryGrid>
      )}
      {importer.dialog}
    </>
  );
}
function Detail({ id }: { id: string }) {
  const read = usePlaybookRead(useCallback(() => api.localSkill(id), [id]));
  const [round, setRound] = useState(0);
  const imported = useShareImports(round);
  // Import… here makes NEW copies only, so this skill is unchanged; the list re-reads on return.
  const importer = useImportEntry(() => setRound((n) => n + 1));
  if (!read.data)
    return (
      <>
        <Link className={lib.back} to={SKILLS_PATH}>
          <ArrowLeft size={16} aria-hidden />
          All skills
        </Link>
        <p
          role={read.error ? "alert" : "status"}
          className={read.error ? styles.error : undefined}
        >
          {read.error ?? "Loading skill…"}
        </p>
      </>
    );
  return (
    <>
      <Editor
        initial={read.data}
        reload={read.reload}
        source={skillSource(read.data, imported.has(`skill:${read.data.id}`))}
        onImport={importer.open}
      />
      {importer.dialog}
    </>
  );
}

/** The skill detail's ⋯ (#2144): Duplicate saves an unchanged copy under a free ID. */
function SkillMenu({ title, onDuplicate, held }: { title: string; onDuplicate: () => void; held: boolean }) {
  return (
    <MoreMenu
      title={title}
      items={[
        {
          key: "duplicate",
          label: "Duplicate",
          icon: <Copy size={16} aria-hidden />,
          disabled: held,
          onSelect: onDuplicate,
        },
      ]}
    />
  );
}

function Editor({
  initial,
  reload,
  source,
  onImport,
}: {
  initial: LocalSkill;
  reload: () => Promise<LocalSkill | null>;
  source: LibrarySource;
  onImport: () => void;
}) {
  const navigate = useNavigate();
  const exporter = useExportEntry("skill");
  const released = useRef(false);
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
  const idOk = SKILL_LOCAL_ID.test(newId);
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
  const title = base.gallery_origin?.title ?? base.id;
  const duplicate = async () => {
    setBusy(true);
    setError("");
    try {
      const taken = new Set((await api.localSkills()).skills.map((s) => s.id));
      const stem = `${base.id.slice(0, 43)}-copy`;
      let copyId = stem;
      for (let n = 2; taken.has(copyId); n += 1) copyId = `${stem}-${n}`;
      const operation = beginOperation();
      const value = await api.saveLocalSkill(
        base.id, copyId, base.revision, base.gallery_origin?.pointer ?? null, base.files, operation,
      );
      finishOperation(value);
      if (!live.current) return;
      if (value.state === "complete" && value.result) {
        // The copy is saved and nothing here is unsaved: open it without the leave question,
        // even though `busy` is still true in the guard's last render.
        released.current = true;
        navigate(`${SKILLS_PATH}/${encodeURIComponent(value.result.id)}`);
      }
      else setError(value.message ?? "Check status above before retrying.");
    } catch (e) {
      if (live.current) setError(errorText(e));
    } finally {
      if (live.current) setBusy(false);
    }
  };
  return (
    <LibraryDetail
      testId="skill-detail"
      back={{ to: SKILLS_PATH, label: "All skills" }}
      kicker="skills"
      kind="Skill"
      version={base.gallery_origin?.version}
      source={source}
      title={title}
      by={title !== base.id ? <code>{base.id}</code> : undefined}
      bar={
        <ActionBar
          primary={
            <button
              type="button"
              className={buttons.primary}
              disabled={busy || !!done}
              onClick={() => {
                const draft = document.querySelector<HTMLTextAreaElement>(
                  "[data-testid='skill-detail'] textarea",
                );
                draft?.closest("details")?.setAttribute("open", "");
                draft?.focus();
              }}
            >
              <Pencil size={16} aria-hidden />
              Edit
            </button>
          }
          onExport={() => exporter.open(base.id)}
          onImport={onImport}
        >
          <SkillMenu title={title} held={busy || dirty || base.gallery_origin?.state === "unavailable"} onDuplicate={() => void duplicate()} />
        </ActionBar>
      }
      facts={
        <>
          <Fact label="Version">
            {base.gallery_origin?.version ? `v${base.gallery_origin.version}` : "Unversioned"}
            <span className={lib.small}>revision {base.revision.slice(0, 7)}</span>
          </Fact>
          <SourceFact word={skillProvenance(base, source)} />
          <Fact label={`Files · ${Object.keys(base.files).length}`}>
            {sizeText(base.files)}
            <span className={lib.small}>Saving here doesn't turn the skill on in any agent.</span>
          </Fact>
        </>
      }
    >
      <LeaveGuard dirty={dirty || busy} released={released} />
      <Origin
        value={base.gallery_origin}
        kind="skill"
        id={base.id}
        revision={base.revision}
      />
      <RepoOrigin value={base.repo_origin} />
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
              pattern="[a-z0-9][a-z0-9-]{0,63}"
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
      {exporter.dialog}
    </LibraryDetail>
  );
}
