/** `newIssueUrl` (#1742): the "Report an issue" link lands on the public repo's new-issue form,
 *  and the only facts it carries off the install are the two it names. */
import { expect, test } from "vitest";

import {
  ISSUE_UA_MAX,
  ISSUE_VERSION_MAX,
  SOURCE_URL,
  newIssueUrl,
} from "./links";

test("opens the public repo's new-issue form with only a prefilled body", () => {
  const url = new URL(
    newIssueUrl({ version: "0.21.0", userAgent: "TestBrowser/1.0" }),
  );
  expect(`${url.origin}${url.pathname}`).toBe(`${SOURCE_URL}/issues/new`);
  // No title (the reporter writes it) and no labels (GitHub ignores them for non-triagers).
  expect([...url.searchParams.keys()]).toEqual(["body"]);
  const body = url.searchParams.get("body")!;
  expect(body).toContain("### What happened");
  expect(body).toContain("### Steps to reproduce");
  expect(body).toContain("- BattleLab: 0.21.0");
  expect(body).toContain("- Browser: TestBrowser/1.0");
});

test("characters that mean something in a URL survive the round trip", () => {
  const ua = "Mozilla/5.0 (X11; Linux) a&b=c#d +e ?f";
  const url = new URL(
    newIssueUrl({ version: "1.0.0+main.abc", userAgent: ua }),
  );
  expect(url.hash).toBe("");
  const body = url.searchParams.get("body")!;
  expect(body).toContain(`- Browser: ${ua}`);
  expect(body).toContain("- BattleLab: 1.0.0+main.abc");
});

test("an unknown version or browser says so instead of leaving a blank", () => {
  const body = new URL(
    newIssueUrl({ version: null, userAgent: "" }),
  ).searchParams.get("body")!;
  expect(body).toContain("- BattleLab: unknown");
  expect(body).toContain("- Browser: unknown");
});

test("a long user agent is cut, so the URL stays bounded", () => {
  const ua = "X".repeat(10_000);
  const raw = newIssueUrl({ version: "v".repeat(10_000), userAgent: ua });
  const url = new URL(raw);
  expect([...url.searchParams.keys()]).toEqual(["body"]);
  const body = url.searchParams.get("body")!;
  expect(body).toContain(`- Browser: ${"X".repeat(ISSUE_UA_MAX)}…`);
  expect(body).not.toContain("X".repeat(ISSUE_UA_MAX + 1));
  expect(raw.length).toBeLessThan(2_000);
});

test("a value exactly at the limit is kept whole; one past it is cut", () => {
  const body = (ua: string, version: string) =>
    new URL(newIssueUrl({ version, userAgent: ua })).searchParams.get("body")!;
  const atUa = "U".repeat(ISSUE_UA_MAX);
  const atVer = "1".repeat(ISSUE_VERSION_MAX);
  expect(body(atUa, atVer)).toContain(`- Browser: ${atUa}\n`);
  expect(body(atUa, atVer)).toContain(`- BattleLab: ${atVer}\n`);
  expect(body(`${atUa}U`, `${atVer}1`)).toContain(`- Browser: ${atUa}…\n`);
  expect(body(`${atUa}U`, `${atVer}1`)).toContain(`- BattleLab: ${atVer}…\n`);
});
