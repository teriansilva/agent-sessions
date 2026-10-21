/** Fixture primitives for the app-mode E2E harness (#806).
 *
 *  Separate from `appmode.ts` on purpose: that module imports `@playwright/test`, which can only
 *  be loaded inside a Playwright run, so nothing in it can be unit-tested. These two functions
 *  are the parts with real failure modes — a leaked temp directory and an inherited `GIT_*`
 *  variable — and they depend on node builtins only, so `harness.test.ts` can drive them.
 */
import type { ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** Sleep synchronously. `stop()` is a synchronous callback with no `await` available, and a
 *  teardown that returns before the processes are gone is the bug below. */
function sleepSync(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/** How long `startStack()` may spend booting the whole stack, and how long the Playwright hook
 *  that calls it is given. The ORDER of these two is the invariant, not either number.
 *
 *  They used to be unrelated: the hook allowed 240s while startup could spend three sequential
 *  120s waits, i.e. up to 360s. When the hook wins that race Playwright aborts it mid-await, so
 *  `startStack()` never returns, `stack` is never assigned, and `afterAll` has nothing to call
 *  `stop()` on — leaving three detached process groups and a temp home behind on a shared runner,
 *  which is exactly the leak the rest of this file works to prevent.
 *
 *  Startup therefore runs against ONE overall budget that is comfortably under the hook's, so the
 *  failure surfaces as a normal rejection (cleaned up by `withTempHome`'s fence) rather than as a
 *  hook timeout. `harness.test.ts` pins the ordering.
 */
export const STARTUP_BUDGET_MS = 180_000;
export const HOOK_TIMEOUT_MS = 240_000;


/** SIGKILL each child's process GROUP and wait until node has actually observed them exit.
 *
 *  Async on purpose, and that is the whole point. `process.kill()` returns before the process is
 *  gone, so a teardown that deletes straight afterwards races its own dying children. Waiting
 *  synchronously is impossible here: a sync wait blocks node's event loop, and a blocked loop can
 *  never process the SIGCHLD that reaps its own children — the pids stay valid as zombies and the
 *  wait burns its full deadline every time (written, measured, discarded). Awaiting `exit` works
 *  precisely because it yields the loop, which is what lets the reap happen.
 *
 *  Bounded: a teardown that hangs is worse than one that leaves a directory behind.
 */
export async function killGroupsAndWait(
  procs: ChildProcess[],
  timeoutMs = 5000,
): Promise<void> {
  const dying = procs.map((c) => {
    if (c.exitCode !== null || c.signalCode !== null) return Promise.resolve();
    // Listener BEFORE the signal, or a fast exit fires into nothing and we wait for the deadline.
    const gone = new Promise<void>((resolve) => c.once("exit", () => resolve()));
    try {
      // The GROUP (negative pid): `uv run` execs a child python, so signalling the direct child
      // leaves the real process behind.
      if (c.pid) process.kill(-c.pid, "SIGKILL");
    } catch {
      /* group already gone */
    }
    try {
      c.kill("SIGKILL");
    } catch {
      /* already gone */
    }
    return gone;
  });
  let timer: NodeJS.Timeout | undefined;
  await Promise.race([
    Promise.all(dying),
    new Promise<void>((resolve) => {
      timer = setTimeout(resolve, timeoutMs);
    }),
  ]);
  if (timer) clearTimeout(timer);
}


/** Remove a tree, retrying, because a just-signalled child can recreate files under it.
 *
 *  MEASURED: a leaked `/tmp/bl-appmode-*` from a PASSING run contained `.claude.json` and a
 *  `.claude/backups/…` file — written by the agent AFTER the recursive delete had walked past
 *  that directory. `process.kill(-pid, "SIGKILL")` returns before the group is actually gone, so
 *  teardown removed the tree while its own children were still dying in it.
 *
 *  The obvious fix — wait for the children to exit, then delete — cannot be written here, and
 *  the reason is worth recording so nobody re-attempts it. `stop()` is a synchronous callback, so
 *  the wait would have to be a synchronous one; a synchronous wait blocks node's event loop; and
 *  a blocked event loop can never process the `SIGCHLD` that reaps its own children. The pids
 *  therefore stay valid as ZOMBIES for as long as we wait, `process.kill(pid, 0)` keeps
 *  succeeding, and the loop spins until its deadline every single time. (Written, measured, and
 *  deleted — it failed exactly that way.)
 *
 *  Retrying with a sleep between passes sidesteps all of it: the sleeps give the already-dead
 *  processes time to finish going, and a zombie cannot write a file. Never throws — a cleanup
 *  failure must not replace the real one.
 */
export function removeTree(path: string, attempts = 5): void {
  for (let i = 0; i < attempts; i++) {
    try {
      rmSync(path, { recursive: true, force: true });
      if (!existsSync(path)) return;
    } catch {
      /* retry */
    }
    sleepSync(100);
  }
}

/** Run `body` against a throwaway HOME that is removed if anything in it throws.
 *
 *  The directory is created HERE and handed to the body, which is what makes "every fallible step
 *  is inside the fence" structural rather than a rule someone has to remember. The harness used
 *  to `mkdtempSync` first and seed a git repository afterwards, with the try/catch only around
 *  the process spawns — so a setup command that failed (a git binary that behaves differently, a
 *  full disk) left a `/tmp/bl-appmode-*` behind on every run. On a shared CI runner those
 *  accumulate until it runs out of space or threads, and the failure surfaces on somebody else's
 *  unrelated job.
 *
 *  `onFail` runs BEFORE the directory is removed: anything holding it open (a child process with
 *  it as cwd) has to be killed first, or the `rmSync` races the processes it is cleaning up after.
 */
export async function withTempHome<T>(
  prefix: string,
  body: (home: string) => Promise<T>,
  onFail?: () => void | Promise<void>,
): Promise<T> {
  const home = mkdtempSync(join(tmpdir(), prefix));
  try {
    return await body(home);
  } catch (e) {
    try {
      await onFail?.();
    } catch {
      /* best effort — a failure to tear down must not replace the real error */
    }
    removeTree(home);
    throw e;
  }
}

/** A child environment for fixture `git` calls with every `GIT_*` variable stripped.
 *
 *  Neutralising `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` is not enough: `GIT_DIR`, `GIT_WORK_TREE`,
 *  `GIT_INDEX_FILE` and `GIT_OBJECT_DIRECTORY` all redirect a plain `git init` somewhere else
 *  entirely. Any of them present in the shell — or exported by a CI step, or by a git hook that
 *  invoked the tests — would point the fixture's commands at a repository that is not the
 *  throwaway one, and the first thing this fixture does is `git init` and commit.
 */
export function hermeticGitEnv(
  base: NodeJS.ProcessEnv = process.env,
): Record<string, string> {
  const env = Object.fromEntries(
    Object.entries(base).filter(([k]) => !k.startsWith("GIT_")),
  ) as Record<string, string>;
  env.GIT_CONFIG_GLOBAL = "/dev/null";
  env.GIT_CONFIG_SYSTEM = "/dev/null";
  return env;
}
