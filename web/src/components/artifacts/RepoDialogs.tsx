/** The repository dialogs (#2347 P1): New repository, and "Add an artifact…" on a repository's
 *  page. Both are the artifacts dialog chrome (`ArtifactDialog`: modal, focus contained and returned,
 *  a bottom sheet at ≤800px). A refusal shows the server's own words and keeps the dialog open. */
import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { Search } from "lucide-react";

import { ApiError, api } from "../../lib/api";
import type {
  Artifact,
  ArtifactRepo,
  ArtifactRepoItem,
} from "../../types/artifacts";
import { ArtifactDialog } from "./ArtifactDialog";
import { KindTag } from "./ArtifactBits";
import { formatSize, revLabel } from "./artifactModel";
import { REPO_NAME_HINT } from "./repoModel";
import styles from "./artifacts.module.css";

const message = (e: unknown) =>
  e instanceof ApiError || e instanceof Error
    ? e.message
    : "The request failed. Try again.";

export function NewRepoDialog({
  onClose,
  onCreated,
  returnFocusTo,
}: {
  onClose: () => void;
  onCreated: (repo: ArtifactRepo) => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const formId = useId();
  const hintId = useId();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !name.trim()) return;
    setBusy(true);
    setError("");
    try {
      onCreated(await api.createArtifactRepo(name.trim(), description.trim()));
    } catch (err) {
      setError(message(err));
      setBusy(false);
    }
  };
  return (
    <ArtifactDialog
      tag="New repository"
      title="A named collection of artifacts, hosted on this BattleLab"
      onClose={onClose}
      returnFocusTo={returnFocusTo}
      testId="repo-new-dialog"
      actions={
        <>
          <button type="button" className={styles.ghost} onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            form={formId}
            className={styles.primary}
            disabled={busy || !name.trim()}
          >
            {busy ? "Creating…" : "Create repository"}
          </button>
        </>
      }
    >
      <form id={formId} className={styles.repoForm} onSubmit={(e) => void submit(e)}>
        <label className={styles.fieldLbl}>
          Name
          <input
            className={styles.optInput}
            value={name}
            maxLength={64}
            autoComplete="off"
            spellCheck={false}
            aria-describedby={hintId}
            onChange={(e) => setName(e.target.value)}
            placeholder="design-assets"
          />
        </label>
        <p id={hintId} className={styles.note}>
          {REPO_NAME_HINT}
        </p>
        <label className={styles.fieldLbl}>
          Description (optional)
          <input
            className={styles.optInput}
            value={description}
            maxLength={300}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <p className={styles.note}>
          Adding an artifact copies no bytes: the repository keeps that revision even after the
          artifact itself is deleted. It holds up to 2 GB.
        </p>
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
      </form>
    </ArtifactDialog>
  );
}

/** Pick one of the operator's artifacts (the server searches), and add its latest revision as a
 *  new item or as a new revision of an item already in the repository. */
export function AddArtifactDialog({
  repo,
  items,
  onClose,
  onAdded,
  returnFocusTo,
}: {
  repo: ArtifactRepo;
  items: ArtifactRepoItem[];
  onClose: () => void;
  onAdded: (item: ArtifactRepoItem) => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<Artifact[] | null>(null);
  const [pick, setPick] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const groupName = useId();

  useEffect(() => {
    const t = window.setTimeout(() => setQuery(q.trim()), 250);
    return () => window.clearTimeout(t);
  }, [q]);
  useEffect(() => {
    const ctl = new AbortController();
    api
      .artifacts(query ? { q: query } : {}, ctl.signal)
      .then((page) => setFound(page.items))
      .catch((e: unknown) => {
        if (!ctl.signal.aborted) setError(message(e));
      });
    return () => ctl.abort();
  }, [query]);

  const chosen = useMemo(
    () => found?.find((a) => a.id === pick) ?? null,
    [found, pick],
  );
  // A new revision keeps its item's kind: only items of the chosen artifact's kind are offered.
  const targets = useMemo(
    () => (chosen ? items.filter((i) => i.kind === chosen.kind) : []),
    [chosen, items],
  );
  // A target the newly chosen artifact cannot revise (another kind) reads as "a new item".
  const into = targets.some((i) => i.id === target) ? target : "";

  const add = async () => {
    if (!chosen || busy) return;
    setBusy(true);
    setError("");
    try {
      onAdded(await api.addToArtifactRepo(repo.id, chosen.id, into || undefined));
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ArtifactDialog
      tag="Add an artifact"
      value={repo.name}
      title={`Add one of your artifacts to “${repo.name}”`}
      onClose={onClose}
      returnFocusTo={returnFocusTo}
      testId="repo-add-dialog"
      actions={
        <>
          <button type="button" className={styles.ghost} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className={styles.primary}
            disabled={!chosen || busy}
            onClick={() => void add()}
          >
            {busy ? "Adding…" : "Add to repository"}
          </button>
        </>
      }
    >
      <label className={styles.search}>
        <Search size={14} className={styles.searchIcon} aria-hidden="true" />
        <input
          className="nj-field"
          type="search"
          placeholder="Search your artifacts…"
          aria-label="Search your artifacts"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </label>
      <div
        className={styles.pickList}
        role="radiogroup"
        aria-label="Artifact to add"
        data-testid="repo-add-list"
      >
        {found === null && !error ? (
          <p className={styles.note}>Reading your artifacts…</p>
        ) : found && found.length === 0 ? (
          <p className={styles.note}>
            {query ? "No artifact matches." : "You have no artifacts yet."}
          </p>
        ) : (
          (found ?? []).map((a) => (
            <label key={a.id} className={styles.pickRow} data-pick={a.id}>
              <input
                type="radio"
                name={groupName}
                checked={pick === a.id}
                onChange={() => setPick(a.id)}
              />
              <KindTag kind={a.kind} entry={a.entry} />
              <span className={styles.pickTitle}>{a.title}</span>
              <span className={styles.pickMeta}>
                {revLabel(a.revision)} · {formatSize(a.bytes)}
              </span>
            </label>
          ))
        )}
      </div>
      {chosen && (
        <label className={styles.fieldLbl}>
          Add as
          <select
            className={`nj-field ${styles.select} ${styles.optInput}`}
            value={into}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="">A new item</option>
            {targets.map((i) => (
              <option key={i.id} value={i.id}>
                A new revision of “{i.title}” ({revLabel(i.revision)})
              </option>
            ))}
          </select>
        </label>
      )}
      <p className={styles.note}>
        Its latest revision is added; no bytes are copied.
      </p>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </ArtifactDialog>
  );
}
