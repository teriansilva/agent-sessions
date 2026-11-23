/** The app-mode harness's fixture primitives (#806, review round 3 finding 7; #1147).
 *
 *  These were real leaks rather than hypotheticals: a setup failure left its `/tmp/bl-appmode-*`
 *  home behind because the cleanup fence started after the fixture was built; the fixture's
 *  `git` calls inherited the caller's `GIT_*` environment; and a detached `dtach` master (with a
 *  real agent under it) escaped the app's group kill on every run.
 */
import { execFileSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  appmodeAgentBinary,
  findRunOwnedMasters,
  hermeticGitEnv,
  HOOK_TIMEOUT_MS,
  killGroupsAndWait,
  killRunOwnedMasters,
  processesReferencing,
  reapRunOwnedMasters,
  removeTree,
  STARTUP_BUDGET_MS,
  withTempHome,
} from "../../e2e/harness";

describe("withTempHome", () => {
  it("removes the home when the body throws", async () => {
    let created = "";
    await expect(
      withTempHome("bl-harness-test-", async (home) => {
        created = home;
        // Fail the way a real setup step fails: after the directory exists and has content in it.
        mkdirSync(join(home, "proj"), { recursive: true });
        writeFileSync(join(home, "proj", "a.txt"), "one\n");
        throw new Error("git init failed");
      }),
    ).rejects.toThrow("git init failed");

    expect(created).not.toBe("");
    expect(existsSync(created)).toBe(false);
  });

  it("keeps the home when the body succeeds — teardown is the caller's job then", async () => {
    const home = await withTempHome("bl-harness-test-", async (h) => h);
    expect(existsSync(home)).toBe(true);
    // The success path hands ownership to the returned stack's `stop()`, so this must NOT clean
    // up: a fence that also removed the home on success would delete the stack under the test.
    await withTempHome("bl-harness-test-", async () => undefined);
  });

  it("kills the children BEFORE removing the home", async () => {
    // Ordering is load-bearing: a child whose cwd is the home keeps it busy, so removing first
    // and killing second races the very processes it is cleaning up after.
    const order: string[] = [];
    let created = "";
    await expect(
      withTempHome(
        "bl-harness-test-",
        async (home) => {
          created = home;
          throw new Error("boom");
        },
        () => order.push("killed"),
      ),
    ).rejects.toThrow("boom");
    expect(order).toEqual(["killed"]);
    expect(existsSync(created)).toBe(false);
  });
});

describe("hermeticGitEnv", () => {
  it("strips every GIT_* variable, not just the two config ones", () => {
    const env = hermeticGitEnv({
      PATH: "/usr/bin",
      GIT_DIR: "/somewhere/else/.git",
      GIT_WORK_TREE: "/somewhere/else",
      GIT_INDEX_FILE: "/tmp/idx",
      GIT_OBJECT_DIRECTORY: "/tmp/obj",
      GIT_AUTHOR_NAME: "someone",
    });
    expect(env.PATH).toBe("/usr/bin");
    const leaked = Object.keys(env).filter(
      (k) => k.startsWith("GIT_") && !["GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"].includes(k),
    );
    // GIT_DIR alone is enough to point `git init` at a repository that is not the fixture's.
    expect(leaked).toEqual([]);
  });

  it("still neutralises the user's global and system config", () => {
    const env = hermeticGitEnv({ PATH: "/usr/bin" });
    expect(env.GIT_CONFIG_GLOBAL).toBe("/dev/null");
    expect(env.GIT_CONFIG_SYSTEM).toBe("/dev/null");
  });
});

describe("removeTree", () => {
  it("removes a populated tree", () => {
    const dir = mkdtempSync(join(tmpdir(), "bl-harness-rm-"));
    mkdirSync(join(dir, "a", "b"), { recursive: true });
    writeFileSync(join(dir, "a", "b", "f.txt"), "x");
    removeTree(dir);
    expect(existsSync(dir)).toBe(false);
  });

  it("does not throw when the tree is already gone", () => {
    const dir = mkdtempSync(join(tmpdir(), "bl-harness-rm-"));
    removeTree(dir);
    expect(() => removeTree(dir)).not.toThrow();
  });
});

