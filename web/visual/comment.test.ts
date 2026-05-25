import { describe, expect, it } from "vitest";
// Import the pure helpers from the Node poster script (Node 22 .mjs; vitest transpiles).
import { renderBody, areasOf, viewportsOf, MARKER } from "../../scripts/post-visual-comment.mjs";

const manifest = {
  head_sha: "abcdef1234567890",
  viewports: ["desktop", "mobile"],
  paths: [
    { name: "login", viewport: "desktop", status: "ok", file: "login__desktop.png" },
    { name: "login", viewport: "mobile", status: "ok", file: "login__mobile.png" },
    { name: "app-home", viewport: "desktop", status: "ok", file: "app-home__desktop.png" },
    { name: "app-home", viewport: "mobile", status: "login_failed", file: null, error: "login failed" },
  ],
};

describe("post-visual-comment renderer", () => {
  it("lists areas + viewports in order", () => {
    expect(viewportsOf(manifest)).toEqual(["desktop", "mobile"]);
    expect(areasOf(manifest)).toEqual(["login", "app-home"]);
  });

  it("renders the marker, header, an img cell for uploaded shots, and status for the rest", () => {
    const body = renderBody(manifest, {
      "login__desktop.png": "https://x/a.png",
      "login__mobile.png": "https://x/b.png",
      "app-home__desktop.png": "https://x/c.png",
    });
    expect(body.startsWith(MARKER)).toBe(true);
    expect(body).toContain("📸 Visual snapshot");
    expect(body).toContain("3/4 ok");
    expect(body).toContain("`abcdef12`"); // short sha
    expect(body).toContain('<img src="https://x/a.png"'); // uploaded cell
    expect(body).toContain("_login_failed_"); // missing admin shot
    expect(body).toContain("<details>"); // non-ok detail block
  });

  it("degrades to a status cell when an ok shot has no uploaded url", () => {
    const body = renderBody(manifest, {}); // no uploads at all
    expect(body).toContain("_upload failed_");
    expect(body).not.toContain("<img");
  });

  it("renders an empty (scope=none) manifest as 0/0 with no rows/images — clears a stale snapshot", () => {
    const empty = { head_sha: "abcdef1234567890", viewports: ["desktop", "mobile"], paths: [] };
    const body = renderBody(empty, {}, "commit trailer (none)");
    expect(body).toContain("0/0 ok");
    expect(body).toContain("scope: commit trailer (none)");
    expect(body).not.toContain("<img");
    expect(body).not.toContain("failed to upload");
  });

  it("flags a PARTIAL upload failure explicitly (one ok uploads, another ok doesn't)", () => {
    // login__desktop uploads; the other two ok shots (login__mobile, app-home__desktop) don't.
    const body = renderBody(manifest, { "login__desktop.png": "https://x/a.png" });
    expect(body).toContain('<img src="https://x/a.png"'); // the one that uploaded
    expect(body).toContain("failed to upload"); // visible degraded banner
    expect(body).toContain("2/3 captured screenshot(s) failed to upload");
    expect(body).toContain("upload failed"); // listed in the details
  });
});
