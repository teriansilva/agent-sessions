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

# Web
cd web
npm test          # vitest component/unit tests
npm run lint      # eslint
npm run build     # tsc + Vite production build
npm run test:e2e  # Playwright (desktop + emulated mobile); installs chromium on first run
```

Run all of the above locally before opening a PR — CI runs the same checks. The `e2e_install`
deselect is a local-iteration convenience only; CI always runs the full suite, so land nothing
that hasn't passed a plain `pytest` at least once.

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

`install.sh` / `uninstall.sh` are smoke-tested on **every PR** (`installer-smoke.yml`): a pristine
container (no usable Python/Node — the vendored-toolchain path) installs from your checkout, the
installed app must serve `/healthz` → `{"ok":true}`, and the uninstall must remove the install
root + prefs + cache while a seeded `~/.claude` survives. It takes ~5–10 min (vendors both
toolchains + a real Vite build) and runs in parallel with the other checks. Reproduce locally
with `scripts/smoke-install` (needs Docker).

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
- Update the docs (`README.md` / `CLAUDE.md` / `docs/`) when you change behavior.
- For UI changes, include before/after screenshots — see [`docs/visual-review.md`](docs/visual-review.md).

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
