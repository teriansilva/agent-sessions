// The ONE requestAnimationFrame scheduler for JS motion (docs/design.md §8 "Budget and safety":
// one atmosphere canvas, CSS keyframes everywhere else, one shared rAF scheduler for JS effects).
// First user: the API pane's eased follow-scroll (#1727).
//
// A job is a function called once per frame with the frame time; it returns `true` to run again
// next frame and `false` when it is done. The loop exists only while a job does: with nothing
// scheduled no frame is requested, so an idle page wakes nothing (the idle budget,
// e2e/nightjarShell.ts `expectIdleBudget`). Every job runs in the same frame callback, so N
// effects cost one rAF wake, not N.

export type FrameJob = (now: number) => boolean;

const jobs = new Set<FrameJob>();
let frame = 0;
// Inside `run`: a job scheduled by another job joins the next frame `run` requests itself, never
// a second loop of its own.
let running = false;

function run(now: number) {
  frame = 0;
  running = true;
  for (const job of [...jobs]) {
    let again = false;
    try {
      again = job(now);
    } catch (e) {
      // One broken effect must not stop the others (or leave the loop spinning on it).
      console.error(e);
    }
    if (!again) jobs.delete(job);
  }
  running = false;
  if (jobs.size && !frame) frame = requestAnimationFrame(run);
}

/** Run `job` on every frame until it returns false. Returns its cancel. */
export function scheduleFrames(job: FrameJob): () => void {
  jobs.add(job);
  if (!frame && !running) frame = requestAnimationFrame(run);
  return () => {
    jobs.delete(job);
    if (!jobs.size && frame) {
      cancelAnimationFrame(frame);
      frame = 0;
    }
  };
}

/** Jobs scheduled right now (tests). */
export function scheduledFrames(): number {
  return jobs.size;
}
