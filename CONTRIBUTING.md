# Contributing to agent-sessions (BattleLab)

Thanks for your interest! This is a small, focused project. PRs and issues are welcome.

## Dev setup

Backend (Python ≥ 3.11):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

Frontend (Node ≥ 20):

```bash
cd web && npm ci
```

## Running the tests

```bash
# Python
ruff check src tests          # lint
ruff format --check src tests # formatting
pytest                        # unit + integration
pytest -m "not e2e_install"   # same, minus the multi-minute install.sh end-to-end tests
pytest -n 8 --dist loadfile   # the full suite in parallel, exactly as pr-validate runs it

# Web
cd web
npm test          # vitest component/unit tests
npm run lint      # eslint
npm run build     # tsc + Vite production build
npm run test:e2e  # Playwright (desktop + emulated mobile); installs chromium on first run

# What CI will select for your change (offline; prints the test files / specs)
python3 .forgejo/scripts/pr_scope.py --files $(git diff --name-only origin/main)
python3 .forgejo/scripts/e2e_scope.py --files $(git diff --name-only origin/main)
```

Run the tests your change touches locally before opening a PR. The `e2e_install` deselect is a
local-iteration convenience only. Land nothing that hasn't passed the tests CI selected for it.

**What CI runs is scoped to the diff (#1244, #2072).** Each check picks the tests the change can
affect; any doubt means the full suite.

**Forcing the full suites on a PR:**
- **Add the `ci:full` label before you push:** every later push of the PR runs both suites in full.
- **On a PR that has already run scoped:** dispatch `pr-validate` and `web-ci` on the PR's branch
  (Actions → the workflow → Run workflow, or `POST …/actions/workflows/<file>/dispatches` with
  `{"ref": "<branch>"}`). A dispatched run is always full, and its checks report on the same
  commit, so the PR's combined status includes them.
- **Adding the label alone doesn't re-run anything:** label events don't trigger CI, because
  agents change status labels constantly.

- **pytest** (`.forgejo/scripts/pr_scope.py`):
  - **Full suite** for packaging, deploy, CI and test-harness paths (`pyproject.toml`, `uv.lock`,
    the installer scripts, `deploy/`, `scripts/`, `.forgejo/`, `conftest.py` and other test
    helpers), or more than 25 changed backend files.
  - **A changed backend module** runs the test files whose module-level imports reach it, plus
    one hop of function-level imports, plus the tests that name it. `main.py` runs every test
    that imports it.
  - **The whole-tree tests always run** on a backend change: route inventories, ratchets, and
    anything that walks `src/`.
  - **A changed test file** runs itself.
  - **Web, docs and landing changes** run the tests that mention their paths.
  - **The installer contract and deploy-shape smoke** run on any backend change.
- **Playwright** (`.forgejo/scripts/e2e_scope.py`):
  - **Full suite** when the harness, the build or the app shell changes (`package*.json`, the Vite
    and Playwright configs, `src/app/`, `src/theme/`, `index.css`, `tokens.css`, `main.tsx`, the
    shared e2e fixtures).
  - **Otherwise** only the specs that address what the change renders: its testids and those of
    the components above it (up to the app shell), the page it is an entry of, or, failing both,
    the route it renders under.
  - **Shards with no specs** pass in seconds.
- **Every push to main runs both full suites.** That is the backstop for anything a scoped PR run
  didn't select.
- **A few failures are rechecked before red** (`.forgejo/scripts/recheck.py`). When at most 5
  tests fail, they run again alone, with one worker and no retries. Both runs are judged from a
  per-test report (pytest's `recheck_report` plugin, Playwright's JSON reporter), never from the
  exit code:
  - **if each one ran and passed** (a skip doesn't count), the check is green and each is named in
    a `Passed only on a serial recheck` warning. A test that keeps appearing there is flaky; fix it.
  - **if not**, the check is red.
  - **Never rechecked:** a crashed xdist or Playwright worker, a collection or internal error, a
    run-level Playwright error, or an abort.
- **A red check stops its siblings:** the long steps (pytest, the e2e shards, app-mode e2e) run
  under `.forgejo/scripts/sibling_watch.py`. When another check for the same commit has failed, it
  stops them within ~30 s and says so (`aborted: sibling "…" failed`), so a red commit stops
  holding runners. An aborted check didn't fail on its own: fix the sibling it names.

Test-suite policies worth knowing:

- **PBKDF2 runs at a test-scoped work factor** (#699). An autouse session fixture in
  `tests/conftest.py` drops `auth._PBKDF2_ITERS` from 600k to 1,000 for the test session —
  `verify_password` reads the iteration count from the hash string itself, so the full
  parse/derive/compare path is still exercised and production code is untouched. The fixture
  *asserts the production constant is ≥ 600k before patching*: weakening the real work factor
  fails the whole suite.
- **Markers are strict** (`--strict-markers`): a typo'd `@pytest.mark.…` is a collection error,
  not a silent no-op. Register new markers in `pyproject.toml`.
- **`deploy_shape` marks the tests `pr-validate`'s wheel pass re-runs** (#704). The app resolves
  its Jinja templates and `/static` assets *relative to the installed module* (`main.py`'s
  `_HERE = Path(__file__).parent`), so those reads can only break in a non-editable install —
  which is why that pass installs with `pip install .` and re-runs `-m deploy_shape` from
  outside the repo. **Mark a new test `deploy_shape` when it covers a resource read off the
  installed package** (a new template, a new `/static` asset). Presence of every packaged
  resource is checked separately in that job and derives from the source tree, so it can't
  drift; the marker is what proves the app can still *resolve* them. The rest of the suite runs
  once, in the editable pass — re-running it against the wheel catches nothing (a wheel built
  with no templates at all still passes 283 of the non-rendering tests).

`install.sh` / `uninstall.sh` are smoke-tested on **every PR that touches the app, packaging, the
installer or CI** (`installer-smoke.yml`): a pristine
container (no usable Python/Node — the vendored-toolchain path) installs from your checkout, the
installed app must serve `/healthz` → `{"ok":true}`, and the uninstall must remove the install
root + prefs + cache while a seeded `~/.claude` survives. It takes ~5–10 min (vendors both
toolchains + a real Vite build) and runs in parallel with the other checks. Reproduce locally
with `scripts/smoke-install` (needs Docker). Toolchain tarballs (only the sha256-pinned ones) and
the pip/npm caches persist in `~/.cache/agent-sessions-smoke` between runs; set
`SMOKE_CACHE_DIR=off` for a fully cold run (#1102).

Tests never touch your real `~/.claude`: they use a `mktemp -d` `AGENT_SESSIONS_HOME`. Installer
tests must set `AGENT_SESSIONS_NO_SERVICE=1` so they don't write your real systemd unit. Heavy
UI-build tests (real `npm ci` + Vite build) are gated behind `AGENT_SESSIONS_TEST_UI_BUILD=1`.

## The shell-free rule (please read)

The engine launchers are **shell-free by design** — a load-bearing security property, not a style
preference. Every subprocess call is `subprocess.run([...], check=True)` with a literal argv list;
no `shell=True`, no `os.system`, no inline shell interpreter. UUIDs, cwds, and session/tab names
are validated **before** they reach subprocess. CI (`.forgejo/workflows/pr-validate.yml`) greps
`src/` for shell patterns and fails the build if any appear, and the unit tests assert the same.

If you find yourself wanting to add a shell layer "just for this one case" — don't. Refactor the
case to fit the argv model. See [`CLAUDE.md`](CLAUDE.md) for the other project invariants
(engine-qualified identity, single-writer session lock, the PWA service-worker denylist, the auth
model).

## PR conventions

- One logical change per PR; keep the diff scoped.
- Add a regression test for every bug fix and every new behavior.
- Conventional, descriptive commit messages. No AI-attribution trailers.
- Update the docs (`README.md` / `CLAUDE.md` / `docs/`) when you change behavior; a user-visible
  change also updates the manual (see [Docs are part of done](#docs-are-part-of-done)).
- For UI changes, include before/after screenshots — see [`docs/visual-review.md`](docs/visual-review.md).

## Docs are part of done

The manual in `docs/site` is what users read at docs.battlelabos.com, and what HQ searches when
someone asks it how BattleLab works. A feature the manual doesn't describe is a feature HQ can't
explain. So:

- **A user-visible change updates its manual page in the same PR.** That covers UI, behaviour, a
  setting, or anything a user can reach. Say which in the PR's test plan:
  `- [x] Manual: guide/<page> updated`, or `- [x] Manual: none — <why it isn't user-visible>`.
- **A new section, sub-menu entry or HQ capability group gets a row in
  [`docs/docs-coverage.md`](docs/docs-coverage.md)**, naming the page that documents it, or
  `gap #<issue>` for the open issue that will write it. `tests/test_docs_coverage.py` fails on an
  unmapped entry, a missing page, a page that isn't in the sidebar, or a stale row.
- The test proves a page *exists*; review checks it is *right*. Write against the running app, and
  prefer generated blocks (`scripts/gen-engine-docs`) for facts the code already knows.

## UI changes — NIGHTJAR

The visual language is NIGHTJAR // OPS, [`docs/design.md`](docs/design.md) v2. New or changed UI:

- **Uses the NIGHTJAR primitives** from `web/src/components/hud/` and `nightjar.css` (panels,
  plates, callsign headers, LEDs, pips and gauges, chips and tags) and the tokens in
  `web/src/tokens.css`. No literal colour, radius, shadow, `z-index` or off-scale font size; never
  copy v1 chrome. The unlinked `/__nightjar` route (behind the normal login) renders every
  primitive in its states, in whichever mode, motion mode and viewport you open it.
- **Adds its ledger rows.** Every stylesheet under `web/src`, every component that renders a
  dialog, menu, listbox or portal, and every page has a row in
  [`docs/nightjar-ledger.md`](docs/nightjar-ledger.md); a new one without a row fails
  `nightjarLedger.test.ts`. A `done` row names its sweep captures and its motion.
- **Runs its sweep rows.** Add the states it introduces as capture ids in
  `web/visual/nightjar/manifest.mjs` and photograph them with `web/visual/nightjar/sweep.mjs
  --only <ids>` (how to run it is in `docs/visual-review.md`); those shots are the PR's evidence.
- **Honours the motion modes** (Full / Reduced / Off): animate only through `motion.css` and
  `theme/motion.ts`, never read `prefers-reduced-motion` directly, and never move a work surface
  (the terminal, the editor, a focused field) — see
  [`docs/invariants/nightjar-motion.md`](docs/invariants/nightjar-motion.md).

## Licensing of contributions

BattleLab is licensed **AGPL-3.0-or-later** (see [`LICENSE`](LICENSE)).

Contributions are **inbound=outbound**: by opening a pull request you agree that your
contribution is licensed under the same AGPL-3.0-or-later terms as the project, and you confirm
you have the right to license it that way (it's your own work, or you have permission from
whoever owns it). No copyright assignment is requested and no CLA needs signing — you keep the
copyright in what you wrote.

Please don't paste in code under an incompatible license. Permissive licenses (MIT, ISC, BSD,
Apache-2.0) are fine to vendor with their notices intact; GPL-2.0-**only** or proprietary code
is not, because it can't be redistributed under AGPL-3.0.

The **BattleLab** name and logo are not covered by the code license — see
[`TRADEMARK.md`](TRADEMARK.md).

## Code of conduct

Be respectful and constructive. We follow the spirit of the
[Contributor Covenant](https://www.contributor-covenant.org/). Report unacceptable behavior by
opening a confidential issue or contacting the maintainers.