describe("killGroupsAndWait", () => {
  it("does not return until node has observed the child exit", async () => {
    // The assertion that distinguishes awaiting from firing-and-returning: `exit` can only be
    // emitted on a later event-loop turn, so a version that signals and returns leaves both
    // `exitCode` and `signalCode` null. Deterministic — not a race on how fast SIGKILL lands.
    const child = spawn("sleep", ["30"], { detached: true });
    await new Promise((r) => child.once("spawn", r));
    expect(child.exitCode).toBeNull();
    expect(child.signalCode).toBeNull();

    await killGroupsAndWait([child]);

    expect(child.exitCode !== null || child.signalCode !== null).toBe(true);
  });

  it("reaps the process so its pid is really gone", async () => {
    const child = spawn("sleep", ["30"], { detached: true });
    await new Promise((r) => child.once("spawn", r));
    const pid = child.pid!;
    const alive = () => {
      try {
        process.kill(pid, 0);
        return true;
      } catch {
        return false;
      }
    };
    expect(alive()).toBe(true); // guard: the probe works

    await killGroupsAndWait([child]);
    // Awaiting is what allows the reap — a blocked loop would leave this a zombie and `alive()`
    // would still be true.
    expect(alive()).toBe(false);
  });

  it("returns promptly for a child that already exited", async () => {
    const child = spawn("true", [], { detached: true });
    await new Promise((r) => child.once("exit", r));
    const started = Date.now();
    await killGroupsAndWait([child], 5000);
    expect(Date.now() - started).toBeLessThan(1000);
  });
});

describe("startup budget vs hook timeout (#806, review round 6)", () => {
  it("leaves the hook enough room to see a slow boot fail", () => {
    // The ordering IS the invariant. If startup may run longer than the hook that awaits it,
    // Playwright kills the hook mid-await: `startStack()` never returns, `stack` is never
    // assigned, and `afterAll` has no handle to stop three detached process groups with.
    expect(STARTUP_BUDGET_MS).toBeLessThan(HOOK_TIMEOUT_MS);
    // ...and with real margin, not by a second — the boot still has to reject, unwind its
    // cleanup fence, and let the hook report.
    expect(HOOK_TIMEOUT_MS - STARTUP_BUDGET_MS).toBeGreaterThanOrEqual(30_000);
  });
});

/** A fake `dtach` master for the Linux regression tests.
 *
 *  It is a Node process whose argv is `<script> -n <socket>` — the same create-master shape
 *  `dtach -n <sock>` has — and it spawns a DETACHED child (`setsid`-like), because the real
 *  `dtach` calls `setsid()` on its agent: the master and agent are in different process groups,
 *  which is precisely why the app's group kill never reached them (#1147).
 *
 *  The fake master is itself a leak risk, so every test that starts one owns its whole tree:
 *  the `finally` fence reaps the recorded master AND child by validated pid/pgid, waits for them
 *  to be gone, and only then removes the runtime and script directories. The script dir is
 *  recorded here so the fence can remove it too.
 */
const MASTER_SOURCE = `
const { spawn } = require("node:child_process");
const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
  detached: true,
  stdio: "ignore",
});
process.stdout.write("CHILD=" + child.pid + "\\n");
setInterval(() => {}, 1000);
`;

interface FakeMaster {
  pid: number;
  child: number;
  runtimeDir: string;
  scriptDir: string;
  exited: Promise<number | null>;
}

interface ProcStat {
  state: string;
  pgid: number;
}

function procStat(pid: number): ProcStat | null {
  try {
    const raw = readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = raw.slice(raw.lastIndexOf(")") + 2).split(" ");
    if (fields.length < 3) return null;
    const pgid = Number(fields[2]);
    return Number.isInteger(pgid) ? { state: fields[0], pgid } : null;
  } catch {
    return null;
  }
}

function ownedPgid(): number {
  return procStat(process.pid)?.pgid ?? -1;
}

/** Does this pid still carry a marker for `runtimeDir` (its env, or an argv path under it)? */
function referencesRuntimeDir(pid: number, runtimeDir: string): boolean {
  const dir = resolve(runtimeDir);
  try {
    const env = readFileSync(`/proc/${pid}/environ`, "utf8").split("\0");
    if (env.includes(`AGENT_SESSIONS_RUNTIME_DIR=${dir}`)) return true;
  } catch {
    /* gone */
  }
  try {
    const argv = readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0");
    return argv.some((a) => a === dir || a.startsWith(`${dir}/`));
  } catch {
    return false;
  }
}

/** SIGKILL one validated pid (and its own group), never pid 1 and never our group. */
function killValidated(pid: number, runtimeDir: string): void {
  if (!Number.isInteger(pid) || pid <= 1) return;
  if (!referencesRuntimeDir(pid, runtimeDir)) return;
  const st = procStat(pid);
  const own = ownedPgid();
  if (st !== null && st.pgid === pid && st.pgid !== own && st.pgid > 1) {
    try {
      process.kill(-pid, "SIGKILL");
    } catch {
      /* group already gone */
    }
  }
  try {
    process.kill(pid, "SIGKILL");
  } catch {
    /* already gone */
  }
}

