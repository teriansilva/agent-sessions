import { useCallback, useRef, useState } from "react";
import { ApiError, api } from "../../lib/api";
import {
  MAX_BATCH_FILES,
  type PlannedFile,
  type UploadPlan,
  planUpload,
} from "./uploadPlan";

/** How many uploads are in flight at once.
 *
 *  Small on purpose. Each request carries a whole file body, and through the Home Free relay
 *  that body is resident in the browser, the mux and the agent at the same time — so a wide fan
 *  of concurrent uploads multiplies peak memory on the box for no wall-clock gain on a link that
 *  is already the bottleneck. Server-side these also share the panel's single admission budget
 *  with the tree's own reads, so going wider would mostly queue anyway.
 */
export const CONCURRENCY = 3;

export type RowState =
  | { kind: "queued" }
  | { kind: "sending" }
  | { kind: "done"; name: string }
  | { kind: "skipped"; reason: string }
  | { kind: "failed"; reason: string }
  | { kind: "collision"; reason: string };

export interface QueueRow {
  id: string;
  relpath: string;
  size: number;
  state: RowState;
}

export type CollisionChoice = "skip" | "keep_both" | "replace";

export interface UploadsApi {
  rows: QueueRow[];
  busy: boolean;
  /** A whole-batch refusal (too many files / too many bytes). Nothing was sent. */
  refusal: string | null;
  /** The first unresolved collision, if any — the operator chooses per file or for the rest. */
  collision: { row: QueueRow; remaining: number } | null;
  start: (dir: string, files: PlannedFile[]) => Promise<void>;
  /** Show a whole-drop refusal that did not come from the planner — a traversal failure, say. */
  refuse: (reason: string) => void;
  resolveCollision: (choice: CollisionChoice, applyToRest: boolean) => void;
  dismiss: () => void;
}

let seq = 0;
const nextId = () => `u${++seq}`;

/** Drive one upload batch: plan it, reserve it, send it, and report per file (#807).
 *
 *  Two shapes are deliberate:
 *
 *  * **A per-file failure never aborts the batch.** A single 26 MB file in a 40-file drop is one
 *    red row; the other thirty-nine still land. Collapsing a batch to one success/failure would
 *    hide what actually happened, which is the thing this queue exists to show.
 *  * **A collision is an operator choice, never a decision the panel makes.** The server refuses
 *    a name that exists (`O_EXCL`), and `replace` is only ever sent *after* someone chose it.
 */
