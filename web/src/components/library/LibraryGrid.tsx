/** The Library grid (#2144): its cards, then the dashed tiles that close it, plus the toolbar both
 *  tabs share (search, facet chips, a count). Import lives here as a tile, not in the page header:
 *  it opens the same file import the header's Import… did. */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Plus, Search, Upload } from "lucide-react";
import { Chip } from "../hud/Controls";
import styles from "./library.module.css";

export function LibraryGrid({
  children,
  newTile,
  onImport,
  importHint = "A .battlelab file someone shared with you: playbooks, skills and what they depend on.",
  testId,
}: {
  children?: ReactNode;
  /** The dashed `+ New …` tile (Playbooks, Specialists). */
  newTile?: { to: string; title: string; hint?: string };
  /** The dashed Import from file tile (every local tab). */
  onImport?: () => void;
  importHint?: string;
  testId?: string;
}) {
  return (
    <div className={styles.grid} data-testid={testId}>
      {children}
      {newTile && (
        <Link to={newTile.to} className={styles.tile} data-testid="library-new-tile">
          <Plus size={22} aria-hidden />
          <span className={styles.tileTitle}>{newTile.title}</span>
          {newTile.hint && <span className={styles.tileHint}>{newTile.hint}</span>}
        </Link>
      )}
      {onImport && (
        <button
          type="button"
          className={styles.tile}
          onClick={onImport}
          data-testid="library-import-tile"
        >
          <Upload size={22} aria-hidden />
          <span className={styles.tileTitle}>Import from file</span>
          <span className={styles.tileHint}>{importHint}</span>
        </button>
      )}
    </div>
  );
}

export interface Facet<T extends string> {
  value: T;
  label: string;
  n: number;
}

/** One toolbar on both tabs: the search, the facet chips this list can actually filter on, and the
 *  count of what is shown. */
export function LibraryToolbar<T extends string>({
  label,
  placeholder,
  search,
  onSearch,
  facets,
  facet,
  onFacet,
  count,
  children,
}: {
  label: string;
  placeholder: string;
  search: string;
  onSearch: (value: string) => void;
  facets?: Facet<T>[];
  facet?: T;
  onFacet?: (value: T) => void;
  count?: string;
  /** Extra controls kept on the row (Refresh). */
  children?: ReactNode;
}) {
  return (
    <div className={styles.toolbar}>
      <span className={styles.search}>
        <Search size={16} aria-hidden />
        <input
          className="nj-field"
          type="search"
          aria-label={label}
          value={search}
          placeholder={placeholder}
          onChange={(e) => onSearch(e.target.value)}
        />
      </span>
      {facets && onFacet && (
        <div className={styles.chips} role="group" aria-label="Show">
          {facets.map((f) => (
            <Chip key={f.value} on={facet === f.value} count={f.n} onClick={() => onFacet(f.value)}>
              {f.label}
            </Chip>
          ))}
        </div>
      )}
      {children}
      {count && (
        <span className={styles.count} role="status">
          {count}
        </span>
      )}
    </div>
  );
}

/** The page head every Library gallery tab shares: the breadcrumb, the title and ONE primary
 *  (#2144: Import… and Export… left the header). */
export function LibraryHead({
  section,
  title,
  count,
  primary,
}: {
  /** The breadcrumb's last word: `LIBRARY // <section>`. */
  section: string;
  title: string;
  count?: number;
  primary?: ReactNode;
}) {
  return (
    <>
      <div className={`nj-kicker ${styles.kicker}`}>
        <span className="nj-notch" aria-hidden="true" />
        Library // <b>{section}</b>
        {count !== undefined ? ` // ${count}` : ""}
      </div>
      <div className={styles.head}>
        <h1 className={styles.h1}>{title}</h1>
        {primary}
      </div>
    </>
  );
}