/** True once a pid has exited: absent, or a zombie (already dead, awaiting its reaper). */
function gone(pid: number): boolean {
  const st = procStat(pid);
  return st === null || st.state === "Z";
}

async function waitGone(pids: number[], timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (pids.some((pid) => !gone(pid))) {
    if (Date.now() >= deadline) {
      throw new Error(`helper processes did not exit: ${pids.filter((pid) => !gone(pid)).join(", ")}`);
    }
    await new Promise((r) => setTimeout(r, 50));
  }
}

/** Reap a fake master's whole tree, including the recorded detached child.
 *
 *  Pids are re-checked against this run's marker immediately before the signal, and group kills
 *  are limited to a group leader that is that same pid — never pid 1, never our own group.
 */
async function reapFakeMaster(fake: FakeMaster, runtimeDir: string): Promise<void> {
  // Anything still naming this run's dir, plus the recorded child even if its master died and
  // reparented it. Both are validated, so a reused pid cannot be hit.
  const candidates = new Set<number>([fake.pid, fake.child, ...processesReferencing(runtimeDir)]);
  for (const pid of candidates) killValidated(pid, runtimeDir);
  await Promise.race([
    fake.exited,
    new Promise<void>((r) => setTimeout(r, 1000)),
  ]);
  await waitGone([...candidates]);
}

/** The fence for a partial `startFakeMaster` failure: reap whatever it managed to start. */
async function reapAnyUnder(runtimeDir: string): Promise<void> {
  const candidates = processesReferencing(runtimeDir);
  for (const pid of candidates) killValidated(pid, runtimeDir);
  await waitGone(candidates);
}

async function startFakeMaster(runtimeDir: string, socketName = "claude-seeded.sock"): Promise<FakeMaster> {
  const scriptDir = mkdtempSync(join(tmpdir(), "bl-harness-master-"));
  const script = join(scriptDir, "master.js");
  writeFileSync(script, MASTER_SOURCE);
  try {
    const child = spawn(process.execPath, [script, "-n", join(runtimeDir, socketName)], {
      detached: true,
      stdio: ["ignore", "pipe", "ignore"],
      env: { ...process.env, AGENT_SESSIONS_RUNTIME_DIR: runtimeDir },
    });
    await new Promise((r) => child.once("spawn", r));
    const childPid = await new Promise<number>((res, rej) => {
      let buf = "";
      child.stdout!.on("data", (d: Buffer) => {
        buf += d.toString();
        const m = buf.match(/CHILD=(\d+)/);
        if (m) res(Number(m[1]));
      });
      child.once("exit", () => rej(new Error("fake master exited before printing its child")));
    });
    await new Promise((r) => setTimeout(r, 50)); // let /proc show the child under the master
    return {
      pid: child.pid!,
      child: childPid,
      runtimeDir,
      scriptDir,
      exited: new Promise((r) => child.once("exit", r)),
    };
  } catch (e) {
    // Partial setup: the fence starts BEFORE the first master, so nothing this call started
    // survives a failed spawn/parse.
    await reapAnyUnder(runtimeDir);
    removeTree(scriptDir);
    throw e;
  }
}

async function stopQuietly(pid: number | undefined, timeoutMs = 5000): Promise<void> {
  if (!pid || !Number.isInteger(pid) || pid <= 1) return;
  const st = procStat(pid);
  const own = ownedPgid();
  if (st !== null && st.pgid === pid && st.pgid !== own && st.pgid > 1) {
    try {
      process.kill(-pid, "SIGKILL");
    } catch {
      /* group already gone */
    }
  }
  try {
    process.kill(pid, "SIGKILL");
  } catch {
    /* already gone */
  }
  await waitGone([pid], timeoutMs);
}

describe("appmode agent fixture (#1147)", () => {
  it("resolves the absolute, executable stand-in and refuses to fall back", () => {
    const path = appmodeAgentBinary();
    expect(path).toBe(resolve(process.cwd(), "e2e", "fake-agent.sh"));
    expect(existsSync(path)).toBe(true);
    // It really runs: this is the fixture, not a missing path that provenance would silently
    // treat as "agent absent" (which would leave app-mode specs green with no agent).
    expect(execFileSync(path, ["--version"], { encoding: "utf8" })).toContain("claude");

    const empty = mkdtempSync(join(tmpdir(), "bl-harness-no-agent-"));
    expect(() => appmodeAgentBinary(empty)).toThrow(/Refusing to boot/);
  });
});

