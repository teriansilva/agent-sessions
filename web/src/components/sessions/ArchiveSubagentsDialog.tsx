/** "Archive only this / Archive with N subagents" (#2325, `docs/invariants/session-lineage.md`).
 *
 *  Archiving is a lifecycle operation, not presentation: archiving subagents stops their agents.
 *  So the default is the main session alone (its subagents become main sessions of their own),
 *  and the subagent set is FROZEN when the prompt opens — the exact keys shown are the keys sent.
 *  The server re-validates each one and runs it through the same checks as archiving it alone;
 *  the outcome comes back per key and is shown as it is, failures included, never retried. */
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { announceSessionRowChanged } from "../../lib/sessionRowEvents";
import type { ArchiveSubagentResult, Session } from "../../types/api";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import styles from "./ArchiveSubagentsDialog.module.css";

/** The server's cap on one request; a larger tree offers "only this". */
export const ARCHIVE_SUBAGENTS_MAX = 200;

/** Every listed descendant of ``id``, depth-first, through the lazy children route. */
async function descendants(id: string, archived: boolean): Promise<Session[] | "too-many"> {
  const out: Session[] = [];
  const stack = [id];
  while (stack.length) {
    const parent = stack.pop()!;
    let offset: number | null = 0;
    while (offset != null) {
      const page = await api.sessionChildren(parent, { offset, limit: 200, archived });
      for (const c of page.sessions) {
        out.push(c);
        if (out.length > ARCHIVE_SUBAGENTS_MAX) return "too-many";
        if ((c.child_count ?? 0) > 0) stack.push(c.id);
      }
      offset = page.next_offset;
    }
  }
  return out;
}

type Phase =
  | { kind: "counting" }
  | { kind: "ready"; subs: Session[] }
  | { kind: "unavailable"; why: string }
  | { kind: "done"; results: ArchiveSubagentResult[]; titles: Record<string, string> };

export function ArchiveSubagentsDialog({
  s,
  onArchiveOnly,
  onClose,
  returnFocusTo,
}: {
  s: Session;
  onArchiveOnly: () => Promise<void>;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [phase, setPhase] = useState<Phase>({ kind: "counting" });
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    descendants(s.id, s.archived).then(
      (subs) => {
        if (!live) return;
        setPhase(
          subs === "too-many"
            ? { kind: "unavailable", why: `More than ${ARCHIVE_SUBAGENTS_MAX} subagents.` }
            : subs.length
              ? { kind: "ready", subs }
              : { kind: "unavailable", why: "No subagents are listed any more." },
        );
      },
      () => live && setPhase({ kind: "unavailable", why: "Couldn't read its subagents." }),
    );
    return () => {
      live = false;
    };
  }, [s.id, s.archived]);

  const title = s.title || "(untitled)";

  if (phase.kind === "done") {
    const ok = phase.results.filter((r) => r.result === "archived").length;
    // The rows leave the list only once the report is read: this dialog belongs to the main
    // session's row, and announcing its archive earlier would unmount the report with it.
    const close = () => {
      for (const x of phase.results) {
        if (x.result === "archived") announceSessionRowChanged(x.id, { archived: true });
      }
      onClose();
    };
    return (
      <ConfirmDialog
        tag="ARCHIVE SESSION"
        title={`Archived with subagents — ${ok} of ${phase.results.length} done`}
        cancelLabel="Close"
        confirmLabel="Done"
        onCancel={close}
        onConfirm={close}
        returnFocusTo={returnFocusTo}
        markers={{ dialog: { "data-testid": "archive-subagents-result" } }}
      >
        {ok < phase.results.length ? <p>Partial result. Nothing that failed was retried.</p> : null}
        <ul className={styles.results}>
          {phase.results.map((r) => (
            <li key={r.id} className={styles.result} data-result={r.result}>
              <span className={styles[r.result]} aria-hidden="true">
                {r.result === "archived" ? "✓" : r.result === "skipped" ? "–" : "✕"}
              </span>
              <span className={styles.name}>{phase.titles[r.id] || r.id}</span>
              {r.result !== "archived" ? (
                <span className={styles[r.result]}>
                  {r.result === "skipped" ? "skipped" : "failed"}
                  {r.reason ? ` · ${r.reason}` : ""}
                </span>
              ) : (
                <span className={styles.srOnly}>archived</span>
              )}
            </li>
          ))}
        </ul>
      </ConfirmDialog>
    );
  }

  const subs = phase.kind === "ready" ? phase.subs : [];
  const running = subs.filter((c) => c.running).length;
  const withSubagents = async () => {
    setBusy(true);
    try {
      const r = await api.archiveWithSubagents(
        s.id,
        subs.map((c) => c.id),
      );
      const titles: Record<string, string> = { [s.id]: title };
      for (const c of subs) titles[c.id] = c.title || "(untitled)";
      setPhase({ kind: "done", results: r.results, titles });
    } catch {
      setPhase({ kind: "unavailable", why: "The archive request failed; nothing was confirmed." });
    } finally {
      setBusy(false);
    }
  };
  const onlyThis = async () => {
    setBusy(true);
    try {
      await onArchiveOnly();
      onClose();
    } finally {
      setBusy(false);
    }
  };
  return (
    <ConfirmDialog
      tag="ARCHIVE SESSION"
      title={`Archive “${title}”?`}
      confirmLabel="Archive only this"
      onCancel={onClose}
      onConfirm={() => void onlyThis()}
      busy={busy}
      safeFirst
      returnFocusTo={returnFocusTo}
      markers={{ dialog: { "data-testid": "archive-subagents-dialog" } }}
      secondary={
        phase.kind === "ready"
          ? {
              label: `Archive with ${phase.subs.length} subagent${phase.subs.length === 1 ? "" : "s"}`,
              onClick: () => void withSubagents(),
              markers: { "data-testid": "archive-with-subagents" },
            }
          : undefined
      }
    >
      {phase.kind === "counting" ? (
        <p role="status">Counting its subagents…</p>
      ) : phase.kind === "unavailable" ? (
        <p>{phase.why} You can still archive only this session.</p>
      ) : (
        <p>
          It has {subs.length} subagent{subs.length === 1 ? "" : "s"} (all descendants
          {running ? `, ${running} running` : ""}). Archiving only this session leaves them
          listed as main sessions. Archiving them too stops their agents; each one passes its own
          archive checks.
        </p>
      )}
    </ConfirmDialog>
  );
}
