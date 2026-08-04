import { ChevronRight, FolderPlus, House } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { FsDir } from "../types/api";
import styles from "./FolderPickerModal.module.css";

/** Pick a folder from the filesystem, rooted at `~/` (#448). Browse by navigating into
 *  subdirectories (breadcrumb to jump back up), `Select this folder` returns the current path, and
 *  `New folder` creates one under the current dir. Used by the new-session folder override and the
 *  Settings default-folder picker. The server (`/api/fs/*`) is the security boundary — every path
 *  is realpath-contained under home; this component only ever shows what the server returns.
 *
 *  Accessibility mirrors MoveToProjectModal: `role="dialog"`, `aria-modal`, labelled title, focus
 *  moves in on open + returns to the trigger on close, Esc + backdrop cancel. */
export function FolderPickerModal({
  initialPath,
  title = "Choose a folder",
  onPick,
  onCancel,
  returnFocusTo,
}: {
  initialPath?: string;
  title?: string;
  onPick: (path: string) => void;
  onCancel: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [home, setHome] = useState("");
  const [cwd, setCwd] = useState("");
  const [dirs, setDirs] = useState<FsDir[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const firstRef = useRef<HTMLButtonElement>(null);

  const browse = useCallback((path?: string) => {
    setDirs(null);
    setError(null);
    api
      .fsDirs(path)
      .then((r) => {
        setHome(r.home);
        setCwd(r.path);
        setDirs(r.dirs);
      })
      .catch((e) =>
        setError(
          e instanceof ApiError && e.message
            ? e.message
            : "Couldn’t read that folder.",
        ),
      );
  }, []);

  // Initial browse (server resolves an empty/undefined path to home). The synchronous loading
  // reset inside browse() is the intended on-open behaviour, not a cascading-render bug.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    browse(initialPath);
  }, [browse, initialPath]);
  // Focus the first actionable control once the listing lands.
  useEffect(() => {
    if (dirs) firstRef.current?.focus();
  }, [dirs]);
  // Restore focus to the trigger on unmount + global Esc to cancel.
  useEffect(() => () => returnFocusTo?.focus?.(), [returnFocusTo]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  // Breadcrumb: home + each path segment below it, each clickable to jump up.
  const rel =
    home && cwd.startsWith(home)
      ? cwd.slice(home.length).replace(/^\//, "")
      : "";
  const segs = rel ? rel.split("/") : [];
  const segPath = (i: number) => [home, ...segs.slice(0, i + 1)].join("/");

  const createFolder = async () => {
    const name = newName.trim();
    if (!name || creating) return;
    setCreating(true);
    setError(null);
    try {
      const { path } = await api.fsMkdir(cwd, name);
      setNewName("");
      browse(path); // navigate into the freshly-created folder so Select picks it
    } catch (e) {
      setError(
        e instanceof ApiError && e.message
          ? e.message
          : "Couldn’t create that folder.",
      );
    } finally {
      setCreating(false);
    }
  };

  const titleId = "folder-picker-title";
  return (
    <div className={styles.backdrop} onMouseDown={onCancel}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={styles.dialog}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h3 id={titleId} className={styles.title}>
          {title}
        </h3>

        <div className={styles.crumbs} aria-label="Current folder">
          <button
            type="button"
            ref={firstRef}
            className={styles.crumb}
            onClick={() => browse(home)}
            aria-label="Home"
          >
            <House size={13} aria-hidden="true" /> ~
          </button>
          {segs.map((s, i) => (
            <span key={segPath(i)} className={styles.crumbSeg}>
              <ChevronRight
                size={12}
                aria-hidden="true"
                className={styles.sep}
              />
              <button
                type="button"
                className={styles.crumb}
                onClick={() => browse(segPath(i))}
              >
                {s}
              </button>
            </span>
          ))}
        </div>

        {error && <p className={styles.error}>{error}</p>}
        {dirs === null ? (
          <p className={styles.empty}>Loading…</p>
        ) : dirs.length === 0 ? (
          <p className={styles.empty}>No subfolders here.</p>
        ) : (
          <ul className={styles.list}>
            {dirs.map((d) => (
              <li key={d.path}>
                <button
                  type="button"
                  className={styles.row}
                  onClick={() => browse(d.path)}
                  title={`Open ${d.name}`}
                >
                  <span className={styles.name}>{d.name}</span>
                  <ChevronRight
                    size={14}
                    aria-hidden="true"
                    className={styles.chev}
                  />
                </button>
              </li>
            ))}
          </ul>
        )}

        <form
          className={styles.mkrow}
          onSubmit={(e) => {
            e.preventDefault();
            void createFolder();
          }}
        >
          <input
            type="text"
            className={styles.mkInput}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="new folder name"
            aria-label="New folder name"
            disabled={creating || dirs === null}
          />
          <button
            type="submit"
            className={styles.mkBtn}
            disabled={creating || !newName.trim()}
          >
            <FolderPlus size={13} aria-hidden="true" /> Create
          </button>
        </form>

        <div className={styles.actions}>
          <button type="button" className={styles.cancel} onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className={styles.select}
            onClick={() => onPick(cwd)}
            disabled={dirs === null || !cwd}
          >
            Select {rel ? `~/${rel}` : "~"}
          </button>
        </div>
      </div>
    </div>
  );
}
