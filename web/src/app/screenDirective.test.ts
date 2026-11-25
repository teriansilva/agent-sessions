import { describe, expect, it } from "vitest";
import { matchRoute } from "../components/ask/askMatches";
import { missionLink } from "../lib/missionLink";
import {
  AUTOMATIONS_PATH,
  DASHBOARD_PATH,
  MAP_PATH,
  artifactPath,
  hqConversationPath,
} from "../lib/routes";
import { SETTINGS_SECTIONS, settingsPath } from "../routes/settingsTabs";
import {
  SCREEN_SECTIONS,
  directiveRoute,
  parseDirective,
  screenLines,
  sectionOf,
  showOf,
  type ScreenOutcome,
  type ScreenShow,
} from "./screenDirective";

const S1 = "claude:00000000-0000-4000-8000-000000000001";
const M1 = `msn_${"a".repeat(32)}`;
const id = (n: number) => `sd_${String(n).padStart(16, "0")}`;
const win = (n: number, key = S1, title = "api-rebase"): ScreenShow =>
  showOf({ id: id(n), op: "open", target: { type: "session", id: key }, where: "map", title })!;
const text = (l: { lead: string; strong: string; tail: string }) => l.lead + l.strong + l.tail;
const lines = (shows: ScreenShow[], outcomes: Record<string, ScreenOutcome> = {}) =>
  screenLines(shows, (i) => (outcomes[i] ? { outcome: outcomes[i], title: "" } : undefined));

describe("a directive → its route (the one table)", () => {
  it("maps each target type and section through the route builders", () => {
    const open = (type: string, target: string, where = "full") =>
      parseDirective({ id: id(1), op: "open", target: { type, id: target }, where })!;
    expect(directiveRoute(open("session", S1))).toBe(matchRoute(S1));
    expect(directiveRoute(open("session", S1, "map"))).toBe(matchRoute(S1));
    expect(directiveRoute(open("mission", M1))).toBe(missionLink(M1));
    expect(directiveRoute(open("artifact", "1234-5678-1234-5678"))).toBe(
      artifactPath("1234-5678-1234-5678"),
    );
    expect(directiveRoute(open("conversation", `ask_${"0".repeat(32)}`))).toBe(
      hqConversationPath(`ask_${"0".repeat(32)}`),
    );
    const go = (section: string) => parseDirective({ id: id(2), op: "go", section })!;
    expect(directiveRoute(go("automations"))).toBe(AUTOMATIONS_PATH);
    expect(directiveRoute(go("dashboard"))).toBe(DASHBOARD_PATH);
    expect(directiveRoute(go("settings"))).toBe(settingsPath());
    expect(directiveRoute(go("settings.agents-defaults"))).toBe(settingsPath("agents-defaults"));
    expect(directiveRoute(parseDirective({ id: id(3), op: "map_arrange" })!)).toBe(MAP_PATH);
  });

  it("knows every Settings page the app lists, by settingsPath()", () => {
    for (const s of SETTINGS_SECTIONS) {
      expect(sectionOf(`settings.${s.id}`)).toEqual({
        label: s.label,
        group: "Settings",
        to: settingsPath(s.id),
      });
    }
    expect(Object.values(SCREEN_SECTIONS).every((s) => s.to.startsWith("/"))).toBe(true);
  });

  it("ignores a member it does not know, and anything malformed", () => {
    const ok = { id: id(1), op: "go", section: "map" };
    expect(parseDirective(ok)).toEqual(ok);
    for (const bad of [
      null,
      "go",
      { ...ok, id: "1" },
      { ...ok, op: "navigate" },
      { ...ok, op: "toString" },
      { ...ok, section: "/settings" },
      { ...ok, section: "https://example.com/" },
      { ...ok, section: "settings.nope" },
      { ...ok, section: "constructor" },
      { id: id(1), op: "open", target: { type: "url", id: "x" }, where: "full" },
      { id: id(1), op: "open", target: { type: "mission", id: M1 }, where: "map" },
      { id: id(1), op: "open", target: { type: "session", id: S1 }, where: "tab" },
      { id: id(1), op: "open", target: { type: "session", id: "" }, where: "full" },
      { id: id(1), op: "map_focus" },
      { id: id(1), op: "map_close", key: "nokey" },
    ]) {
      expect(parseDirective(bad)).toBeNull();
    }
    // A path or URL a message smuggled in beside the closed fields is never carried along.
    expect(parseDirective({ ...ok, path: "/x", url: "https://example.com/" })).toEqual(ok);
  });
});

