/** One shelf tile (#1740, board 2): what the item is (its kind), where it came from (its source
 *  tag), and where the review stands (its verdict, with the start of its note). A tile is a
 *  control — pressing it opens the lightbox, where verdicts are set — and never sets anything
 *  itself. Agent-written text is rendered as text only. */
import { FileX, GitBranch, Play } from "lucide-react";
import { forwardRef, useState } from "react";

import type { ShelfItem, ShelfKind } from "../../../types/shelf";
import { ShelfDuration, ShelfThumb } from "./ShelfMedia";
import { KIND_ICON } from "./shelfIcons";
import {
  KIND_LABEL,
  docType,
  extOf,
  fmtBytes,
  fmtClock,
  fmtDuration,
  footState,
  isChanged,
  isGone,
} from "./shelfLogic";
import styles from "./shelf.module.css";

export function KindTag({ kind }: { kind: ShelfKind }) {
  const Icon = KIND_ICON[kind];
  return (
    <span className={styles.kind}>
      <Icon aria-hidden="true" />
      {KIND_LABEL[kind]}
    </span>
  );
}

export function SourceTag({ item }: { item: ShelfItem }) {
  return item.source === "delivered" ? (
    <span className={styles.src} data-source="delivered">
      <span className={styles.sq} aria-hidden="true" />
      Delivered
    </span>
  ) : (
    <span className={styles.src} data-source="found">
      <GitBranch aria-hidden="true" />
      Found · git
    </span>
  );
}

export function StateWord({
  word,
  led,
  tone,
}: {
  word: string;
  led: "up" | "attention" | "idle";
  tone: "ok" | "warn" | "idle";
}) {
  const toneCls = tone === "ok" ? styles.isOk : tone === "warn" ? styles.isWarn : styles.isIdle;
  return (
    <span className={`${styles.state} ${toneCls}`}>
      <span className={`hud-led ${led}`} aria-hidden="true" />
      {word}
    </span>
  );
}

function Media({ missionId, item }: { missionId: string; item: ShelfItem }) {
  const [duration, setDuration] = useState<number | null>(null);
  const tag = <KindTag kind={item.kind} />;
  if (isGone(item)) {
    return (
      <div className={`${styles.media} ${styles.gone}`}>
        {tag}
        <div className={styles.goneInner}>
          <FileX aria-hidden="true" />
          <span>{item.file?.state === "missing" ? "File not found" : "Not accessible"}</span>
          <span className={styles.goneName}>{item.name}</span>
        </div>
      </div>
    );
  }
  switch (item.kind) {
    case "image":
      return (
        <div className={`${styles.media} ${styles.checker}`}>
          {tag}
          {item.mime ? <ShelfThumb missionId={missionId} item={item} /> : null}
          {isChanged(item) ? (
            <span className={`${styles.flag} nj-stamp is-warn`} data-shelf-changed="">
              <span className={styles.hideM}>Changed since delivered</span>
              <span className={styles.showM}>Changed</span>
            </span>
          ) : null}
        </div>
      );
    case "video":
      return (
        <div className={styles.media}>
          {tag}
          {item.mime ? (
            <ShelfDuration missionId={missionId} item={item} onDuration={setDuration} />
          ) : null}
          <span className={styles.play} aria-hidden="true">
            <Play />
          </span>
          {duration !== null ? <span className={styles.dur}>{fmtDuration(duration)}</span> : null}
          {isChanged(item) ? (
            <span className={`${styles.flag} ${styles.flagTop} nj-stamp is-warn`} data-shelf-changed="">
              <span className={styles.hideM}>Changed since delivered</span>
              <span className={styles.showM}>Changed</span>
            </span>
          ) : null}
        </div>
      );
    case "audio": {
      const Icon = KIND_ICON.audio;
      return (
        <div className={`${styles.media} ${styles.card}`}>
          {tag}
          {item.mime ? (
            <ShelfDuration missionId={missionId} item={item} onDuration={setDuration} />
          ) : null}
          <div className={styles.aBig}>
            <Icon aria-hidden="true" />
            {duration !== null ? fmtDuration(duration) : "—"}
          </div>
          <div className={styles.sub}>
            {item.name} · {fmtBytes(item.size)}
          </div>
        </div>
      );
    }
    case "document":
      return (
        <div className={`${styles.media} ${styles.card}`}>
          {tag}
          <span className={styles.docIco} aria-hidden="true">
            {(extOf(item.name) || "file").slice(0, 4).toUpperCase()}
          </span>
          <div className={styles.docName}>{item.name}</div>
          <div className={styles.sub}>
            {docType(item.name)} · {fmtBytes(item.size)}
          </div>
        </div>
      );
    case "link":
      return (
        <div className={`${styles.media} ${styles.card}`}>
          {tag}
          <div className={styles.host}>{item.host}</div>
          <div className={styles.urlPath}>{item.url_path}</div>
        </div>
      );
    case "code": {
      const m = item.meta;
      if (!m) return <div className={`${styles.media} ${styles.card}`}>{tag}</div>;
      const files = m.files.length + (m.files_more || 0);
      const ref = m.commits.length
        ? m.commits.length === 1
          ? m.commits[0].sha.slice(0, 7)
          : `${m.commits.length}${m.commits_more ? "+" : ""} commits`
        : "uncommitted";
      return (
        <div className={`${styles.media} ${styles.card}`}>
          {tag}
          <div className={styles.repo}>{m.repo_name}</div>
          <div className={styles.sub}>
            {m.branch ? `${m.branch} · ` : ""}
            {ref}
          </div>
          <DiffStat add={m.add} del={m.del} files={files} />
        </div>
      );
    }
    case "text":
      return (
        <div className={`${styles.media} ${styles.card} ${styles.cardTop}`}>
          {tag}
          <p className={styles.excerpt}>{item.text}</p>
        </div>
      );
    default:
      return <div className={`${styles.media} ${styles.card}`}>{tag}</div>;
  }
}

