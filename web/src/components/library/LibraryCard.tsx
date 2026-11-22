/** THE ONE LIBRARY CARD (#2144): Playbooks, Skills, Specialists and Mission plans, on both the
 *  Yours and the Public gallery tab. A stamp (kind · version), a source tag, a title, a two-line
 *  summary, a per-kind meta row and a provenance footer with the card's own controls.
 *
 *  The whole card opens the item: the title is the card's ONE link and its `::after` is stretched
 *  over the card (never a wrapping `<a>`, so no control is nested inside a link). The footer's
 *  controls (Export, a specialist's Release) sit above that layer and act on their own. */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Box, Check, Download, FileText, GalleryHorizontal, Library, Sparkles } from "lucide-react";
import type { ShareKind } from "../../types/share";
import { Stamp } from "../hud/Meters";
import { useExportEntry } from "../share/useLibraryShare";
import buttons from "../ui/actionButton.module.css";
import styles from "./library.module.css";

/** What a copy IS: never a state, so never a status hue (the accent square marks your own). */
export type LibrarySource =
  | "local"
  | "imported"
  | "gallery"
  | "bundled"
  | "catalog"
  | "mission";

const SOURCE: Record<LibrarySource, { word: string; icon: ReactNode }> = {
  local: { word: "Local", icon: <span className={styles.sq} aria-hidden="true" /> },
  imported: { word: "Imported", icon: <FileText size={12} aria-hidden /> },
  gallery: { word: "Gallery", icon: <GalleryHorizontal size={12} aria-hidden /> },
  bundled: { word: "Bundled", icon: <Box size={12} aria-hidden /> },
  catalog: { word: "Catalog", icon: <Library size={12} aria-hidden /> },
  mission: { word: "From a mission", icon: <Sparkles size={12} aria-hidden /> },
};

export function SourceTag({ source }: { source: LibrarySource }) {
  const s = SOURCE[source];
  return (
    <span className={styles.src} data-source={source}>
      {s.icon}
      {s.word}
    </span>
  );
}

/** The kind · version stamp a card and a detail header open with. */
export function KindStamp({ kind, version }: { kind: string; version?: string | null }) {
  return (
    <Stamp>
      {kind}
      {version ? (
        <>
          {" · "}
          <b>v{version.replace(/^v/i, "")}</b>
        </>
      ) : null}
    </Stamp>
  );
}

/** The DEFAULT mark (a playbook new projects start from). */
export function DefaultMark() {
  return <strong className={styles.default}>Default</strong>;
}

/** A public entry you already hold a copy of: a fact with its mark, never a state colour. */
export function InLibraryMark() {
  return (
    <span className={styles.have} data-testid="in-your-library">
      <Check size={12} aria-hidden />
      In your library
    </span>
  );
}

export function LibraryCard({
  to,
  title,
  kind,
  version,
  badges,
  source,
  summary,
  meta,
  provenance,
  actions,
  children,
  invalid = false,
  testId,
}: {
  /** Where the card opens; none (an unreadable item) leaves the title as plain text. */
  to?: string;
  title: string;
  kind: string;
  version?: string | null;
  /** Marks beside the stamp (Default). */
  badges?: ReactNode;
  source?: LibrarySource;
  /** The two-line summary. Omitted when the kind carries none (a local skill). */
  summary?: ReactNode;
  /** The per-kind mono meta row (or rows, one per entry). */
  meta?: ReactNode | ReactNode[];
  provenance?: ReactNode;
  /** The card's own controls, raised above the stretched link. */
  actions?: ReactNode;
  /** Extra body between the meta row and the footer (an unreadable item's reason). */
  children?: ReactNode;
  invalid?: boolean;
  testId?: string;
}) {
  return (
    <article
      className={`${styles.card} ${invalid ? styles.invalid : ""}`.trim()}
      data-testid={testId}
    >
      <div className={styles.top}>
        <KindStamp kind={kind} version={version} />
        {badges}
        {source && <SourceTag source={source} />}
      </div>
      <h2 className={styles.title}>
        {to ? (
          <Link className={styles.link} to={to} data-library-card-link="">
            {title}
          </Link>
        ) : (
          title
        )}
      </h2>
      {summary !== undefined && <p className={styles.sum}>{summary}</p>}
      {(Array.isArray(meta) ? meta : meta ? [meta] : []).map((row, i) => (
        <p key={i} className={styles.meta}>
          {row}
        </p>
      ))}
      {children}
      {(provenance || actions) && (
        <div className={styles.foot}>
          <span className={styles.prov}>{provenance}</span>
          {actions && <div className={styles.actions}>{actions}</div>}
        </div>
      )}
    </article>
  );
}

/** `n thing(s)` with the number bolded, for a card's meta row. */
export function Count({ n, one, many = `${one}s` }: { n: number; one: string; many?: string }) {
  return (
    <span>
      <b>{n}</b> {n === 1 ? one : many}
    </span>
  );
}

/** The meta row's separator. */
export function Dot() {
  return <span aria-hidden="true">·</span>;
}

/** A card's Export: today's export dialog with this item chosen. It sits above the stretched link,
 *  so pressing it never opens the card. */
export function CardExport({ kind, id, title }: { kind: ShareKind; id: string; title: string }) {
  const entry = useExportEntry(kind);
  return (
    <>
      <button
        type="button"
        className={`${buttons.ghost} ${buttons.tight}`}
        aria-label={`Export ${title}`}
        data-testid="card-export"
        onClick={() => entry.open(id)}
      >
        <Download size={14} aria-hidden />
        Export
      </button>
      {entry.dialog}
    </>
  );
}
