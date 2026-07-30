import { expect, test } from "vitest";
import { sessionStatus } from "./sessionStatus";
import type { Session } from "../types/api";

/** #744: extracted from SessionList so the sidebar row and the session brief resolve one status
 *  from one row. These pin the precedence — the sidebar's dot and the brief's dot are now the
 *  same function, so a regression here is a regression in both places. */

function row(over: Partial<Session> = {}): Session {
  return {
    id: "claude:abc",
    engine: "claude",
    uuid: "abc",
    short_uuid: "abc",
    cwd: "/p",
    project: { kind: "folder", id: "/p", name: "/p" },
    last_mtime: 0,
    first_user_message: "",
    title: "t",
    sticky: false,
    archived: false,
    ...over,
  } as unknown as Session;
}

test("intervention wins over working and draft (#744)", () => {
  const s = sessionStatus(
    row({
      intervention_required: true,
      intervention_reason: "waiting on permission",
      working: true,
      has_draft: true,
    }),
  );
  expect(s.variant).toBe("attention");
  expect(s.label).toBe("intervention required: waiting on permission");
});

test("an excluded session never shows the intervention dot (#744)", () => {
  const s = sessionStatus(
    row({ intervention_required: true, review_excluded: true, working: true }),
  );
  expect(s.variant).toBe("up");
});

test("working beats an unsent draft (#744)", () => {
  expect(sessionStatus(row({ working: true, has_draft: true })).variant).toBe("up");
});

test("an unsent draft beats idle (#744)", () => {
  expect(sessionStatus(row({ has_draft: true })).variant).toBe("draft");
});

test("idle carries no role and no accessible name — it is decorative (#744)", () => {
  const s = sessionStatus(row());
  expect(s.variant).toBe("idle");
  expect(s.role).toBeUndefined();
  expect(s.label).toBeUndefined();
});

test("an intervention with no reason still names itself (#744)", () => {
  const s = sessionStatus(row({ intervention_required: true, intervention_reason: "" }));
  expect(s.label).toBe("intervention required: see session");
  expect(s.title).toBe("Intervention required");
});
