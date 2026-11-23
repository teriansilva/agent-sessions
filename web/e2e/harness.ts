/** Fixture primitives for the app-mode E2E harness (#806, #1147).
 *
 *  Separate from `appmode.ts` on purpose: that module imports `@playwright/test`, which can only
 *  be loaded inside a Playwright run, so nothing in it can be unit-tested. The parts with real
 *  failure modes — a leaked temp directory, an escaped `dtach` master, an inherited `GIT_*`
 *  variable and the agent fixture's fail-closed selection — depend on node builtins only, so
 *  `src/test/harness.test.ts` can drive them.
 */
import type { ChildProcess } from "node:child_process";
import { constants, accessSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";

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

/** The stand-in agent the app-mode stack launches (#1147).
 *
 *  Fail closed: a missing or non-executable fixture is a refusal to boot, not a reason to fall
 *  back to the operator's real `claude` (the leak in #1147 launched a real, spinning agent from
 *  `~/.local/bin` on every run). The path is absolute because the manifest's `binary.env_var`
 *  override requires one.
 */
export function appmodeAgentBinary(cwd = process.cwd()): string {
  const path = resolve(cwd, "e2e", "fake-agent.sh");
  try {
    accessSync(path, constants.X_OK);
  } catch {
    throw new Error(
      `the app-mode agent fixture is missing or not executable: ${path}. ` +
        "Refusing to boot a stack that could fall back to the host's real agent.",
    );
  }
  return path;
}

interface ProcStat {
  state: string;
  ppid: number;
  pgid: number;
}

/** `/proc/<pid>/stat` without assuming anything about `comm` (which may hold spaces/brackets). */
function procStat(pid: number): ProcStat | null {
  try {
    const raw = readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = raw.slice(raw.lastIndexOf(")") + 2).split(" ");
    if (fields.length < 3) return null;
    const ppid = Number(fields[1]);
    const pgid = Number(fields[2]);
    if (!Number.isInteger(ppid) || !Number.isInteger(pgid)) return null;
    return { state: fields[0], ppid, pgid };
  } catch {
    return null; // gone, or unreadable; callers distinguish the two where it matters
  }
}

/** A live (non-zombie) process. A pid that exists only as a zombie has already exited. */
function procAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 1) return false;
  const st = procStat(pid);
  return st !== null && st.state !== "Z";
}

/** `/proc/<pid>/cmdline` as argv, or null when it cannot be read. */
function procArgv(pid: number): string[] | null {
  try {
    return readFileSync(`/proc/${pid}/cmdline`, "utf8")
      .split("\0")
      .filter((a) => a.length > 0);
  } catch {
    return null;
  }
}

/** `/proc/<pid>/environ` as `KEY=VALUE` strings, or null when it cannot be read. */
function procEnv(pid: number): string[] | null {
  try {
    return readFileSync(`/proc/${pid}/environ`, "utf8")
      .split("\0")
      .filter((a) => a.length > 0);
  } catch {
    return null;
  }
}

function isUnder(path: string, dir: string): boolean {
  return path === dir || path.startsWith(dir.endsWith("/") ? dir : `${dir}/`);
}

/** Is this argv element exactly a socket path under THIS run's runtime dir? */
function runSocketArg(arg: string, runtimeDir: string): boolean {
  const dir = resolve(runtimeDir);
  return dirname(arg) === dir && basename(arg).endsWith(".sock");
}

/** Does this argv describe a `dtach` CREATE master for one of this run's sockets?
 *
 *  Create-only (`-c`/`-n`): a `dtach -a` reader is a client, not the master that owns the agent.
 *  The socket must be an EXACT argv element under `runtimeDir` — never a substring, and never the
 *  seeded UUID, so another run (or another project's master) cannot match.
 */
function isRunOwnedMaster(argv: string[], runtimeDir: string): boolean {
  if (!argv.includes("-c") && !argv.includes("-n")) return false;
  return argv.some((a) => runSocketArg(a, runtimeDir));
}

/** Every `dtach` master whose create argv names a socket under THIS run's runtime dir. */
export function findRunOwnedMasters(runtimeDir: string): number[] {
  if (process.platform !== "linux") return [];
  const dir = resolve(runtimeDir);
  let names: string[];
  try {
    names = readdirSync("/proc");
  } catch {
    return [];
  }
  const pids: number[] = [];
  for (const name of names) {
    if (!/^\d+$/.test(name)) continue;
    const pid = Number(name);
    if (pid === process.pid) continue;
    const argv = procArgv(pid);
    if (argv && isRunOwnedMaster(argv, dir)) pids.push(pid);
  }
  return pids;
}

/** The process tree rooted at `roots`, captured in one procfs pass (ppid -> children). */
function procTree(roots: number[]): { pids: Set<number>; pgids: Set<number> } {
  const pids = new Set<number>(roots);
  const pgids = new Set<number>();
  if (!pids.size) return { pids, pgids };
  const children = new Map<number, number[]>();
  let names: string[];
  try {
    names = readdirSync("/proc");
  } catch {
    return { pids, pgids };
  }
  for (const name of names) {
    if (!/^\d+$/.test(name)) continue;
    const st = procStat(Number(name));
    if (st === null) continue;
    if (!children.has(st.ppid)) children.set(st.ppid, []);
    children.get(st.ppid)!.push(Number(name));
  }
  const queue = [...pids];
  while (queue.length) {
    const parent = queue.shift()!;
    for (const child of children.get(parent) ?? []) {
      if (!pids.has(child)) {
        pids.add(child);
        queue.push(child);
      }
    }
  }
  // Group ids belong to the TREE only — never to every process in the snapshot.
  for (const pid of pids) {
    const st = procStat(pid);
    if (st !== null) pgids.add(st.pgid);
  }
  return { pids, pgids };
}

