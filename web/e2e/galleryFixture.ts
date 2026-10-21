/** A populated public-gallery listing for the visual grid (#1440). Browser-side mocks only, like
 *  `structuredGuidanceFixture.ts`: the capture host has no signed listing, and the server's trust
 *  root is the installed release key — never overridden for a screenshot. Writes are not mocked;
 *  the confirmation capture stops at the dialog. */
import type { Page } from "@playwright/test";

export const GALLERY_FIXTURE_PLAYBOOK = "f".repeat(64);
const SKILL = "9".repeat(64);
const flow = [
  "format = 1",
  'id = "ship"',
  'title = "Issue to live"',
  "",
  "[[steps]]",
  'id = "plan"',
  'title = "Plan the change"',
  'actor = { kind = "agent", engine = "claude" }',
  "",
  "[[steps]]",
  'id = "build"',
  'title = "Implement and test"',
  'actor = { kind = "agent", engine = "codex" }',
  'after = ["plan"]',
  "",
  "[[steps]]",
  'id = "review"',
  'title = "Review the pull request"',
  'actor = { kind = "operator" }',
  'after = ["build"]',
].join("\n");
const playbook = {
  digest: GALLERY_FIXTURE_PLAYBOOK,
  available: true,
  content: {
    format: 1,
    kind: "playbook",
    id: "issue-to-live",
    version: "1.3.0",
    title: "Issue to live",
    author: "BattleLab",
    publisher: "BattleLab",
    summary: "Plan, build, review and ship one issue with a reviewer at the end.",
    source: "https://example.com/issue-to-live",
    requirements: ["A git repository with a main branch", "One coding agent"],
    files: {
      "playbook.toml":
        'format = 2\n[identity]\nid = "issue-to-live"\nname = "Issue to live"\nversion = "1.3.0"\n\n[flows]\ndefault = "ship"\n',
      "flows/ship.toml": flow,
      "AGENTS.md":
        "# How this repository works\n\nOpen an issue first, branch from main, keep pull requests small.\n",
    },
  },
};
const skill = {
  digest: SKILL,
  available: true,
  content: {
    format: 1,
    kind: "skill",
    id: "review-checklist",
    version: "1.0.0",
    title: "Review checklist",
    author: "BattleLab",
    publisher: "BattleLab",
    summary: "A short checklist an agent follows before it calls a change done.",
    source: "https://example.com/review-checklist",
    requirements: [],
    files: { "SKILL.md": "# Review\nRead the change and report concrete findings.\n" },
  },
};
const listing = {
  state: "ready",
  entries: [playbook, skill],
  error: null,
  sequence: 4,
  digest: "e".repeat(64),
  issued_at: 1792000000,
  expires_at: 1892000000,
};

export async function mockGalleryListing(page: Page): Promise<void> {
  await page.route(/\/api\/gallery(\/refresh)?$/, (route) => route.fulfill({ json: listing }));
  await page.route("**/api/gallery/content/**", (route) => {
    const shown = route.request().url().includes(SKILL) ? skill : playbook;
    return route.fulfill({
      json: {
        entry: shown,
        importable: true,
        reason: null,
        cut_digest: listing.digest,
        preview:
          shown === playbook
            ? {
                inputs: [
                  { name: "repo", label: "Repository", required: true },
                  { name: "reviewer", label: "Reviewer", required: false },
                ],
                flows: [
                  {
                    id: "ship",
                    title: "Issue to live",
                    steps: [
                      { id: "plan", title: "Plan the change", actor: { kind: "agent", engine: "claude" }, after: [], note: false },
                      { id: "build", title: "Implement and test", actor: { kind: "agent", engine: "codex" }, after: ["plan"], note: false },
                      { id: "review", title: "Review the pull request", actor: { kind: "operator" }, after: ["build"], note: false },
                    ],
                  },
                ],
              }
            : null,
      },
    });
  });
}
