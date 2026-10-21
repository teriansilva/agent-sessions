import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, test } from "vitest";
import { MISSION_PATH } from "../lib/missionLink";
import {
  newestRelease,
  releaseLabel,
  seenCovers,
  whatsNewBundleVersion,
  whatsNewDue,
  whatsNewLabel,
  type DueInput,
} from "./due";
import { DOCS_HOME_URL } from "../lib/links";
import { RELEASES, TEMPLATES_ROUTE, type WhatsNewRelease } from "./releases";
import { atLeast, compareVersions, parseVersion } from "./version";

const R020: WhatsNewRelease = {
  version: "0.20.0",
  assetDir: "whatsnew/0.20",
  slides: [{ id: "intro", eyebrow: "e", title: "t", body: "b" }],
};

const input = (over: Partial<DueInput> = {}): DueInput => ({
  config: { onboarded: true, whats_new_seen: null },
  wizardOpen: false,
  bundle: "0.20.0",
  server: "0.20.0",
  updateReady: false,
  dismissed: new Set(),
  releases: [R020],
  ...over,
});
const due = (over: Partial<DueInput> = {}) => whatsNewDue(input(over)) !== null;

describe("eligibility — the #971 table, bundle stamp = server version", () => {
  test.each([
    ["dev", false],
    ["test", false],
    ["0.0.0+abc1234", false],
    ["", false],
    ["0.19.2", false],
    ["0.19.3.dev4+g8b1c66b", false],
    ["0.20.0.dev1", false],
    ["0.20.0rc1", false],
    ["0.20.0", true],
    ["0.20.0+g1234abc", true],
    ["0.20.0.post1", true],
    ["0.20.1.dev2+g1234abc", true],
  ])("%j → due %s", (version, expected) => {
    expect(due({ bundle: version, server: version })).toBe(expected);
  });

  test("0.21.0 with 0.20.0 seen and no newer entry in this bundle → not due", () => {
    expect(
      due({ bundle: "0.21.0", server: "0.21.0", config: { onboarded: true, whats_new_seen: "0.20.0" } }),
    ).toBe(false);
  });

  test("an unstamped bundle against a released server → not due", () => {
    expect(due({ bundle: "dev", server: "0.20.0" })).toBe(false);
  });

  test("a stale tab, bundle 0.19.2 against server 0.20.0 → not due (it waits for the reload chip)", () => {
    expect(due({ bundle: "0.19.2", server: "0.20.0" })).toBe(false);
  });

  test("the server version is not known yet (pending or failed) → not due", () => {
    expect(due({ server: null })).toBe(false);
  });

  test("a fresh service-worker shell is waiting → not due", () => {
    expect(due({ updateReady: true })).toBe(false);
  });
});

describe("config and tab preconditions", () => {
  test("the key absent from config → not due", () => {
    expect(due({ config: { onboarded: true } })).toBe(false);
  });
  test("no config yet → not due", () => {
    expect(due({ config: null })).toBe(false);
  });
  test.each([false, undefined])("onboarded %s → not due", (onboarded) => {
    expect(due({ config: { onboarded, whats_new_seen: null } })).toBe(false);
  });
  test("a forced password change pending → not due", () => {
    expect(due({ config: { onboarded: true, must_change_password: true, whats_new_seen: null } })).toBe(false);
  });
  test("the wizard or tour is open → not due", () => {
    expect(due({ wizardOpen: true })).toBe(false);
  });
  test.each([
    [null, true],
    ["banana", true],
    ["0.19.0", true],
    ["0.20.0", false],
    ["0.21.0", false],
  ])("whats_new_seen %j → due %s", (seen, expected) => {
    expect(due({ config: { onboarded: true, whats_new_seen: seen } })).toBe(expected);
  });
  test("dismissed in this tab → not due, whatever the server holds", () => {
    expect(due({ dismissed: new Set(["0.20.0"]) })).toBe(false);
  });
});

