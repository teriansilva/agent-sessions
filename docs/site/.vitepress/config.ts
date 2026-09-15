import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitepress";

const here = dirname(fileURLToPath(import.meta.url));

// The build stamp is produced by scripts/build-docs-site BEFORE the snapshot is archived, because
// the snapshot is a `git archive` extraction with no `.git` — `git describe` cannot run in it
// (#828). Reading it here keeps the provenance in one place: the footer, on every page.
type Stamp = { commit: string; describe: string; ahead: string; built: string };
function stamp(): Stamp {
  try {
    return JSON.parse(readFileSync(resolve(here, "stamp.json"), "utf8")) as Stamp;
  } catch {
    // A local `vitepress dev` has no stamp; say so rather than inventing one.
    return {
      commit: "local",
      describe: "local",
      ahead: "ahead of last publish: unknown",
      built: "local build",
    };
  }
}
const s = stamp();

export default defineConfig({
  title: "BattleLab Docs",
  description:
    "Documentation for BattleLab (agent-sessions) — the self-hosted command deck for AI coding agents. Every feature, and the whole security model.",
  lang: "en-US",
  cleanUrls: true,
  // SECURITY-NOTES.md is a note to maintainers about this project's dependency advisories, and
  // it lives here so it sits beside the package.json it describes. VitePress turns every .md
  // under srcDir into a route, which published it at /SECURITY-NOTES — a page in no nav, in the
  // sitemap, and reachable by search. Excluded rather than moved: next to the manifest is where
  // a reader of that manifest will look for it.
  srcExclude: ["SECURITY-NOTES.md"],
  appearance: "dark",
  // A dead @include or a link to a page that does not exist fails the build rather than shipping
  // a 404 into the sidebar — the structural half of "docs cannot drift" (#828).
  ignoreDeadLinks: false,
  head: [
    ["meta", { name: "theme-color", content: "#0d0e10" }],
    ["meta", { name: "robots", content: "index, follow" }],
  ],
  sitemap: { hostname: "https://docs.battlelabos.com" },

  markdown: {
    config(md) {
      // Transcluded canonical files (SECURITY.md, docs/reference.md, CONTRIBUTING.md …) carry
      // links that are relative to THEIR directory in the repo, not to the docs page including
      // them — so `README.md#trust-model` inside SECURITY.md means the repo root README, and
      // rendering it as-is produces a link to a docs route that does not exist. Rather than
      // weaken `ignoreDeadLinks` (which would also stop catching genuinely broken docs links),
      // resolve each one against the page's `linkBase` and rewrite it to the public mirror.
      // A page that transcludes nothing declares no linkBase and is left completely alone.
      const BLOB = "https://github.com/teriansilva/agent-sessions/blob/main";

      // …except that not every repo path EXISTS on the mirror. `.gitattributes` strips the
      // export-ignored ones, so a rewritten link to CLAUDE.md or docs/visual-review.md would be
      // a confidently-wrong 404 — worse than no link, because it looks authoritative. Those
      // targets are rendered as plain text instead. The list is derived from `.gitattributes`
      // rather than hardcoded, so it cannot drift from the filter that actually removes them.
      const excluded: string[] = readFileSync(resolve(here, "../../../.gitattributes"), "utf8")
        .split("\n")
        .filter((l) => /\sexport-ignore(\s|$)/.test(l))
        .map((l) => "/" + l.trim().split(/\s+/)[0].replace(/^\/+|\/+$/g, ""));
      const isExcluded = (path: string) =>
        excluded.some((e) => path === e || path.startsWith(e + "/"));

      const defaultRender =
        md.renderer.rules.link_open ??
        ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));
      const defaultClose =
        md.renderer.rules.link_close ??
        ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));

      // link_open/link_close are rendered as separate tokens, so "this one became a span" has
      // to be remembered between them. Links can nest inside other inline containers, hence a
      // stack rather than a flag.
      const asText: boolean[] = [];

      md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
        const base: string | undefined = env?.frontmatter?.linkBase;
        const hrefIdx = tokens[idx].attrIndex("href");
        if (base && hrefIdx >= 0) {
          const href = tokens[idx].attrs![hrefIdx][1];
          // Absolute URLs, anchors and mail links are already correct; everything else is a
          // path inside the repository.
          if (!/^(https?:|mailto:|#|\/)/.test(href)) {
            const [path, hash] = href.split("#");
            const resolved = new URL(path, `file://${base}`).pathname;
            if (isExcluded(resolved)) {
              asText.push(true);
              return `<span class="internal-ref" title="${resolved} is internal to the private repository and is not published to the public mirror">`;
            }
            tokens[idx].attrs![hrefIdx][1] =
              `${BLOB}${resolved}${hash ? `#${hash}` : ""}`;
          }
        }
        asText.push(false);
        return defaultRender(tokens, idx, options, env, self);
      };

      md.renderer.rules.link_close = (tokens, idx, options, env, self) =>
        asText.pop() ? "</span>" : defaultClose(tokens, idx, options, env, self);
    },
  },

  themeConfig: {
    siteTitle: "BATTLELAB",
    outline: { level: [2, 3], label: "On this page" },
    search: { provider: "local" },

    nav: [
      { text: "Start", link: "/start/install" },
      { text: "Guide", link: "/guide/engines" },
      { text: "Security", link: "/security/" },
      { text: "Reference", link: "/reference/" },
      { text: "GitHub", link: "https://github.com/teriansilva/agent-sessions" },
    ],

    sidebar: [
      {
        text: "Start",
        collapsed: false,
        items: [
          { text: "Install", link: "/start/install" },
          { text: "First login", link: "/start/first-login" },
          { text: "Update & rollback", link: "/start/update" },
          { text: "Uninstall", link: "/start/uninstall" },
        ],
      },
      {
        text: "Guide",
        collapsed: false,
        items: [
          { text: "Engines", link: "/guide/engines" },
          { text: "Sessions", link: "/guide/sessions" },
          { text: "Terminal", link: "/guide/terminal" },
          { text: "Projects", link: "/guide/projects" },
          { text: "Files & git", link: "/guide/files-and-git" },
          { text: "Mission control", link: "/guide/missions" },
          { text: "Templates", link: "/guide/templates" },
          { text: "AI review", link: "/guide/ai-review" },
          { text: "Handoff", link: "/guide/handoff" },
          { text: "Dictation", link: "/guide/dictation" },
          { text: "Notifications", link: "/guide/notifications" },
          { text: "Home Free", link: "/guide/home-free" },
          { text: "Settings", link: "/guide/settings" },
        ],
      },
      {
        text: "Security",
        collapsed: false,
        items: [
          { text: "Trust model & guarantees", link: "/security/" },
          { text: "Relay crypto", link: "/security/relay-crypto" },
        ],
      },
      {
        text: "Reference",
        collapsed: false,
        items: [{ text: "CLI, API & environment", link: "/reference/" }],
      },
      {
        text: "Internals",
        collapsed: false,
        items: [
          { text: "Session handling", link: "/internals/session-handling" },
          { text: "Install footprint", link: "/internals/install-footprint" },
        ],
      },
      {
        text: "Contributing",
        collapsed: false,
        items: [{ text: "Contributing", link: "/contributing/" }],
      },
    ],

    socialLinks: [
      { icon: "github", link: "https://github.com/teriansilva/agent-sessions" },
    ],

    editLink: {
      pattern:
        "https://github.com/teriansilva/agent-sessions/edit/main/docs/site/:path",
      text: "Edit this page",
    },

    footer: {
      message: `Built from <code>${s.describe}</code> · <code>${s.commit}</code> · ${s.ahead}`,
      copyright: `AGPL-3.0-or-later · ${s.built}`,
    },
  },
});