describe.skipIf(process.platform !== "linux")("run-owned dtach teardown (#1147)", () => {
  it("kills THIS run's master and its detached agent, and leaves another run's session alone", async () => {
    const ours = mkdtempSync(join(tmpdir(), "bl-harness-rt-"));
    const control = mkdtempSync(join(tmpdir(), "bl-harness-rt-control-"));
    const started: FakeMaster[] = [];
    // The fence exists BEFORE the first master starts, so a partial setup failure is covered too.
    try {
      const mine = await startFakeMaster(ours);
      started.push(mine);
      const other = await startFakeMaster(control, "claude-seeded.sock");
      started.push(other);

      // Selection is by the run's OWN socket path, not by name or the shared seeded UUID.
      expect(findRunOwnedMasters(ours)).toContain(mine.pid);
      expect(findRunOwnedMasters(ours)).not.toContain(other.pid);
      expect(findRunOwnedMasters(control)).toContain(other.pid);
      // Both the master and the agent under it carry this run's runtime dir.
      expect(processesReferencing(ours)).toEqual(expect.arrayContaining([mine.pid, mine.child]));

      const { killed, unverified } = await killRunOwnedMasters(ours);
      expect(unverified).toEqual([]);
      expect(killed).toEqual(expect.arrayContaining([mine.pid, mine.child]));
      expect(processesReferencing(ours)).toEqual([]);
      // The concurrent, unrelated run is untouched.
      expect(findRunOwnedMasters(control)).toContain(other.pid);
      expect(processesReferencing(control)).toContain(other.pid);
    } finally {
      // Both trees, INCLUDING each recorded detached child — the child is in its own group and
      // the master's group kill never reaches it. Wait for confirmed exit before removing dirs.
      await reapAnyUnder(ours); // covers a `startFakeMaster(ours)` that did not return a handle
      await reapAnyUnder(control);
      for (const fake of started) await reapFakeMaster(fake, fake.runtimeDir);
      for (const pid of started.flatMap((f) => [f.pid, f.child])) await waitGone([pid]);
      removeTree(ours);
      removeTree(control);
      for (const fake of started) removeTree(fake.scriptDir);
      // Post-cleanup proof: no helper child or master still names either run.
      expect(processesReferencing(ours)).toEqual([]);
      expect(processesReferencing(control)).toEqual([]);
      for (const pid of started.flatMap((f) => [f.pid, f.child])) expect(gone(pid)).toBe(true);
    }
  });

  it("is a no-op for a run that never got as far as a runtime dir", async () => {
    // The startup rollback runs this fence even when boot failed before `mkdtemp`. It must not
    // throw over a directory that does not exist.
    const missing = join(tmpdir(), "bl-harness-rt-never-created");
    await expect(reapRunOwnedMasters(missing)).resolves.toBeUndefined();
  });

  it("does not match a process that merely mentions the socket path", async () => {
    const dir = mkdtempSync(join(tmpdir(), "bl-harness-rt-"));
    const sock = join(dir, "claude-seeded.sock");
    // Exact argv element is required: a substring (and `-a`, an attach CLIENT) is not a master.
    const decoy = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)", "--sock=" + sock], {
      detached: true,
      stdio: "ignore",
    });
    await new Promise((r) => decoy.once("spawn", r));
    try {
      expect(findRunOwnedMasters(dir)).toEqual([]);
    } finally {
      await stopQuietly(decoy.pid!);
      removeTree(dir);
    }
  });

  it("fails loudly when a process still references the run's runtime dir", async () => {
    const dir = mkdtempSync(join(tmpdir(), "bl-harness-rt-"));
    // No socket argv, so it is not a master — but its environment names this run's dir, which is
    // exactly what an agent under a leaked master looks like to the post-teardown check.
    const stray = spawn("sleep", ["60"], {
      detached: true,
      stdio: "ignore",
      env: { ...process.env, AGENT_SESSIONS_RUNTIME_DIR: dir },
    });
    await new Promise((r) => stray.once("spawn", r));
    try {
      expect(processesReferencing(dir)).toContain(stray.pid!);
      await expect(reapRunOwnedMasters(dir)).rejects.toThrow(/left processes referencing/);
    } finally {
      await stopQuietly(stray.pid!);
      removeTree(dir);
    }
  });
});
