/** Every activation the UI can send is bound (#1741, Hermes 7284/7290/7299): it carries
 *  `expected_active` and `expected_revision`, so the server refuses it when the roster moved since
 *  the operator confirmed. A static sweep of every call site — a new unbound path fails here. */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, test } from "vitest";

const SRC = resolve(process.cwd(), "src");
const files = (dir: string): string[] =>
  readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : /\.tsx?$/.test(n) && !/\.test\.tsx?$/.test(n) ? [p] : [];
  });

/** The argument text of every `name(` call in `source`, balanced on parentheses. */
function calls(source: string, name: string): string[] {
  const out: string[] = [];
  let i = source.indexOf(`${name}(`);
  while (i !== -1) {
    let depth = 0;
    let j = i + name.length;
    for (; j < source.length; j++) {
      if (source[j] === "(") depth++;
      else if (source[j] === ")" && --depth === 0) break;
    }
    out.push(source.slice(i + name.length + 1, j));
    i = source.indexOf(`${name}(`, j);
  }
  return out;
}

describe("activation binding (#1741)", () => {
  test("every activate call site in web/src sends expected_active and expected_revision", () => {
    const sites: string[] = [];
    for (const f of files(SRC)) {
      const source = readFileSync(f, "utf8");
      const rel = relative(SRC, f);
      for (const args of calls(source, "api.pluginActivate")) {
        sites.push(`${rel}: pluginActivate`);
        // Either the literal carries both preconditions, or it is the flow's bound `activation`.
        const bound = /expected_active/.test(args) && /expected_revision/.test(args);
        expect(bound || args.trim() === "activation", `${rel}: api.pluginActivate(${args})`).toBe(true);
      }
      for (const fn of ["start", "begin"]) {
        for (const args of calls(source, fn)) {
          if (!/"activate"/.test(args)) continue;
          sites.push(`${rel}: ${fn}("activate")`);
          // A bound state follows the kind: start("activate", binding) / begin(c, "activate", x).
          expect(/"activate"\s*,\s*\S/.test(args), `${rel}: ${fn}(${args})`).toBe(true);
        }
      }
    }
    // The flow's own `activation` is built only with both preconditions, and an unbound activate
    // throws before anything is sent.
    const flow = readFileSync(join(SRC, "components/plugins/AgentSetupFlow.tsx"), "utf8");
    expect(flow).toMatch(/const activation = bound \? \{ \.\.\.body, expected_active: bound\.active, expected_revision: bound\.revision \}/);
    expect(flow).toMatch(/if \(kind === "activate" && !bound\) throw/);
    // The sweep found the sites it is about: the flow (chain, Enable, Enable anyway) and Roll back.
    expect(sites.filter((s) => s.includes("AgentSetupFlow")).length).toBeGreaterThanOrEqual(4);
    expect(sites.some((s) => s.includes("RosterList"))).toBe(true);
  });
});
