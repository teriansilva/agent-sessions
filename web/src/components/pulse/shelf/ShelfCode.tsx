/** A found code change in the lightbox (#1740 P1b, board 3c): its files on the left, the selected
 *  file's diff on the right. The diff comes from the mission-bound diff route (by item id; the
 *  patches were taken at collection inside the mission's boundary) and is rendered as TEXT, one
 *  line per span — nothing in it is ever parsed as markup. A deleted file has no viewer. */
import { useEffect, useState } from "react";

import { api, ApiError } from "../../../lib/api";
import type { ShelfDiffFile, ShelfItem } from "../../../types/shelf";
import styles from "./shelf.module.css";

const GLYPH: Record<string, [string, string]> = {
  A: ["A", styles.glA],
  M: ["M", styles.glM],
  D: ["D", styles.glD],
};

function lineClass(line: string): string {
  if (line.startsWith("@@")) return styles.diffHunk;
  if (line.startsWith("+++") || line.startsWith("---")) return styles.diffHunk;
  if (line.startsWith("+")) return styles.diffAdd;
  if (line.startsWith("-")) return styles.diffDel;
  return "";
}

export function ShelfCodeStage({
  missionId,
  item,
}: {
  missionId: string;
  item: ShelfItem;
}) {
  const [files, setFiles] = useState<ShelfDiffFile[] | null>(null);
  const [more, setMore] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [sel, setSel] = useState(0);
  useEffect(() => {
    let alive = true;
    api
      .missionShelfDiff(missionId, item.id)
      .then((d) => {
        if (!alive) return;
        setFiles(d.files);
        setMore(d.files_more);
      })
      .catch((e: unknown) => {
        if (alive)
          setErr(
            e instanceof ApiError
              ? e.message
              : "The changes could not be read.",
          );
      });
    return () => {
      alive = false;
    };
  }, [missionId, item.id]);
  if (err) return <div className={styles.stageMsg}>{err}</div>;
  if (!files) return <div className={styles.stageMsg}>Loading…</div>;
  const f = files[sel];
  return (
    <div className={styles.code} data-shelf-code="">
      <ul className={styles.fileList} aria-label="Changed files">
        {files.map((x, i) => {
          const [g, cls] = GLYPH[x.status] ?? [x.status, ""];
          return (
            <li key={x.path}>
              <button
                type="button"
                className={styles.fileBtn}
                aria-current={i === sel}
                onClick={() => setSel(i)}
                title={x.path}
                data-shelf-code-file={x.path}
              >
                <span
                  className={`${styles.gl} ${cls}`}
                  aria-label={statusWord(x.status)}
                >
                  {g}
                </span>
                <span className={styles.fileName}>{x.path}</span>
                <span className={styles.diffstat}>
                  {x.add !== null ? (
                    <span className={styles.dsAdd}>+{x.add}</span>
                  ) : null}
                  {x.del ? (
                    <span className={styles.dsDel}>−{x.del}</span>
                  ) : null}
                </span>
              </button>
            </li>
          );
        })}
        {more ? <li className={styles.hint}>…and {more} more files</li> : null}
      </ul>
      {f ? (
        <pre className={styles.diff} tabIndex={0} data-shelf-diff={f.path}>
          <span className={styles.diffHead}>
            {f.path}
            {f.worktree_too ? " · also changed in the working tree since" : ""}
          </span>
          {f.status === "D" ? (
            <span className={styles.diffLine}>Deleted — nothing to show.</span>
          ) : f.unavailable ? (
            <span className={styles.diffLine}>
              This file's changes could not be read.
            </span>
          ) : (
            f.patch
              .split("\n")
              .filter((l, i, a) => i < a.length - 1 || l !== "")
              .map((l, i) => (
                <span key={i} className={`${styles.diffLine} ${lineClass(l)}`}>
                  {l || " "}
                </span>
              ))
          )}
          {f.truncated ? (
            <span className={styles.diffLine}>
              … the diff is longer than the shelf keeps.
            </span>
          ) : null}
        </pre>
      ) : (
        <div className={styles.stageMsg}>No files.</div>
      )}
    </div>
  );
}

function statusWord(s: string): string {
  return s === "A" ? "added" : s === "D" ? "deleted" : "modified";
}