export function useUploads(onSettled?: () => void): UploadsApi {
  const [rows, setRows] = useState<QueueRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [collision, setCollision] = useState<UploadsApi["collision"]>(null);
  // Collision questions are asked ONE AT A TIME. With three workers, concurrent 409s each
  // overwrote a single resolver ref: the operator answered the one prompt they could see and the
  // other two waited forever, so `start()` never settled and the queue stayed "uploading".
  // A FIFO of waiters plus a serialising chain fixes both halves.
  const waiters = useRef<Array<{ row: QueueRow; remaining: number; resolve: (c: CollisionChoice) => void }>>([]);
  const asking = useRef(false);
  const stickyChoice = useRef<CollisionChoice | null>(null);
  // A synchronous fence across EVERY start path (picker and tree drop), because `busy` is state
  // and does not update until React re-renders — two drops in the same tick both passed it.
  const running = useRef(false);

  const patch = useCallback((id: string, state: RowState) => {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, state } : r)));
  }, []);

  /** Show the next queued collision, or drain the queue against a sticky choice.
   *
   *  Iterative rather than self-recursive: a `useCallback` cannot call itself by name (it is not
   *  in scope inside its own initializer), and the loop is what "apply to the rest" needs anyway
   *  — it answers every waiter without a prompt in between.
   */
  const pump = useCallback(() => {
    for (;;) {
      if (asking.current) return;
      const next = waiters.current[0];
      if (!next) return;
      if (stickyChoice.current) {
        waiters.current.shift();
        next.resolve(stickyChoice.current);
        continue;
      }
      asking.current = true;
      setCollision({ row: next.row, remaining: next.remaining + waiters.current.length - 1 });
      return;
    }
  }, []);

  const askCollision = useCallback(
    (row: QueueRow, remaining: number): Promise<CollisionChoice> => {
      if (stickyChoice.current) return Promise.resolve(stickyChoice.current);
      return new Promise((resolve) => {
        waiters.current.push({ row, remaining, resolve });
        pump();
      });
    },
    [pump],
  );

  const resolveCollision = useCallback(
    (choice: CollisionChoice, applyToRest: boolean) => {
      if (applyToRest) stickyChoice.current = choice;
      setCollision(null);
      const answered = waiters.current.shift();
      asking.current = false;
      answered?.resolve(choice);
      // Whatever is still queued gets its turn, rather than waiting forever behind the one
      // prompt the operator happened to see.
      pump();
    },
    [pump],
  );

  const start = useCallback(
    async (dir: string, files: PlannedFile[]) => {
      // Synchronous, before any await: two drops (or a drop plus a pick) in the same tick would
      // otherwise both pass, and the first to finish would clear `busy` while the second ran on,
      // replacing rows and the sticky collision choice underneath it.
      //
      // But the fence must SAY SO. Returning silently threw away a batch the operator had chosen
      // — files they dropped simply vanished, which is the opposite of the bounded-and-honest
      // contract this panel is built on. Refusing out loud is the minimum; queueing would be
      // better and is deliberately not attempted here (it needs its own design).
      if (running.current) {
        setRefusal(
          `An upload is already running — wait for it to finish, then drop those ${files.length} file${files.length === 1 ? "" : "s"} again.`,
        );
        return;
      }
      running.current = true;
      const plan: UploadPlan = planUpload(files);
      setRefusal(plan.refusal);
      stickyChoice.current = null;
      const planned: QueueRow[] = [
        ...plan.files.map((f) => ({
          id: nextId(),
          relpath: f.relpath,
          size: f.size,
          state: { kind: "queued" } as RowState,
        })),
        ...plan.rejected.map((r) => ({
          id: nextId(),
          relpath: r.relpath,
          size: 0,
          state: { kind: "skipped", reason: r.reason } as RowState,
        })),
      ];
      setRows(planned);
      if (plan.refusal || !plan.files.length) {
        running.current = false;
        return;
      }

      setBusy(true);
      try {
        // The manifest is the ADMISSION check — it buys a fast, honest failure for a drop that
        // was never going to fit. The bound that actually holds is charged per chunk server-side.
        let batchId: string | undefined;
        try {
          const batch = await api.filesUploadBatch(
            plan.files.slice(0, MAX_BATCH_FILES).map((f) => ({ relpath: f.relpath, size: f.size })),
          );
          batchId = batch.batch_id;
        } catch (e) {
          setRefusal(e instanceof ApiError ? e.message : "Could not start the upload.");
          return;
        }

        const queue = plan.files.map((f, i) => ({ f, row: planned[i] }));
        let cursor = 0;
        const runOne = async (): Promise<void> => {
          for (;;) {
            const at = cursor++;
            if (at >= queue.length) return;
            const { f, row } = queue[at];
            patch(row.id, { kind: "sending" });
            try {
              const res = await api.filesUpload(dir, f.relpath, f.file, { batchId });
              patch(row.id, { kind: "done", name: res.name });
            } catch (e) {
              const err = e instanceof ApiError ? e : null;
              // 409 from the collision check is the ONE failure that is a question rather than
              // an outcome — everything else is reported and the batch carries on.
              if (err?.status === 409 && /already exists/i.test(err.message)) {
                const remaining = queue.length - at - 1;
                const choice = await askCollision(row, remaining);
                if (choice === "skip") {
                  // Tell the SERVER *first*, and only claim Skip once it agrees. Going quiet
                  // left the manifest entry pending and the batch holding its slot for the full
                  // idle TTL — but swallowing the error and rendering SKIPPED anyway was worse:
                  // it recreated exactly that lockout while telling the operator it had been
                  // handled. A settlement that did not happen is a failed row, not a skipped one.
                  if (batchId) {
                    try {
                      await api.filesUploadSkip(batchId, f.relpath);
                    } catch (e3) {
                      patch(row.id, {
                        kind: "failed",
                        reason: e3 instanceof ApiError ? e3.message : "could not record the skip",
                      });
                      continue;
                    }
                  }
                  patch(row.id, { kind: "skipped", reason: "SKIPPED — exists" });
                  continue;
                }
                try {
                  const res = await api.filesUpload(dir, f.relpath, f.file, {
                    batchId,
                    onCollision: choice,
                  });
                  patch(row.id, { kind: "done", name: res.name });
                } catch (e2) {
                  patch(row.id, {
                    kind: "failed",
                    reason: e2 instanceof ApiError ? e2.message : "upload failed",
                  });
                }
                continue;
              }
              patch(row.id, {
                kind: "failed",
                reason: err ? err.message : "upload failed",
              });
            }
          }
        };
        await Promise.all(Array.from({ length: CONCURRENCY }, runOne));
      } finally {
        waiters.current = [];
        asking.current = false;
        running.current = false;
        // A refusal raised WHILE this ran ("an upload is already running", an unreadable folder)
        // described the run that has now finished. Leaving it on top of a completed queue tells
        // the operator to wait for something that already ended.
        setRefusal((cur) => (cur === plan.refusal ? cur : null));
        setBusy(false);
        onSettled?.();
      }
    },
    [askCollision, patch, onSettled],
  );

  const dismiss = useCallback(() => {
    setRows([]);
    setRefusal(null);
    setCollision(null);
  }, []);

  const refuse = useCallback((reason: string) => {
    // NEVER clears a running batch's rows. An unreadable second folder used to wipe the first
    // batch's queue while its requests carried on, so later row patches had no targets and the
    // operator lost sight of an upload that was still happening.
    if (!running.current) setRows([]);
    setRefusal(reason);
  }, []);

  return { rows, busy, refusal, collision, start, refuse, resolveCollision, dismiss };
}