/** `+a −d ▪▪▪▪▪ N files`: the share of added vs deleted lines in five squares (state colours, since
 *  they ARE the state of the lines), and the file count. */
export function DiffStat({ add, del, files }: { add: number; del: number; files: number }) {
  const n = 5;
  const tot = add + del;
  const na = tot ? Math.max(add ? 1 : 0, Math.round((add / tot) * n)) : 0;
  const nd = tot ? Math.min(n - na, Math.round((del / tot) * n)) : 0;
  return (
    <div className={styles.diffstat}>
      <span className={styles.dsAdd}>+{add}</span>
      <span className={styles.dsDel}>−{del}</span>
      <span className={styles.diffbar} aria-hidden="true">
        {Array.from({ length: n }, (_, i) => (
          <i key={i} className={i < na ? styles.dbAdd : i < na + nd ? styles.dbDel : undefined} />
        ))}
      </span>
      <span className={styles.dsFiles}>
        {files} {files === 1 ? "file" : "files"}
      </span>
    </div>
  );
}

export const ShelfTile = forwardRef<
  HTMLButtonElement,
  { missionId: string; item: ShelfItem; onOpen: (item: ShelfItem) => void }
>(function ShelfTile({ missionId, item, onOpen }, ref) {
  const foot = footState(item);
  const size =
    item.ref_type === "path" || item.ref_type === "text" ? fmtBytes(item.size) : "";
  return (
    <button
      ref={ref}
      type="button"
      className={`${styles.tile} ${isGone(item) ? styles.isGone : ""}`}
      aria-label={[
        `${KIND_LABEL[item.kind]}: ${item.title}`,
        foot.word,
        isChanged(item) ? "changed since delivered" : "",
        item.verdict && item.note && !isGone(item) ? `note: ${item.note}` : "",
      ]
        .filter(Boolean)
        .join(", ")}
      onClick={() => onOpen(item)}
      data-shelf-tile={item.id}
      data-kind={item.kind}
      data-verdict={item.verdict ?? "none"}
      data-file-state={item.file?.state ?? ""}
    >
      <Media missionId={missionId} item={item} />
      <div className={styles.tBody}>
        <p className={styles.tTitle}>{item.title}</p>
        {item.why ? <p className={styles.tWhy}>{item.why}</p> : null}
        <div className={styles.tMeta}>
          <SourceTag item={item} />
          {(item.sessions?.length ?? 0) > 1 ? (
            <span data-shelf-several="">Touched by {item.sessions!.length} sessions</span>
          ) : null}
          {size ? <span>{size}</span> : null}
          <span>{fmtClock(item.created_at)}</span>
        </div>
      </div>
      <div className={styles.tFoot}>
        <StateWord {...foot} />
        {item.verdict && item.note && !isGone(item) ? (
          <span className={styles.note}>“{item.note}”</span>
        ) : null}
      </div>
    </button>
  );
});
