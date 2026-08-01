import styles from "./EnableLoginDetails.module.css";

// The verified enable-login recipe (checked against install.sh / cli.py / accounts.py). This is
// the ONE source of truth shared by the Settings → Security login-off card and the onboarding
// wizard's login-off step, so the copy can't drift. Single-quoted lines: nothing interpolates,
// and no plaintext password ever touches argv/history — `reset-password --prompt` reads it with
// no echo.
const ENABLE_LOGIN_COMMANDS = [
  'home="${AGENT_SESSIONS_HOME:-$HOME/.local/share/agent-sessions}"',
  "# 1) Set an admin password (prompted, no echo):",
  '"$home/current/venv/bin/agent-sessions" reset-password --prompt --env "$home/env"',
  '# 2) In "$home/env": set  AGENT_SESSIONS_AUTH_MODE=single-user  (was =none)',
  "#    and ensure  AGENT_SESSIONS_USERNAME=admin  is present.",
  "# 3) Restart to pick it up:",
  "systemctl --user restart agent-sessions",
].join("\n");

/**
 * A disclosure with the steps to turn a Home Free (login-off) box into a password login. Reused
 * verbatim by Settings and onboarding so the two surfaces stay in sync (#682).
 */
export function EnableLoginDetails() {
  return (
    <details className={styles.details}>
      <summary className={styles.summary}>Prefer a password login? Here's how to turn it on.</summary>
      <p className={styles.blurb}>
        Home Free runs with login off by design — the access key is the gate. To require an in-app
        password (plus optional two-factor) instead, run these on the box, then reconnect:
      </p>
      <pre className={styles.code}>
        <code>{ENABLE_LOGIN_COMMANDS}</code>
      </pre>
      <p className={styles.blurb}>
        The password is active immediately after the restart — there's no forced first-login
        change. Add two-factor later from this Security tab once login is on.
      </p>
    </details>
  );
}
