# Security

This document summarizes BattleLab's (`agent-sessions`) security posture: the trust model, the
guarantees enforced by code and CI, and the findings + current status of the two security audits
run against this codebase (2026-05-26 and 2026-07-10, the latter tracked in issue #612). Status
is current as of 2026-07-16.

## Trust model — read this first

BattleLab is a **single-admin** tool that launches AI coding agents with permission bypass
**by design** (`--dangerously-skip-permissions` and equivalents). A logged-in user can run
arbitrary commands as the service user — the trust boundary is the same as SSH access to the
host. It is **not multi-tenant**: one admin account, no per-user isolation, never share a login.

The app binds `127.0.0.1` and does not terminate TLS or rate-limit by itself. It **must** sit
behind a reverse proxy providing TLS, and `/login` should be rate-limited at the proxy. See the
[Security / trust model](README.md#security--trust-model) section of the README and
[`deploy/nginx.example.conf`](deploy/nginx.example.conf).

## Enforced security properties

These are pinned by tests and/or CI — regressions fail the build:

- **Shell-free engine launchers.** No shell-enabled subprocess, no inline shell interpreter, no
  `os.system` anywhere in the launch paths — every engine launch is an explicit argv list
  executed without a shell (`asyncio.create_subprocess_exec` on the terminal paths;
  `subprocess.run([...])` for bounded helper commands), with UUID/cwd/name validation *before*
  any subprocess. Guarded by a grep gate in `pr-validate.yml` and unit tests over the call
  graph.
- **Auth stack.** Signed `itsdangerous` session cookie (`HttpOnly`, `Secure`, `SameSite=Lax`);
  CSRF token bound to the cookie and required on every state-changing request; `Origin`/`Referer`
  must equal `AGENT_SESSIONS_ORIGIN`; `/api/auth-check` (204/401) for an optional nginx
  `auth_request` layer. Optional TOTP 2FA issues only a short-lived pre-auth cookie until the
  second factor is verified. New passwords must be ≥ 12 characters; a fresh install forces a
  password change before the data APIs open (403 until completed).
- **Browser containment headers.** CSP, `frame-ancestors`, HSTS, `X-Content-Type-Options`,
  `Referrer-Policy`, and `Permissions-Policy` are set by the app itself
  (`src/agent_sessions/security_headers.py`, PR #647) — not by the reverse proxy.
- **Single-writer terminal sessions.** One `dtach` master per session key, arbitrated by an
  advisory `flock` that survives restarts — no double-resume/double-write even across app
  instances.
- **opencode is read-only.** The opencode provider only reads `opencode.db`; archive, rename and
  sticky state live in the app's own sidecar, never in the engine's data.
- **Bounded scrollback memory.** Per-session scrollback rings are LRU-capped (max 64 resident)
  with eager reclaim when a session's `dtach` master exits (PR #121).
- **Supply-chain pin for the vendored Python.** The installer verifies the vendored Python
  download against pinned SHA-256 sums and fails closed when no sha256 tool exists.
- **Public-mirror scrubbing.** The publish pipeline runs an export-ignore filter, scrub pass and
  snapshot gate (#324) so internal-only content cannot reach the public mirror.

## Audit history

### 2026-05-26 — initial audit

Verdict: the app was found fundamentally sound. Findings and status:

| Severity | Finding | Status |
| --- | --- | --- |
| MEDIUM | Per-session scrollback buffers were never evicted — unbounded process-lifetime memory growth. | **Fixed** — LRU cap + eager reclaim on dead masters (PR #121). |
| LOW | New-password minimum was 8 characters. | **Fixed** — raised to 12 (PR #121). |
| — | Go-public flip risks: full-history secret scan, self-hosted CI runner exposure to untrusted public PRs, private default URLs in `install.sh`/self-update, vendored Node verification. | **Tracked** in the operator-gated flip checklist (#117, on hold). |

### 2026-07-10 — hardening audit (issue #612)

Audited from clean `origin/main` at `645aa0e`. Verdict: the core trust model is in good shape —
single-admin auth has cookie, CSRF and Origin/Referer checks; the launchers preserve the
shell-free argv guarantee; Home Free's app-side tunnel handshake is E2E and replay-resistant.
Remaining risk is hardening, prioritized P0 (immediate) → P2 (defense-in-depth):

| Prio | Finding | Status |
| --- | --- | --- |
| P0 | Production `pip-audit` advisories in `starlette` 0.41.3, `jinja2` 3.1.4, `python-multipart` 0.0.20. | **Fixed** — jinja2 3.1.6, python-multipart 0.0.31, starlette 1.3.1 / fastapi 0.139.0; production `pip-audit` clean (PR #643). |
| P0/P1 | Install/update supply chain: the vendored **Node** fallback downloads without checksum verification (unlike Python); the `stable` update channel lacks a committed trust root (`scripts/release-manifest.json`); `main` channel should be an explicit dev-only opt-in. | **Open** — #612 Phase 1. |
| P1 | No browser containment headers (CSP, frame-ancestors, nosniff, Referrer-Policy, Permissions-Policy, HSTS). | **Fixed** — `security_headers.py` (PR #647); `microphone=(self)` follow-up for push-to-talk (#659). |
| P1 | No Home Free credential lifecycle controls (rotate / disable / show-credentials with lockout-safe ordering). | **Open** — #612 Phase 3 (`install.sh --homefree-rotate-key` / `--homefree-disable` / `--homefree-show-credentials`). |
| P2 | Local file & secret-at-rest defaults: uploads created with default umask (should be `0700` dir / `0600` files from first write); TOTP secrets stored unwrapped (assess wrapping with `AGENT_SESSIONS_SECRET_KEY`). | **Open** — #612 Phase 4. |
| P2 | Dev-only `undici` advisories in the full `pnpm audit` (production `pnpm audit --prod` is clean). | **Open** — dev tooling only, no production exposure. |

Audit tooling notes from that run: `ruff` clean; `pnpm audit --prod` clean; `bandit -r src`
produced only the expected low/false-positive subprocess findings and one SQL-string false
positive over constant opencode schema columns; no shell-invocation regression in the launch
paths.

### Related tracking

- #612 — open hardening phases from the 2026-07-10 audit (supply chain, Home Free lifecycle,
  file modes, closure).
- #117 — operator-gated go-public flip checklist (secret scan, CI runner gating, URL swaps).
- `superstatus.io/battlelab-cloud#36` — Home Free relay/edge hardening (out of scope for this
  repo).

## Re-running the audit checks

```bash
# Production dependency audit (Python)
uv export --frozen --no-dev --no-hashes --no-emit-project \
  --format requirements-txt > /tmp/req.txt
uv run --with pip-audit pip-audit -r /tmp/req.txt

# Web dependency audit (production, then full incl. dev tooling)
cd web && pnpm audit --prod && pnpm audit

# Static analysis
uv run --extra dev ruff check src tests
bandit -r src

# The shell-free guarantee (also enforced by pr-validate.yml + tests/test_engines.py)
uv run pytest tests/test_engines.py tests/test_auth.py tests/test_password.py
```

## Supported versions

Only the latest **stable release** (the installer default, `AGENT_SESSIONS_CHANNEL=stable`) is
supported. The `main` channel is a development opt-in and carries no production guarantees.

## Reporting a vulnerability

Do **not** open a public issue for an exploitable vulnerability. Report it privately to the
maintainer — via GitHub's private vulnerability reporting on the public mirror
(`github.com/teriansilva/agent-sessions`) — and include reproduction steps and the version
(`/api/version`). Given the single-admin trust model, reports about an authenticated admin
running arbitrary commands are by-design behavior, not vulnerabilities — see the trust model
above.
