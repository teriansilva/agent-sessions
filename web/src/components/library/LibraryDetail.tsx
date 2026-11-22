/** THE ONE LIBRARY DETAIL (#2144): back link, header (stamp, source, title, byline), an action bar
 *  (the kind's primary, Export, Import…, and ⋯ for the rest) and two columns: the contents, and a
 *  quiet side panel of facts. On a phone the bar wraps with the primary on its own line and the
 *  facts stack below the contents. */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Download, Upload } from "lucide-react";
import { RowMenu, type RowMenuEntry } from "../sidebar/RowMenu";
import buttons from "../ui/actionButton.module.css";
import { KindStamp, SourceTag, type LibrarySource } from "./LibraryCard";
import styles from "./library.module.css";

export function LibraryDetail({
  back,
  kicker,
  kind,
  version,
  source,
  badges,
  title,
  by,
  bar,
  notices,
  lead,
  facts,
  children,
  after,
  reviewFirst = false,
  testId,
}: {
  back: { to: string; label: string };
  /** The breadcrumb's last word: `LIBRARY // <kicker>`. */
  kicker: string;
  kind: string;
  version?: string | null;
  source?: LibrarySource;
  /** Marks after the source (Default, Verified). */
  badges?: ReactNode;
  title: string;
  /** The byline under the title (a public entry's author and publisher). */
  by?: ReactNode;
  /** The action bar (`ActionBar`). */
  bar?: ReactNode;
  /** Status lines and alerts between the bar and the columns. */
  notices?: ReactNode;
  /** The summary leading the main column. */
  lead?: ReactNode;
  /** The side panel's facts (`Fact` rows). */
  facts?: ReactNode;
  /** The main column's sections. */
  children?: ReactNode;
  /** Full width under both columns (a public entry's Technical details). */
  after?: ReactNode;
  /** On a phone, the bar follows the contents: a public entry is reviewed before it is added
   *  (#1440). */
  reviewFirst?: boolean;
  testId?: string;
}) {
  return (
    <div
      className={`${styles.detail} ${reviewFirst ? styles.reviewFirst : ""}`.trim()}
      data-testid={testId}
    >
      <Link className={styles.back} to={back.to}>
        <ArrowLeft size={16} aria-hidden />
        {back.label}
      </Link>
      <header className={styles.dhead}>
        <div className={`nj-kicker ${styles.kicker}`}>
          <span className="nj-notch" aria-hidden="true" />
          Library // <b>{kicker}</b>
        </div>
        <div className={styles.tags}>
          <KindStamp kind={kind} version={version} />
          {source && <SourceTag source={source} />}
          {badges}
        </div>
        <h1 className={styles.h1}>{title}</h1>
        {by && <p className={styles.by}>{by}</p>}
      </header>
      {bar}
      {notices}
      <div className={`${styles.cols} ${facts ? "" : styles.solo}`.trim()}>
        <div>
          {lead && <p className={styles.lead}>{lead}</p>}
          {children}
        </div>
        {facts && (
          <aside className={styles.side} aria-label="Facts">
            <h2 className={styles.sideHead}>
              <span className="nj-notch" aria-hidden="true" />
              Facts
            </h2>
            <dl className={styles.facts}>{facts}</dl>
          </aside>
        )}
      </div>
      {after && <div className={styles.after}>{after}</div>}
    </div>
  );
}

/** One row of the side panel: a mono label and its value. */
export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** The Source fact: what this copy is, one line of context, and a slot for where it came from
 *  (a gallery origin today; #2145's repository line for skills). */
export function SourceFact({
  word,
  note,
  origin,
}: {
  word: ReactNode;
  note?: ReactNode;
  /** Where the copy came from, one line under the note. */
  origin?: ReactNode;
}) {
  return (
    <Fact label="Source">
      <span className={styles.prov}>{word}</span>
      {note && <span className={styles.small}>{note}</span>}
      {origin && (
        <span className={styles.small} data-testid="library-origin">
          {origin}
        </span>
      )}
    </Fact>
  );
}

/** A section of the main column under its callsign. */
export function DetailSection({
  title,
  children,
  label,
}: {
  title: ReactNode;
  children: ReactNode;
  label?: string;
}) {
  return (
    <section className={styles.section} aria-label={label}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

/** The action bar: the kind's primary first, then Export and Import… (local items), the ⋯ menu for
 *  the rest (`MoreMenu`, as children), and a why-line at the end. `primary` is drawn by the caller (a link or a button). */
export function ActionBar({
  primary,
  onExport,
  onImport,
  why,
  children,
}: {
  primary?: ReactNode;
  onExport?: () => void;
  onImport?: () => void;
  why?: ReactNode;
  /** Controls after Import… — the kind's ⋯ (`MoreMenu`). */
  children?: ReactNode;
}) {
  return (
    <div className={styles.bar} role="toolbar" aria-label="Actions">
      {primary && <span className={styles.primary}>{primary}</span>}
      {onExport && (
        <button type="button" className={buttons.ghost} onClick={onExport} data-testid="library-export">
          <Download size={16} aria-hidden />
          Export
        </button>
      )}
      {onImport && (
        <button type="button" className={buttons.ghost} onClick={onImport} data-testid="library-import">
          <Upload size={16} aria-hidden />
          Import…
        </button>
      )}
      {children}
      {why && <p className={styles.why}>{why}</p>}
    </div>
  );
}

/** The action bar's ⋯: Duplicate, Set as default, Archive, Delete… — a menu under its trigger on a
 *  desktop, a bottom sheet at ≤800px (the shared row-menu chrome). The trigger is
 *  `data-testid="library-more"`, for a confirm an item opens to hand focus back to. */
export function MoreMenu({ items, title }: { items: RowMenuEntry[]; title: string }) {
  if (!items.length) return null;
  return (
    <RowMenu
      items={items}
      title={title}
      sheetTitle="More actions"
      triggerLabel="More actions"
      triggerClassName={`${buttons.ghost} ${buttons.tight}`}
      triggerTestId="library-more"
    />
  );
}
