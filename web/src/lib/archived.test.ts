import { expect, test } from "vitest";
import { ApiError } from "./api";
import { ARCHIVED_DETAIL, isArchivedRefusal } from "./archived";

// #2064: the server refuses a send to an archived session with 409 and exactly the backend's
// `archive_gate.ARCHIVED_DETAIL`. Pinned verbatim: if either side rewords it, the pane would stop
// recognising the refusal and show it as a generic one, so this must fail first.
test("the archived refusal detail is the backend's exact string", () => {
  expect(ARCHIVED_DETAIL).toBe("this session is archived — unarchive it to continue");
});

test("isArchivedRefusal: a 409 carrying exactly that detail, and nothing else", () => {
  expect(isArchivedRefusal(new ApiError(409, ARCHIVED_DETAIL))).toBe(true);
  // Same words, another status: not the archive gate.
  expect(isArchivedRefusal(new ApiError(503, ARCHIVED_DETAIL))).toBe(false);
  // A 409 for another reason (busy, the conversation changed, mission-held).
  expect(isArchivedRefusal(new ApiError(409, "the conversation changed"))).toBe(false);
  expect(isArchivedRefusal(new ApiError(409, `${ARCHIVED_DETAIL}.`))).toBe(false);
  expect(isArchivedRefusal(new Error(ARCHIVED_DETAIL))).toBe(false);
  expect(isArchivedRefusal(null)).toBe(false);
  expect(isArchivedRefusal(undefined)).toBe(false);
  expect(isArchivedRefusal("409")).toBe(false);
});
