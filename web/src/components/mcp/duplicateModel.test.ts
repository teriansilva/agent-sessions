import { describe, expect, test, vi } from "vitest";

import type { McpSetting } from "../../types/mcp";
import {
  credentialGroups,
  defaultRow,
  duplicateBody,
  duplicatedWords,
  hasErrors,
  nextFreeName,
  nextFreeVariable,
  refusalPlace,
  remap,
  staysWords,
  storeNewSecrets,
  validateDuplicate,
  type DupRow,
} from "./duplicateModel";

const cred = (key: string, variable: string): McpSetting => ({ key, kind: "credential", variable, prefix: "", deliver: "env" });
const plain = (key: string, value: string): McpSetting => ({ key, kind: "plain", value, deliver: "env" });

describe("the copy's name", () => {
  test("<name>_2, then the next number free", () => {
    expect(nextFreeName("nextcloud", ["nextcloud"])).toBe("nextcloud_2");
    expect(nextFreeName("nextcloud", ["nextcloud", "nextcloud_2", "nextcloud_3"])).toBe("nextcloud_4");
    expect(nextFreeName("files", ["files", "nextcloud_2"])).toBe("files_2");
  });

  test("a long name is shortened to fit 32 characters, never the number", () => {
    const long = "a".repeat(31) + "b";
    expect(nextFreeName(long, [long])).toBe(`${"a".repeat(30)}_2`);
    expect(nextFreeName(long, [long]).length).toBe(32);
    // a separator left at the cut is dropped, not doubled
    expect(nextFreeName(`${"a".repeat(29)}-xyz`, [])).toBe(`${"a".repeat(29)}_2`);
  });

  test("a new credential's variable: <variable>_2, free among the secrets", () => {
    expect(nextFreeVariable("nc_pass", ["nc_pass"])).toBe("nc_pass_2");
    expect(nextFreeVariable("nc_pass", ["nc_pass", "nc_pass_2"])).toBe("nc_pass_3");
  });
});

describe("one row per distinct credential variable", () => {
  test("two settings naming one variable are one row, with both keys; plain settings are not rows", () => {
    const groups = credentialGroups([
      plain("NC_HOST", "https://cloud.example.org"),
      cred("NC_PASS", "nc_pass"),
      cred("NC_PASS_AGAIN", "nc_pass"),
      cred("NC_TOKEN", "nc_token"),
    ]);
    expect(groups).toEqual([
      { variable: "nc_pass", keys: ["NC_PASS", "NC_PASS_AGAIN"], deliver: "env" },
      { variable: "nc_token", keys: ["NC_TOKEN"], deliver: "env" },
    ]);
  });

  test("an untouched row keeps the original's variable; an empty library starts on New credential", () => {
    const g = { variable: "nc_pass", keys: ["NC_PASS"], deliver: "env" as const };
    expect(defaultRow(g, new Set(["nc_pass"]), false)).toEqual({ mode: "existing", target: "nc_pass", newName: "nc_pass_2", secret: "" });
    expect(defaultRow(g, new Set(), true).mode).toBe("new");
  });
});

const G = [{ variable: "nc_pass", keys: ["NC_PASS"], deliver: "env" as const }];
const row = (p: Partial<DupRow>): Record<string, DupRow> => ({
  nc_pass: { mode: "existing", target: "nc_pass", newName: "nc_pass_2", secret: "", ...p },
});