describe("the line under the answer", () => {
  it("reads Shown only for what this device applied", () => {
    const [line] = lines([win(1)], { [id(1)]: { state: "shown", where: "map" } });
    expect(line.kind).toBe("shown");
    expect(text(line)).toBe("Shown: api-rebase · in the map");
    expect(line.strong).toBe("api-rebase");
  });

  it("reads Asked to show from history, another device or a closed page", () => {
    const [line] = lines([win(1)]);
    expect(line.kind).toBe("asked");
    expect(text(line)).toBe("Asked to show: api-rebase · in the map");
    expect(line.open).toEqual({ type: "sessions", sessions: [{ key: S1, title: "api-rebase" }] });
    // A window still queued for the map is not a result either.
    expect(text(lines([win(1)], { [id(1)]: { state: "pending" } })[0])).toBe(
      "Asked to show: api-rebase · in the map",
    );
  });

  it("says why a map window opened full screen instead", () => {
    expect(text(lines([win(1)], { [id(1)]: { state: "fallback", reason: "host" } })[0])).toBe(
      "Shown: api-rebase · full screen (the map can't host a window here)",
    );
    expect(text(lines([win(1)], { [id(1)]: { state: "fallback", reason: "cap" } })[0])).toBe(
      "Shown: api-rebase · full screen (the map is full)",
    );
  });

  it("words a mission, an artifact, a section and a session opened full screen", () => {
    const one = (raw: object, outcome: ScreenOutcome = { state: "shown", where: "full" }) =>
      text(lines([showOf({ id: id(1), ...raw })!], { [id(1)]: outcome })[0]);
    const open = (type: string, target: string, title = "") => ({
      op: "open",
      target: { type, id: target },
      where: "full",
      title,
    });
    expect(one(open("mission", M1, "Release train v0.24"))).toBe(
      "Shown: Release train v0.24 · mission",
    );
    expect(one(open("artifact", "1234-5678-1234-5678"))).toBe("Shown: an artifact · artifact");
    expect(one(open("session", S1, "api-rebase"))).toBe("Shown: api-rebase · full screen");
    expect(one({ op: "go", section: "automations" })).toBe("Shown: Automations · Library");
    expect(one({ op: "go", section: "dashboard" })).toBe("Shown: Dashboard");
    expect(one({ op: "go", section: "settings.security" })).toBe("Shown: Security · Settings");
    expect(one({ op: "map_arrange" }, { state: "done" })).toBe(
      "Shown: the map · windows arranged",
    );
  });

  it("counts several windows in one line", () => {
    const shows = [win(1), win(2, "claude:b", "b"), win(3, "claude:c", "c")];
    const all = Object.fromEntries(
      shows.map((s) => [s.id, { state: "shown", where: "map" } as ScreenOutcome]),
    );
    const out = lines(shows, all);
    expect(out).toHaveLength(1);
    expect(text(out[0])).toBe("Shown: 3 sessions · in the map");
    expect(out[0].open).toEqual({ type: "route", to: MAP_PATH });
  });

  it("splits a partial fit into what was shown and what was only asked for", () => {
    const shows = [win(1), win(2, "claude:b", "b"), win(3, "claude:c", "c"), win(4, "claude:d", "d")];
    const out = lines(shows, {
      [id(1)]: { state: "shown", where: "map" },
      [id(2)]: { state: "shown", where: "map" },
      [id(3)]: { state: "left", reason: "cap" },
      [id(4)]: { state: "left", reason: "cap" },
    });
    expect(out.map(text)).toEqual([
      "Shown: 2 of 4 sessions · in the map (the map is full)",
      "Asked to show: 2 more sessions",
    ]);
    expect(out.map((l) => l.kind)).toEqual(["shown", "asked"]);
    // Its Open offers the remaining sessions, in the order asked.
    expect(out[1].open).toEqual({
      type: "sessions",
      sessions: [
        { key: "claude:c", title: "c" },
        { key: "claude:d", title: "d" },
      ],
    });
  });

  it("says Not opened when an unsaved-changes guard held it, with no warning kind", () => {
    const show = showOf({ id: id(1), op: "go", section: "automations" })!;
    const [line] = lines([show], { [id(1)]: { state: "held" } });
    expect(line.kind).toBe("held");
    expect(text(line)).toBe("Not opened: you have unsaved changes on this page");
    expect(line.open).toEqual({ type: "route", to: AUTOMATIONS_PATH });
  });

  it("shows a member this build does not know as a link-less note", () => {
    const show = showOf({ id: id(1), op: "teleport", somewhere: "/x" })!;
    expect(show.directive).toBeNull();
    const [line] = lines([show]);
    expect(line.open).toBeNull();
    expect(text(line)).toBe("Asked to show: something this version of the app cannot open");
  });

  it("prefers this device's live title over the stored one, and names an untitled target", () => {
    const out = screenLines([win(1, S1, "")], () => ({
      outcome: { state: "shown", where: "map" },
      title: "live title",
    }));
    expect(out[0].strong).toBe("live title");
    expect(lines([win(1, S1, "")])[0].strong).toBe("a session");
  });
});
