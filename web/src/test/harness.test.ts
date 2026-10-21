/** The app-mode harness's two fixture primitives (#806, review round 3 finding 7).
 *
 *  Both were real leaks rather than hypotheticals: a setup failure left its `/tmp/bl-appmode-*`
 *  home behind because the cleanup fence started after the fixture was built, and the fixture's
 *  `git` calls inherited the caller's `GIT_*` environment.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  hermeticGitEnv,
  HOOK_TIMEOUT_MS,
  killGroupsAndWait,
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