describe("what the form refuses before sending anything", () => {
  const base = { name: "nextcloud_2", takenNames: new Set(["nextcloud"]), groups: G, secretNames: new Set(["nc_pass", "nc_work"]) };

  test("keeping the original's variable is always allowed, even when the library lacks it", () => {
    expect(hasErrors(validateDuplicate({ ...base, rows: row({}), secretNames: new Set() }))).toBe(false);
  });

  test("the name: the server name rule, and a name already listed", () => {
    expect(validateDuplicate({ ...base, name: "Bad Name", rows: row({}) }).name).toMatch(/Lowercase/);
    expect(validateDuplicate({ ...base, name: "battlelab_x", rows: row({}) }).name).toMatch(/BattleLab's own/);
    expect(validateDuplicate({ ...base, name: "nextcloud", rows: row({}) }).name).toBe(
      "A server named nextcloud exists. Pick another name.",
    );
  });

  test("an existing pick must be a library secret", () => {
    expect(validateDuplicate({ ...base, rows: row({ target: "nc_work" }) }).rows).toEqual({});
    expect(validateDuplicate({ ...base, rows: row({ target: "nope" }) }).rows.nc_pass.variable).toBe(
      "No secret of that name in Variables.",
    );
    expect(validateDuplicate({ ...base, rows: row({ target: "" }) }).rows.nc_pass.variable).toBe("Pick a secret.");
  });

  test("a new credential: a free variable name and a value of at least 8 characters", () => {
    const n = (p: Partial<DupRow>) => validateDuplicate({ ...base, rows: row({ mode: "new", ...p }) }).rows.nc_pass ?? {};
    expect(n({ newName: "nc_home", secret: "long-enough-value" })).toEqual({});
    expect(n({ newName: "nc_work", secret: "long-enough-value" }).variable).toMatch(/exists: pick it under Existing/);
    expect(n({ newName: "Bad", secret: "long-enough-value" }).variable).toMatch(/Lowercase/);
    expect(n({ newName: "nc_home", secret: "short" }).value).toBe("At least 8 characters.");
    expect(n({ newName: "nc_home", secret: " padded-value " }).value).toBe("No spaces at the start or end.");
  });

  test("two new credentials cannot share a name", () => {
    const groups = [...G, { variable: "nc_token", keys: ["NC_TOKEN"], deliver: "env" as const }];
    const rows = {
      nc_pass: { mode: "new" as const, target: "nc_pass", newName: "nc_home", secret: "long-enough-value" },
      nc_token: { mode: "new" as const, target: "nc_token", newName: "nc_home", secret: "long-enough-value" },
    };
    expect(validateDuplicate({ ...base, groups, rows }).rows.nc_token.variable).toBe("Two new credentials share this name.");
  });
});

describe("the request and its retries", () => {
  test("only changed picks are sent; the label only when given; one request id", () => {
    const groups = [...G, { variable: "nc_token", keys: ["NC_TOKEN"], deliver: "env" as const }];
    const rows = {
      ...row({ target: "nc_work" }),
      nc_token: { mode: "existing" as const, target: "nc_token", newName: "", secret: "" },
    };
    expect(remap(groups, rows)).toEqual({ nc_pass: "nc_work" });
    expect(duplicateBody({ name: " nc_2 ", label: "", groups, rows, expected: 42, requestId: "rid" })).toEqual({
      name: "nc_2",
      variables: { nc_pass: "nc_work" },
      expected_updated_at: 42,
      request_id: "rid",
    });
    expect(duplicateBody({ name: "nc_2", label: "Home", groups: G, rows: row({}), expected: 1, requestId: "r" })).toEqual({
      name: "nc_2",
      label: "Home",
      expected_updated_at: 1,
      request_id: "r",
    });
  });

  test("a stored credential becomes an existing pick: a retry stores nothing again", async () => {
    const create = vi.fn().mockResolvedValue({});
    const first = await storeNewSecrets(G, row({ mode: "new", newName: "nc_home", secret: "long-enough-value" }), create, String);
    expect(create).toHaveBeenCalledTimes(1);
    expect(create).toHaveBeenCalledWith("nc_home", "long-enough-value");
    expect(first.created).toEqual(["nc_home"]);
    expect(first.rows.nc_pass).toEqual({ mode: "existing", target: "nc_home", newName: "nc_home", secret: "" });
    expect(remap(G, first.rows)).toEqual({ nc_pass: "nc_home" });
    // the duplicate failed (a name clash); Duplicate again with the rows the first attempt left
    const again = await storeNewSecrets(G, first.rows, create, String);
    expect(create).toHaveBeenCalledTimes(1);
    expect(again.created).toEqual([]);
    expect(remap(G, again.rows)).toEqual({ nc_pass: "nc_home" });
  });

  test("a refused store stops there and names the row; nothing before it is lost", async () => {
    const create = vi.fn().mockRejectedValue(new Error("a variable named nc_home already exists"));
    const out = await storeNewSecrets(G, row({ mode: "new", newName: "nc_home", secret: "long-enough-value" }), create, (e) => (e as Error).message);
    expect(out.failed).toEqual({ variable: "nc_pass", message: "a variable named nc_home already exists" });
    expect(out.created).toEqual([]);
    expect(out.rows.nc_pass.mode).toBe("new");
  });

  test("a refusal lands on the name, on the row it names, or under the form", () => {
    const rows = row({ target: "nc_work" });
    expect(refusalPlace("a server named nc_2 exists", G, rows)).toEqual({ field: "name" });
    expect(refusalPlace("a name is lowercase letters, digits, - and _ (2–32, letter first)", G, rows)).toEqual({ field: "name" });
    expect(refusalPlace("no secret named nc_work in Variables", G, rows)).toEqual({ field: "row", variable: "nc_pass" });
    expect(refusalPlace("nc_work in Variables is not a secret", G, rows)).toEqual({ field: "row", variable: "nc_pass" });
    expect(refusalPlace("at most 64 MCP servers", G, rows)).toEqual({ field: "form" });
  });

  test("the words: what stays in Variables, and what the copy still needs", () => {
    expect(staysWords([])).toBe("");
    expect(staysWords(["nc_home"])).toBe("nc_home stays in Variables.");
    expect(staysWords(["a", "b", "c"])).toBe("a, b and c stay in Variables.");
    expect(duplicatedWords("nextcloud", "nextcloud_2", { nc_pass: "nc_home" })).toMatch(
      /^Duplicated nextcloud as nextcloud_2, reading nc_home\. It is enabled in no project/,
    );
    expect(duplicatedWords("nextcloud", "nextcloud_2", {})).toMatch(/^Duplicated nextcloud as nextcloud_2\. /);
  });
});
