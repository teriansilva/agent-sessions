import { describe, expect, it } from "vitest";
import {
  ancestors,
  differences,
  draftFiles,
  fromDetail,
  newDraft,
  type Flow,
  type Manifest,
} from "./playbookDraft";
import type { PlaybookDetail } from "../../types/playbooks";

const original = (): PlaybookDetail => ({
  id: "local-flow",
  source: "local",
  editable: true,
  ok: true,
  error: null,
  default: false,
  revision: "a".repeat(64),
  recovery_total: 0,
  files: {
    "playbook.toml": "# keep this comment\nformat = 3\n",
    "flows/main.toml": "# omitted model remains omitted\n",
    "README.md": "Read this",
    "template/raw.bin": { base64: "AP/+" },
    "runbooks/guide.md": "+++\ntitle = 'Guide'\n+++\nKeep me",
  },
  documents: {
    "playbook.toml": {
      format: 3,
      identity: {
        id: "local-flow",
        name: "Local",
        publisher: "Me",
        version: "1.0.0",
        domain: "mail",
      },
      materials: [{ path: "raw.bin", disposition: "seed" }],
    },
    "flows/main.toml": {
      format: 1,
      title: "Main",
      steps: [
        {
          id: "read",
          title: "Read",
          actor: { kind: "agent", engine: "retiring-agent" },
          skills: [{ id: "private-skill", required: true }],
          memory: "read",
        },
      ],
    },
  },
  readme: "Read this",
});

describe("complete playbook draft", () => {
  it("preserves every untouched file byte and original format", () => {
    const before = fromDetail(original());
    const draft = structuredClone(before);
    (draft.documents["playbook.toml"] as Manifest).identity.name = "Edited";
    const files = draftFiles(draft, before);
    expect(files["playbook.toml"]).toEqual({
      toml: draft.documents["playbook.toml"],
    });
    for (const path of Object.keys(before.files).filter(
      (p) => p !== "playbook.toml",
    ))
      expect(files[path]).toEqual(before.files[path]);
    expect((files["playbook.toml"] as { toml: Manifest }).toml.format).toBe(3);
    expect(original().documents?.["playbook.toml"]).toEqual(
      before.documents["playbook.toml"],
    );
  });
  it("an unavailable engine, omitted model and unedited skills survive a rewritten flow", () => {
    const before = fromDetail(original());
    const draft = structuredClone(before);
    const flow = draft.documents["flows/main.toml"] as Flow;
    flow.steps[0].title = "My change";
    const sent = (
      draftFiles(draft, before)["flows/main.toml"] as { toml: Flow }
    ).toml;
    expect(sent.steps[0]).toMatchObject({
      actor: { kind: "agent", engine: "retiring-agent" },
      skills: [{ id: "private-skill", required: true }],
    });
    expect(sent.steps[0].actor).not.toHaveProperty("model");
    expect(sent.format).toBe(1);
  });
  it("removing a flow removes its file; comparing deletion never shows it as retained", () => {
    const before = fromDetail(original());
    const draft = structuredClone(before);
    delete draft.documents["flows/main.toml"];
    delete draft.files["flows/main.toml"];
    expect(draftFiles(draft, before)).not.toHaveProperty("flows/main.toml");
    expect(differences(draft, before)).toEqual([
      {
        path: "flows/main.toml",
        mine: "(absent)",
        current: `Fields:\n${JSON.stringify(before.documents["flows/main.toml"], null, 2)}\n\nSource TOML (before any draft field edits):\n# omitted model remains omitted\n`,
      },
    ]);
  });
  it("shows a comment-only concurrent TOML change before replacement", () => {
    const mine = fromDetail(original());
    const current = structuredClone(mine);
    current.files["flows/main.toml"] =
      "# The operator added an important comment\n";
    expect(differences(mine, current)).toEqual([
      {
        path: "flows/main.toml",
        mine: "# omitted model remains omitted\n",
        current: "# The operator added an important comment\n",
      },
    ]);
  });
  it("shows concurrent TOML comments alongside differing draft and saved fields", () => {
    const mine = fromDetail(original());
    const current = structuredClone(mine);
    (mine.documents["playbook.toml"] as Manifest).identity.name = "My unsaved name";
    (current.documents["playbook.toml"] as Manifest).identity.name = "Changed elsewhere";
    current.files["playbook.toml"] = "# New operator note\nformat = 3\n";
    const [diff] = differences(mine, current);
    expect(diff.path).toBe("playbook.toml");
    expect(diff.mine).toContain('"name": "My unsaved name"');
    expect(diff.mine).toContain("# keep this comment");
    expect(diff.current).toContain('"name": "Changed elsewhere"');
    expect(diff.current).toContain("# New operator note");
  });
  it("distinguishes equal-length binary replacements by digest and additions by presence", () => {
    const mine = fromDetail(original());
    const current = structuredClone(mine);
    current.files["template/raw.bin"] = { base64: "AP/9" };
    expect(differences(mine, current)).toEqual([{
      path: "template/raw.bin",
      mine: "Binary file: 3 bytes\nSHA-256: d590f90f7944340fb253f0c59cb89fd41d4ec255ff246f524f8f7c94f0a233e5",
      current: "Binary file: 3 bytes\nSHA-256: 57d2de42a73ab3a6416d2077fea44e5f3bd499c74d5503126230fb014addb4df",
    }]);
    delete mine.files["template/raw.bin"];
    expect(differences(mine, current)[0]).toMatchObject({
      mine: "(absent)",
      current: expect.stringContaining("Binary file: 3 bytes"),
    });
    expect(differences(current, mine)[0]).toMatchObject({
      current: "(absent)",
      mine: expect.stringContaining("Binary file: 3 bytes"),
    });
  });
  it("new draft submits its documents even before they have original files", () => {
    const draft = newDraft();
    const files = draftFiles(draft, draft);
    expect(files["playbook.toml"]).toEqual({
      toml: draft.documents["playbook.toml"],
    });
    expect(files["flows/main.toml"]).toEqual({
      toml: draft.documents["flows/main.toml"],
    });
  });
  it("ancestry is transitive, ignores presentation order and terminates on a bad draft cycle", () => {
    const step = (id: string, after: string[]) => ({
      id,
      title: id,
      after,
      actor: { kind: "operator" },
    });
    const steps = [
      step("review", ["build"]),
      step("plan", []),
      step("build", ["plan"]),
    ];
    expect(ancestors(steps, "review")).toEqual(new Set(["build", "plan"]));
    steps[1].after = ["review"];
    expect(ancestors(steps, "review")).toEqual(new Set(["build", "plan"]));
  });
});