describe("version order", () => {
  const ordered = [
    "0.9.0",
    "0.10.0",
    "0.19.3.dev4",
    "0.20.0.dev1",
    "0.20.0a1",
    "0.20.0b2",
    "0.20.0rc1",
    "0.20.0",
    "0.20.0.post1",
    "0.20.1.dev2",
    "0.21.0",
  ];
  test("each version sorts strictly after the one before it", () => {
    for (let i = 1; i < ordered.length; i++) {
      const a = parseVersion(ordered[i - 1])!;
      const b = parseVersion(ordered[i])!;
      expect(compareVersions(a, b), `${ordered[i - 1]} < ${ordered[i]}`).toBe(-1);
      expect(compareVersions(b, a)).toBe(1);
    }
  });
  test("a local label does not move a version", () => {
    expect(compareVersions(parseVersion("0.20.0+g1234abc")!, parseVersion("0.20.0")!)).toBe(0);
  });
  test.each([null, 20, "v0.20.0", "0.20", "00.20.0", " 0.20.0", "0.20.0 ", "0.20.0-rc1"])(
    "%j does not parse",
    (v) => {
      expect(parseVersion(v)).toBeNull();
    },
  );
  test("the 0.0.0 fallback is never eligible", () => {
    expect(atLeast("0.0.0", "0.0.0")).toBe(false);
  });
});

describe("helpers", () => {
  test.each([
    ["0.21.0", "0.20.0", true],
    ["0.20.0", "0.20.0", true],
    ["0.9.0", "0.10.0", false],
    [null, "0.20.0", false],
  ])("seenCovers(%j, %j) → %s", (seen, version, expected) => {
    expect(seenCovers(seen, version)).toBe(expected);
  });
  test("newestRelease picks the highest version, whatever the order", () => {
    const older = { ...R020, version: "0.19.0" };
    const newer = { ...R020, version: "0.21.0" };
    expect(newestRelease([older, newer, R020])?.version).toBe("0.21.0");
    expect(newestRelease([])).toBeNull();
  });
  test("labels drop a zero patch", () => {
    expect(releaseLabel("0.20.0")).toBe("0.20");
    expect(releaseLabel("1.2.3")).toBe("1.2.3");
    expect(whatsNewLabel([R020])).toBe("What's new in 0.20");
  });

  describe("the e2e stamp override", () => {
    const g = globalThis as { __BATTLELAB_E2E_BUNDLE_VERSION__?: unknown };
    afterEach(() => {
      delete g.__BATTLELAB_E2E_BUNDLE_VERSION__;
    });
    test("an unstamped build reports dev without it", () => {
      expect(whatsNewBundleVersion("dev")).toBe("dev");
    });
    test("an unstamped build takes it", () => {
      g.__BATTLELAB_E2E_BUNDLE_VERSION__ = "0.20.0";
      expect(whatsNewBundleVersion("dev")).toBe("0.20.0");
    });
    test("a stamped build ignores it", () => {
      g.__BATTLELAB_E2E_BUNDLE_VERSION__ = "9.9.9";
      expect(whatsNewBundleVersion("0.20.0")).toBe("0.20.0");
    });
  });
});

describe("the shipped manifest", () => {
  const newest = newestRelease()!;

  test("0.20.0 is the newest entry, with seven slides", () => {
    expect(newest.version).toBe("0.20.0");
    expect(newest.slides.map((s) => s.id)).toEqual([
      "intro",
      "missions",
      "files",
      "editing",
      "templates",
      "also",
      "moved",
    ]);
    expect(RELEASES.every((r) => /^\d+\.\d+\.\d+$/.test(r.version))).toBe(true);
  });

  test("every illustration exists, animated and still", () => {
    for (const s of newest.slides.filter((x) => x.image)) {
      for (const suffix of ["", "-still"]) {
        const file = resolve(process.cwd(), "public", newest.assetDir, `${s.image}${suffix}.svg`);
        expect(existsSync(file), file).toBe(true);
      }
    }
  });

  test("the CTAs go to Missions, Templates and the docs home — never a page only the launch creates", () => {
    const ctas = newest.slides.flatMap((s) => (s.cta ? [s.cta] : []));
    expect(ctas.map((c) => c.to ?? c.href)).toEqual([MISSION_PATH, TEMPLATES_ROUTE, DOCS_HOME_URL]);
  });

  test("every intro tile names a slide that exists", () => {
    const ids = new Set(newest.slides.map((s) => s.id));
    for (const t of newest.slides[0].tiles ?? []) expect(ids.has(t.slide), t.slide).toBe(true);
  });
});
