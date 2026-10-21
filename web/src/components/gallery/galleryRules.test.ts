import { describe, expect, it } from "vitest";
import type { GalleryEntry, GalleryIndex, GalleryOrigin } from "../../types/gallery";
import {
  classifyLegacy,
  compareVersions,
  freeId,
  LOCAL_ID,
  updateId,
  updateStatus,
} from "./galleryRules";

const entry = (id: string, version: string, available = true, kind: "playbook" | "skill" = "playbook"): GalleryEntry => ({
  digest: `${id}-${version}`.padEnd(64, "0"),
  available,
  content: {
    format: 1, kind, id, version, title: id, author: "a", publisher: "p", summary: "",
    source: "https://example.com", requirements: [], files: {},
  },
});
const catalog = (entries: GalleryEntry[], state: GalleryIndex["state"] = "ready"): GalleryIndex => ({
  state, entries, error: null,
});
const origin: GalleryOrigin = { state: "verified", kind: "playbook", id: "inbox-triage", version: "1.2.0" };

describe("freeId", () => {
  it("keeps a free valid base", () => {
    expect(freeId("inbox-triage", new Set())).toBe("inbox-triage");
  });
  it("appends the first free number", () => {
    expect(freeId("inbox-triage", new Set(["inbox-triage", "inbox-triage-2"]))).toBe("inbox-triage-3");
  });
  it("stays within 48 characters and valid", () => {
    const long = "a".repeat(48);
    const id = freeId(long, new Set([long]));
    expect(id).toHaveLength(48);
    expect(id.endsWith("-2")).toBe(true);
    expect(LOCAL_ID.test(id)).toBe(true);
  });
  it("normalises an unusable base into a valid ID", () => {
    for (const raw of ["Inbox Triage!", "9lives", "-x", "", "a"]) expect(LOCAL_ID.test(freeId(raw, new Set()))).toBe(true);
  });
});

describe("updateId", () => {
  it("names the version", () => {
    expect(updateId("inbox-triage", "1.3.0", new Set())).toBe("inbox-triage-v1-3-0");
    expect(updateId("inbox-triage", "1.3.0", new Set(["inbox-triage-v1-3-0"]))).toBe("inbox-triage-v1-3-0-2");
  });
});

describe("compareVersions", () => {
  it("orders by semantic-version precedence", () => {
    expect(compareVersions("1.10.0", "1.9.0")).toBeGreaterThan(0);
    expect(compareVersions("1.0.0", "1.0.0-rc.1")).toBeGreaterThan(0);
    expect(compareVersions("1.0.0-rc.2", "1.0.0-rc.10")).toBeLessThan(0);
    expect(compareVersions("1.0.0-alpha", "1.0.0-1")).toBeGreaterThan(0);
    expect(compareVersions("2.0.0", "2.0.0")).toBe(0);
  });
  it("never calls an unparseable version newer", () => {
    expect(compareVersions("garbage", "1.0.0")).toBe(0);
  });
});

describe("updateStatus", () => {
  it("is available only for a newer, available entry of the same kind and id", () => {
    const newer = entry("inbox-triage", "1.3.0");
    expect(updateStatus(origin, catalog([entry("inbox-triage", "1.2.0"), newer]))).toEqual({ state: "available", entry: newer });
  });
  it("picks the newest available version", () => {
    const best = entry("inbox-triage", "2.0.0");
    expect(updateStatus(origin, catalog([entry("inbox-triage", "1.3.0"), best, entry("inbox-triage", "3.0.0", false)]))).toEqual({ state: "available", entry: best });
  });
  it("is current when nothing newer is available", () => {
    expect(updateStatus(origin, catalog([entry("inbox-triage", "1.2.0")]))?.state).toBe("current");
    expect(updateStatus(origin, catalog([entry("other", "9.0.0")]))?.state).toBe("current");
    expect(updateStatus(origin, catalog([entry("inbox-triage", "9.0.0", true, "skill")]))?.state).toBe("current");
  });
  it("reports a withdrawn upstream entry", () => {
    expect(updateStatus(origin, catalog([entry("inbox-triage", "1.2.0", false), entry("inbox-triage", "1.3.0", false)]))?.state).toBe("withdrawn");
  });
  it("never offers an update from an expired, empty or unreadable listing", () => {
    const newer = [entry("inbox-triage", "1.3.0")];
    expect(updateStatus(origin, catalog(newer, "stale"))?.state).toBe("stale");
    expect(updateStatus(origin, catalog([], "empty"))?.state).toBe("unreachable");
    expect(updateStatus(origin, catalog([], "unavailable"))?.state).toBe("unreachable");
    expect(updateStatus(origin, null)?.state).toBe("unreachable");
  });
  it("reports damaged source history", () => {
    expect(updateStatus({ state: "unavailable", error: "x" }, catalog([entry("inbox-triage", "1.3.0")]))?.state).toBe("damaged");
  });
  it("says nothing for a copy that did not come from the gallery", () => {
    expect(updateStatus(null, catalog([]))).toBeNull();
  });
});

describe("classifyLegacy", () => {
  const q = (s: string) => new URLSearchParams(s);
  it("sends a bare link to browsing", () => {
    expect(classifyLegacy(undefined, q(""))).toEqual({ type: "browse" });
  });
  it("keeps an entry digest", () => {
    const digest = "a".repeat(64);
    expect(classifyLegacy(digest, q(""))).toEqual({ type: "entry", digest });
  });
  it("refuses a malformed digest", () => {
    expect(classifyLegacy("not-a-digest", q(""))).toEqual({ type: "invalid" });
  });
  it("resolves a compare link for either kind", () => {
    expect(classifyLegacy(undefined, q("kind=playbook&id=inbox-triage"))).toEqual({ type: "update", kind: "playbook", id: "inbox-triage" });
    expect(classifyLegacy("a".repeat(64), q("kind=skill&id=review"))).toEqual({ type: "update", kind: "skill", id: "review" });
  });
  it("never uses an invalid kind as a destination", () => {
    for (const s of ["kind=widget&id=x", "kind=Playbook&id=x", "id=x", "kind=__proto__&id=x"])
      expect(classifyLegacy(undefined, q(s))).toEqual({ type: "invalid" });
  });
  it("keeps the kind but not an invalid local id", () => {
    expect(classifyLegacy(undefined, q("kind=skill&id=<b>x</b>"))).toEqual({ type: "missing-local", kind: "skill" });
    expect(classifyLegacy(undefined, q("kind=playbook"))).toEqual({ type: "missing-local", kind: "playbook" });
  });
});
