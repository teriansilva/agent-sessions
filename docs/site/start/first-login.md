# First login

## A fresh install forces a password change

The installer writes a random admin password and sets `AGENT_SESSIONS_FORCE_PASSWORD_CHANGE=1`.
Until that change completes, **every `/api/*` route returns 403 "password change required"** — the
app is up, but it holds no data open to a default credential. The SPA routes you to
`/change-password`, whose form takes `current`, `new` and `confirm`.

New passwords must be at least **12 characters**.

::: tip Lost the printed password
`agent-sessions reset-password` on the host sets a new hash directly. With `--prompt` it reads
interactively without echoing; with `--stdin` it reads one line. With neither it generates a
random password and prints it once. Never pass a password as a command-line argument — argv is
readable by other local users.
:::

## Two-factor authentication

TOTP 2FA is optional and off until you enroll. `POST /api/2fa/enroll` returns the secret, an
`otpauth://` URI and a set of recovery codes — **shown once**. `POST /api/2fa/confirm` with a
current code turns it on.

Once enabled, `POST /login` no longer mints a session on its own: a correct password issues a
short-lived **pre-auth** cookie, and `POST /login/totp` exchanges a TOTP or recovery code for the
real session cookie. Disabling 2FA or regenerating recovery codes each require a fresh proof — a
current `{code}` or your `{password}` — so a stolen live session cannot quietly turn it off.

If you are locked out, `agent-sessions clear-2fa` on the host removes the secrets file and disables
2FA. It is deliberately host-only: it is the escape hatch, and reaching it means you already have
the access 2FA was protecting.

2FA does not apply under `AGENT_SESSIONS_AUTH_MODE=none`, which has no login step at all.

## What a session cookie is

A signed `itsdangerous` token, `HttpOnly`, `Secure`, `SameSite=Lax`. Every state-changing request
additionally needs the CSRF token bound to that cookie (`X-CSRF-Token`) **and** an `Origin` or
`Referer` equal to `AGENT_SESSIONS_ORIGIN`. Those two are enforced even under `AUTH_MODE=none`.

::: info Verified against
Commit `218cf3a` — `src/agent_sessions/twofactor.py`; `docs/reference.md § Auth`; `README.md § Security / trust model`.
:::