function ownPgid(): number {
  return procStat(process.pid)?.pgid ?? -1;
}

/** Wait until every pid in `pids` is gone (or a zombie), or the bounded timeout expires. */
async function waitGone(pids: Set<number>, timeoutMs: number): Promise<number[]> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const alive = [...pids].filter((pid) => Number.isInteger(pid) && procAlive(pid));
    if (!alive.length) return [];
    if (Date.now() >= deadline) return alive;
    await new Promise((r) => setTimeout(r, 50));
  }
}

/** SIGKILL every `dtach` master under `runtimeDir` and its whole process tree, and WAIT.
 *
 *  The master deliberately escapes its spawner (`setsid`, the session-continuity guarantee), so a
 *  group kill of the app's own children never reaches it. Identity is the run's OWN socket path
 *  in the master's argv — never a process name, never `pkill -f`, and never the seeded UUID that
 *  every run shares. Each pid is re-checked immediately before the signal so a reused pid cannot
 *  be hit; the process TREE is captured before anything dies, because `dtach` setsid()s its agent
 *  into a DIFFERENT group, so the master's group alone is not the boundary.
 *
 *  Returns the pids that were still alive at the deadline as `unverified`: a teardown that could
 *  not prove them gone must not report "clean".
 */
export async function killRunOwnedMasters(
  runtimeDir: string,
  timeoutMs = 5000,
): Promise<{ killed: number[]; unverified: number[] }> {
  const dir = resolve(runtimeDir);
  const roots = findRunOwnedMasters(dir);
  if (!roots.length) return { killed: [], unverified: [] };
  const { pids, pgids } = procTree(roots);
  const rootSet = new Set(roots);
  const tree = pids;
  const own = ownPgid();
  const killed: number[] = [];

  // Validate, then signal. Groups are signalled only when their leader is a member of the tree
  // and is still that process (so a reused group id is never hit), and never pid 1 or our own.
  for (const pid of tree) {
    if (rootSet.has(pid)) {
      const argv = procArgv(pid);
      if (!argv || !isRunOwnedMaster(argv, dir)) continue; // reused / no longer ours
    } else {
      const st = procStat(pid);
      if (st === null || st.state === "Z") continue;
      if (!tree.has(st.ppid) && !tree.has(st.pgid)) continue; // reused / no longer ours
    }
    try {
      process.kill(pid, "SIGKILL");
      killed.push(pid);
    } catch {
      /* already gone */
    }
  }
  for (const pgid of pgids) {
    if (!tree.has(pgid) || pgid <= 1 || pgid === own) continue;
    if (!procAlive(pgid)) continue;
    try {
      process.kill(-pgid, "SIGKILL");
    } catch {
      /* group already gone */
    }
  }
  const unverified = await waitGone(tree, timeoutMs);
  return { killed, unverified };
}

/** Every live process that still references this run's runtime dir (argv or environment).
 *
 *  The agent's environment carries `AGENT_SESSIONS_RUNTIME_DIR`, and the master's argv carries
 *  its socket: checking both catches a master AND the agent under it, which "no socket file
 *  remains" alone would miss (it can pass after an unlink while the child spins on).
 */
export function processesReferencing(runtimeDir: string): number[] {
  if (process.platform !== "linux") return [];
  const dir = resolve(runtimeDir);
  let names: string[];
  try {
    names = readdirSync("/proc");
  } catch {
    return [];
  }
  const found: number[] = [];
  for (const name of names) {
    if (!/^\d+$/.test(name)) continue;
    const pid = Number(name);
    if (pid === process.pid) continue;
    const argv = procArgv(pid);
    const env = procEnv(pid);
    if (argv === null && env === null) continue; // gone or unreadable; no evidence it is ours
    const inArgv = !!argv?.some((a) => a === dir || isUnder(a, dir));
    const inEnv = !!env?.some((e) => {
      const eq = e.indexOf("=");
      return eq > 0 && isUnder(e.slice(eq + 1), dir);
    });
    if (inArgv || inEnv) found.push(pid);
  }
  return found;
}

/** Teardown for a run's OWN runtime: kill its masters + trees, then PROVE the run is clean.
 *
 *  Runs before the temp home/runtime dir is deleted, and throws loudly rather than deleting over
 *  a process that survived — "unreadable" or "timed out" is a failure, never a clean teardown.
 */
export async function reapRunOwnedMasters(runtimeDir: string, timeoutMs = 5000): Promise<void> {
  const { unverified } = await killRunOwnedMasters(runtimeDir, timeoutMs);
  if (unverified.length) {
    throw new Error(
      `app-mode teardown could not prove these pids gone for runtime dir ${runtimeDir}: ` +
        `${unverified.join(", ")}`,
    );
  }
  const left = processesReferencing(runtimeDir);
  if (left.length) {
    throw new Error(
      `app-mode teardown left processes referencing runtime dir ${runtimeDir}: ${left.join(", ")}`,
    );
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
