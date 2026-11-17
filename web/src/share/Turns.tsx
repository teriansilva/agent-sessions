import type { ReactNode } from "react";
import { Avatar, type AvatarSpec } from "../components/chat/Avatar";
import { Markdown } from "../components/structured/Markdown";
import { whoInitial } from "../lib/who";
import { imageUrl, operatorAvatarUrl, type PublicRequest, type PublicTurn } from "./publicApi";
import styles from "./ShareViewer.module.css";

/** The turn components of the read-only conversation (#1404 P4). The public viewer (`ApiView`)
 *  renders its turns with them, and so does the pane of a session another instance shares
 *  (`components/federation/RemotePane`, #2055): one rendering path for a projected turn, so the
 *  text policy is the same wherever untrusted turns are shown. A message, a name and a tool line
 *  are text nodes; a reply goes through `Markdown` (no raw HTML, http(s)/mailto links only with
 *  `noopener noreferrer`, images never loaded). */

/** A turn's state, in the visitor's words: approvals belong to the owner, never to them. */
function stateLabel(state: string): string | null {
  switch (state) {
    case "running":
      return "working";
    case "queued":
      return "queued — sent after the current turn";
    case "awaiting_approval":
      return "waiting for the owner";
    case "interrupted":
      return "interrupted";
    case "failed":
      return "failed";
    case "uncertain":
      return "outcome unknown";
    default:
      return null;
  }
}

/** The label on a person's block. The operator's is the display name they chose (#1942), else
 *  "Operator" — never their login name, which a visitor never learns. */
function who(turn: PublicTurn, operatorName: string | null | undefined): string {
  return turn.author?.via === "public_link"
    ? `${turn.author.name} · via public link`
    : operatorName || "Operator";
}

/** The disc beside a person's message (#1931). Without an image the operator's shows the first
 *  letter of the display name they chose (#1942), else `O`: a visitor never learns the operator's
 *  login name. */
function whoAvatar(
  turn: PublicTurn,
  operatorAvatar: string | null | undefined,
  operatorName: string | null | undefined,
): AvatarSpec {
  if (turn.author?.via === "public_link") return { kind: "who", name: turn.author.name };
  const initial = operatorName ? whoInitial(operatorName) : "O";
  return operatorAvatar
    ? { kind: "image", src: operatorAvatarUrl(operatorAvatar), fallback: initial }
    : { kind: "accent", initial };
}

/** The operator pane's working animation. */
export function Bars() {
  return (
    <span className={styles.bars} aria-hidden="true">
      <span />
      <span />
      <span />
    </span>
  );
}

function toolCount(n: number, truncated: boolean): string {
  const s = n === 1 ? "" : "s";
  return `${truncated ? "Latest " : ""}${n} command${s} & tool${s}`;
}

/** One projected turn: the person's block (text, attachments, state) and the agent's (steps
 *  folded behind one line, the reply, the working row). */
export function ShareTurn({
  turn: t,
  operatorAvatar,
  operatorName,
  queueAction,
  images = true,
}: {
  turn: PublicTurn;
  /** The operator's avatar version (public viewer only), else none. */
  operatorAvatar?: string | null;
  operatorName?: string | null;
  /** A control beside a queued message's state line (the public viewer's Send now). */
  queueAction?: ReactNode;
  /** `false`: an attachment is never loaded, whatever it carries; it shows as its name. */
  images?: boolean;
}) {
  const label = stateLabel(t.state);
  const attachments = t.attachments ?? [];
  const tools = t.tools ?? [];
  const own = t.kind === "agent";
  const moreTools = t.tools_truncated || (t.tools_omitted ?? 0) > 0;
  return (
    <section className={styles.turn} data-turn={t.turn_id}>
      {!own && (t.text || attachments.length > 0) && (
        <div className={styles.block} data-author={t.author ? "visitor" : "operator"}>
          <Avatar spec={whoAvatar(t, operatorAvatar, operatorName)} className={styles.avatar} />
          <p className={styles.who}>{who(t, operatorName)}</p>
          {t.text && <p className={styles.text}>{t.text}</p>}
          {attachments.length > 0 && (
            <div className={styles.images}>
              {attachments.map((a, i) =>
                images && a.image ? (
                  <img
                    key={i}
                    className={styles.image}
                    src={imageUrl(a.image)}
                    alt={a.name || "attached image"}
                    referrerPolicy="no-referrer"
                  />
                ) : (
                  <span key={i} className={styles.chip}>
                    {a.name || a.mime}
                  </span>
                ),
              )}
            </div>
          )}
          {label && t.state !== "running" && t.state !== "awaiting_approval" && (
            <p className={styles.state}>
              {label}
              {queueAction}
            </p>
          )}
        </div>
      )}
      {(t.reply || tools.length > 0 || t.state === "running" || (own && label)) && (
        <div className={styles.block} data-author="agent">
          <Avatar spec={{ kind: "agent" }} className={styles.avatar} />
          <p className={styles.who}>
            Agent{own ? " · on its own" : ""}
            {t.state === "running" ? " · working" : ""}
            {own && label && t.state !== "running" ? ` · ${label}` : ""}
          </p>
          {tools.length > 0 && (
            // Folded behind one line, as in the operator's pane: the reply is what a
            // visitor reads; the steps are a tap away.
            <details className={styles.activity} data-testid="share-activity">
              <summary>
                {toolCount(tools.length, moreTools)}
                {t.state === "running" && " · working"}
                {tools.some((tool) => /failed|error/i.test(tool.outcome ?? "")) && (
                  <span className={styles.activityFailed}> · failed steps</span>
                )}
              </summary>
              <ul className={styles.tools}>
                {tools.map((tool, i) => (
                  <li key={i} className={styles.tool}>
                    <span className={styles.toolName}>{tool.name}</span>
                    {tool.summary && <span className={styles.toolSummary}> · {tool.summary}</span>}
                  </li>
                ))}
                {moreTools && <li className={styles.tool}>… more steps not shown</li>}
              </ul>
            </details>
          )}
          {t.reply && <Markdown text={t.reply} />}
          {t.state === "running" && (
            <div className={styles.wait} role="status" data-testid="share-working">
              <Bars />
              <span className={styles.waitText}>
                Working
                {tools.length > 0 && (
                  <>
                    {" · "}
                    <b>{tools[tools.length - 1].name}</b>
                    {tools[tools.length - 1].summary && ` ${tools[tools.length - 1].summary}`}
                  </>
                )}
              </span>
            </div>
          )}
        </div>
      )}
      {t.truncated && (
        // The sender cut this turn to fit its byte budget (a session another instance shares).
        <p className={styles.cut} data-testid="turn-truncated">
          Part of this turn was cut to fit.
        </p>
      )}
    </section>
  );
}

/** A pending approval, as someone who cannot answer it sees it. */
export function ShareApproval({ request: q }: { request: Pick<PublicRequest, "summary"> }) {
  return (
    <section className={styles.approval} data-testid="share-approval">
      <p className={styles.tag}>Approval needed</p>
      {q.summary && <p className={styles.text}>{q.summary}</p>}
      <p className={styles.waiting}>Waiting for the owner. Only the owner can approve.</p>
    </section>
  );
}
