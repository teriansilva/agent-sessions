import { expect, test } from "vitest";
import { ApiError } from "../../lib/api";
import { readError } from "./projectsData";

test("a read error is one clause: the status said once, no closing full stop", () => {
  // The server's message already names the status: it is not repeated.
  expect(readError(new ApiError(503, "GET /api/folders → 503"), "Discovery didn’t answer.")).toBe("GET /api/folders → 503");
  expect(readError(new ApiError(503, "Service unavailable."), "x")).toBe("Service unavailable (HTTP 503)");
  // A bare status message falls back to the caller's words, with the status.
  expect(readError(new ApiError(500, "500"), "The memory store didn’t answer.")).toBe("The memory store didn’t answer (HTTP 500)");
  expect(readError(new Error("boom"), "Discovery didn’t answer.")).toBe("Discovery didn’t answer");
});
