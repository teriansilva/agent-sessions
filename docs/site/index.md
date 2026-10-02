---
layout: home
hero:
  name: BattleLab
  text: Every feature, and the whole security model
  tagline: The self-hosted command deck for AI coding agents. This site is the repository's own documentation, rendered — built from the same filtered public snapshot that feeds the GitHub mirror, so it contains only content eligible for the public mirror.
  actions:
    - theme: brand
      text: Install
      link: /start/install
    - theme: alt
      text: Read the security model
      link: /security/
features:
  - title: Engines
    details: Claude Code, Codex, opencode, Gemini, Antigravity and Kimi Code — plus a plain shell, with no agent at all.
    link: /guide/engines
  - title: Sessions
    details: Persistent dtach terminals that survive a closed tab, a redeploy and a reboot, arbitrated by a single-writer lock.
    link: /guide/sessions
  - title: Mission control
    details: Give a mission an instruction and a project; it works out what done means, follows through, and asks when it is unsure.
    link: /guide/missions
  - title: Files & git
    details: A file browser you can upload into and edit in, and a git view that can stage, commit and push — docked in the session that owns them.
    link: /guide/files-and-git
  - title: AI review
    details: Point any OpenAI-compatible endpoint — including a local model — at your running sessions.
    link: /guide/ai-review
  - title: Home Free
    details: Reach your box from any browser through a blind relay that cannot decrypt what it forwards.
    link: /guide/home-free
---

## Where to start

If you have never run BattleLab, read **[Install](/start/install)** and then
**[Trust model](/security/)** — in that order. The second one matters more than it looks: BattleLab
launches AI coding agents with permission bypass by design, so its trust boundary is SSH access to
the host, not the login form.

If you are already running it, the **[Guide](/guide/engines)** has one page per capability, and the
**[Reference](/reference/)** has every CLI subcommand, route and environment variable in one place.

## How this site stays in sync

This site is built from the **filtered public snapshot** — the same tree
`scripts/check-public-snapshot` produces and the GitHub mirror is cut from. Pages like
[Security](/security/) and [Reference](/reference/) are not retyped here: they are the
repository's own `SECURITY.md` and `docs/reference.md`, included verbatim, so there is no second
copy to drift.

That is a claim about *eligibility*, not about matching the mirror's HEAD. The mirror is pushed by
an operator, so a build can be **ahead** of the last publish and describe behaviour that is not yet
mirrored or released. The footer on every page names the exact commit it was built from and how far
ahead of the last publish that is — or says `unknown` rather than guessing, when the provenance
cannot be established.

::: info Rebuilt on every push to `main`
The build is reproducible — `scripts/build-docs-site` runs the whole pipeline, gates included, from
any checkout — and the deployment runs that same script on every push to `main`. The footer stamp
is still the thing to trust: it names the commit you are actually reading.
:::

The `/guide/` pages are the exception worth knowing about: they are new prose, so they can go
*semantically* stale while the build stays green. Each one carries a **verified against** line
naming the commit and the constants it was checked against.
