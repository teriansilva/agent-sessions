# Dependency notes for the docs build

`npm audit` on this project reports advisories in the VitePress toolchain. They are recorded here
rather than silently tolerated, because "the audit is noisy" is how a real finding gets missed.

**Nothing in this project ships.** `docs/site/` has its own `package.json` and lockfile, is
referenced by nothing in `web/`, `pyproject.toml`, `install.sh` or `web-ci`, and produces static
HTML. No package here can reach the application bundle or an installed release.

## Fixed

- **esbuild ≤ 0.24.2** — [GHSA-67mh-4wv8-2f99](https://github.com/advisories/GHSA-67mh-4wv8-2f99),
  a dev-server request-forgery. Resolved with an `overrides` entry pinning `esbuild ^0.25.0`
  (0.25.12 installed) under vite 5, verified by a clean build and a green geometry suite.

## Accepted, with containment

- **vite 5** — path traversal in optimized-deps `.map` handling; a `server.fs.deny` bypass on
  Windows alternate paths; launch-editor NTLMv2 disclosure on Windows.

  All three are **dev-server** issues; none affects `vitepress build` output. There is no patched
  vite 5, and VitePress 1.6.4 (the current stable release) pins `vite ^5.4.14` — the only newer
  line is a `2.0.0-alpha`, which is not an appropriate dependency for this repository.

  **Containment:** the `dev` and `preview` scripts bind `127.0.0.1` explicitly, and the Playwright
  config does the same, so the precondition these advisories need — a dev server reachable by
  another host — does not hold for any documented way of running this project. Revisit when
  VitePress 2 reaches a stable release.
