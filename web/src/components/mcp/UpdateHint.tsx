/** "Newer version available" (#2431): the chip beside a version, and the Updates block on a server's
 *  Overview and Settings → Source — the hint in words, the update action that fits how the server
 *  was added (Update from source for a repository link, Update… for a package added by name), and
 *  Check for updates. The hint is the registry's; nothing here installs anything. */
import { Link } from "react-router-dom";
import { CircleArrowUp, Download, GitBranch, RefreshCw } from "lucide-react";

import { mcpServerUpdatePath } from "../../lib/routes";
import type { McpServerSummary, McpUpdateHint } from "../../types/mcp";
import { availableChip, hintWords, registryHost } from "./updateModel";
import styles from "./mcp.module.css";

/** `v3.4.0 available` in the neutral info tone (not a status colour): it is news, not a state. */
export function AvailableChip({ update, testId = "mcp-update-hint" }: { update: McpUpdateHint | null | undefined; testId?: string }) {
  const words = availableChip(update);
  if (!words) return null;
  return (
    <span className={styles.updChip} data-testid={testId}>
      <CircleArrowUp size={12} aria-hidden="true" />
      {words}
    </span>
  );
}

/** What the page holds about its own Check for updates (the server's view is `server.update`). */
export interface CheckState {
  /** The POST is in flight. */
  requesting: boolean;
  /** A throttled check's words, or the page stopped waiting for a slow one. */
  note: string;
  /** The POST itself failed (not the registry read: that is `update.error`). */
  error: string;
}

interface UpdateProps {
  server: McpServerSummary;
  check: CheckState;
  onCheck: () => void;
  /** Update… for a package added by name: the version field with the published release in it. */
  onUpdate: (version: string) => void;
  /** Settings → Source offers Update from source whatever the hint says (#2427). */
  alwaysOfferSource?: boolean;
}

/** The Updates row (and Last answer, after a failed attempt): `<dt>`/`<dd>` pairs for a `kv` list. */
export function UpdateRows({ server, check }: Pick<UpdateProps, "server" | "check">) {
  const u = server.update;
  if (!u) return null;
  const w = hintWords(u);
  const checking = w.checking || check.requesting;
  const host = registryHost(server.source.kind);
  return (
    <>
      <dt>Updates</dt>
      <dd className={styles.stack} data-testid="mcp-update-line">
        {checking ? (
          <span role="status" data-testid="mcp-update-checking">
            Checking {host || "the registry"}…
          </span>
        ) : w.failed ? (
          <span data-testid="mcp-update-failed">{w.failed}</span>
        ) : (
          <span data-testid="mcp-update-answer">{w.answer}</span>
        )}
        {!checking && !w.failed && host && u.checked_at ? <span className={styles.srcSub}>{host}</span> : null}
      </dd>
      {!checking && w.failed && w.answer && (
        <>
          <dt>Last answer</dt>
          <dd data-testid="mcp-update-answer">{w.answer}</dd>
        </>
      )}
    </>
  );
}

/** The update action that fits the server, Check for updates, and what they do. */
export function UpdateActions({ server, check, onCheck, onUpdate, alwaysOfferSource = false }: UpdateProps) {
  const u = server.update;
  if (!u) return null;
  const w = hintWords(u);
  const checking = w.checking || check.requesting;
  const host = registryHost(server.source.kind);
  const latest = u.latest ?? "";
  const fromSource = !!server.origin;
  return (
    <div className={styles.updBlock} data-testid="mcp-update-block" data-state={u.state} data-checking={checking ? "1" : "0"}>
      <div className={styles.btnRow}>
        {fromSource && (w.available || alwaysOfferSource) && (
          <Link
            className={w.available ? styles.primary : styles.ghost}
            to={mcpServerUpdatePath(server.id)}
            data-testid="mcp-update-from-source"
          >
            <GitBranch size={14} aria-hidden="true" /> Update from source
          </Link>
        )}
        {!fromSource && w.available && (
          <button type="button" className={styles.primary} onClick={() => onUpdate(latest)} data-testid="mcp-update-package">
            <Download size={14} aria-hidden="true" /> Update…
          </button>
        )}
        <button
          type="button"
          className={styles.ghost}
          onClick={onCheck}
          disabled={checking}
          aria-busy={checking || undefined}
          data-testid="mcp-update-check"
        >
          <RefreshCw size={14} aria-hidden="true" /> {checking ? "Checking…" : "Check for updates"}
        </button>
      </div>
      {check.note && (
        <p className={styles.note} role="status" data-testid="mcp-update-note">
          {check.note}
        </p>
      )}
      {check.error && (
        <p className={styles.error} role="alert" data-testid="mcp-update-error">
          {check.error}
        </p>
      )}
      <p className={styles.note}>
        {w.available
          ? fromSource
            ? `${latest} is published on ${host || "the registry"}. Update from source reads the repository again and shows exactly what it would install; it may not have reached ${latest} yet.`
            : `Update… puts ${latest} in the version field. Save, then Review shows exactly what would run and installs it only when you press Install; the new install needs its own check.`
          : "BattleLab looks for a newer release once a day and when you ask, and never installs one by itself."}
      </p>
    </div>
  );
}

/** Rows and actions together, for a panel without a `kv` list of its own (Settings → Source). */
export function UpdateBlock(props: UpdateProps) {
  if (!props.server.update) return null;
  return (
    <div className={styles.updWrap}>
      <dl className={styles.kv}>
        <UpdateRows server={props.server} check={props.check} />
      </dl>
      <UpdateActions {...props} />
    </div>
  );
}
