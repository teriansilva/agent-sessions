/** Folder, git summary, session roster and JUMP IN (#878).
 *
 * `GET /api/missions/{id}/context` takes **no client path** — the server reads the mission's own
 * cwd — so there is nothing here that can be pointed somewhere it should not go. The git read
 * fails CLOSED and names only the exception KIND, never the path, and this component renders that
 * as a degraded state rather than as an empty panel.
 */
import { Link } from "react-router-dom";

import type { MissionContext as MissionContextData } from "../../types/api";

import type { DraftEdit } from "./draftDirection";
import { MissionScreen } from "./MissionScreen";

import { MissionSpawn } from "./MissionSpawn";

import styles from "./mission.module.css";

/** `engine:uuid` → `/s/:engine/:uuid`, both halves encoded. A bare `replace(":", "/")` is wrong
 *  for any id needing escaping, and silently so. */
function sessionRoute(key: string): string {
  const i = key.indexOf(":");
  const engine = i < 0 ? key : key.slice(0, i);
  const uuid = i < 0 ? "" : key.slice(i + 1);
  return `/s/${encodeURIComponent(engine)}/${encodeURIComponent(uuid)}`;
}

/** One line summarising the working tree. Each count is checked rather than assumed, so a
 *  server-side shape change degrades to a shorter line instead of rendering `undefined`. */
function gitLine(g: MissionContextData["git"]): string {
  if (!g) return "";
  const parts: string[] = [];
  const entries = Array.isArray(g.entries) ? g.entries : [];
  if (entries.length) parts.push(`${entries.length} changed`);
  const staged = entries.filter((e) => e.kind === "staged").length;
  if (staged) parts.push(`${staged} staged`);
  if (typeof g.ahead === "number" && g.ahead > 0)
    parts.push(`ahead ${g.ahead}`);
  if (typeof g.behind === "number" && g.behind > 0)
    parts.push(`behind ${g.behind}`);
  if (!parts.length) parts.push("clean");
  return parts.join(" · ");
}

export function MissionContextPanel({
  context,
  onMembershipChanged,
  loading,
  onDetach,
  busy,
  spawn,
  draftEdit,
  onDraftReplaced,
}: {
  context: MissionContextData | null;
  /** #983 P3: an AI draft opened for editing. It prefills the composer of THE SESSION IT WAS
   *  DRAFTED FOR, and no other, because replacing it types into that session. */
  draftEdit?: DraftEdit | null;
  onDraftReplaced?: (actionId: string) => void;
  /** The server told a control that this mission no longer holds its session. The roster the
   *  blocks are built from is stale, so it is re-read — that is what removes the block, and it
   *  is the console's to do because the roster is the console's (#903 review 3, finding 1). */
  onMembershipChanged?: () => void;
  loading?: boolean;
  /** Release one session from the mission (#889). Absent ⇒ read-only, which is what an archived
   *  or closed mission gets — detaching from a finished record changes history for no purpose.
   *
   *  Server-side this is FENCED: the withdrawal commits inside the same lock the write fence
   *  takes around its first byte, so an in-flight nudge either lands first or sees the new epoch.
   *  A detached session can never still be typed into, which is why this is a plain control
   *  rather than a confirm. */
  onDetach?: (sessionKey: string) => void;
  busy?: boolean;
  /** Start a bounded sub-agent alongside one session (#894). Absent ⇒ the mission cannot spawn:
   *  a closed record, or one not `running`. The control is withheld rather than shown-and-refused
   *  for the same reason RELEASE is. */
  spawn?: {
    engine: string;
    /** Where the sub-agent will run — shown to the operator and asserted back on START. */
    cwd: string;
    cap: number;
    live: number | null;
    onChanged: (opts?: { membershipChanged?: boolean }) => void;
    onNote: (msg: string) => void;
  };
}) {
  if (loading && !context) {
    return (
      <div className={styles.empty} data-testid="context-loading">
        Reading the mission's folder…
      </div>
    );
  }
  if (!context) return null;

  const sessions = context.sessions ?? [];
  const first = sessions[0]?.session_key;
  const branch = context.git?.branch;

  return (
    <div data-testid="mission-context">
      {context.cwd ? (
        <div className={styles.cwd}>{context.cwd}</div>
      ) : (
        <div className={styles.empty}>
          No folder yet — this mission is still a draft.
        </div>
      )}

      {context.git_error ? (
        // Fails CLOSED and says so. The server deliberately sends only the exception kind, so
        // this cannot leak a path even if the operator screenshots it.
        <div
          className={styles.notice}
          role="status"
          data-testid="context-git-error"
        >
          <div className={styles.noticeLead}>Git could not be read.</div>
          <div>{context.git_error} — the working tree is not shown.</div>
        </div>
      ) : context.git ? (
        <div className={styles.gitLine}>
          {branch ? `${branch} · ` : ""}
          {gitLine(context.git)}
        </div>
      ) : null}

      {sessions.length ? (
        <>
          {/* The roster is a COUNT rather than a list of keys: every key is printed on its own
              block below, beside the controls that act on it, and printing them twice made the
              ones above look like the labels for the blocks below when they are not necessarily
              in the same order (#903 review 2, finding 2). */}
          <div className={styles.roster}>
            {sessions.length} session{sessions.length === 1 ? "" : "s"}
          </div>
          {/* ONE BLOCK PER SESSION, and every control in it acts on THAT session (#894, #889).
              VIEW SCREEN is read-only — looking takes no lease and does not mark the viewer
              busy, so it cannot silently pause the follow-through — and RELEASE sits beside it
              rather than in a separate list, because a control whose target is named two
              elements away is the ambiguity #903's review was about. */}
          {sessions.map((s) => (
            <div
              key={s.session_key}
              className={styles.rosterRow}
              data-testid="roster-session"
            >
              <MissionScreen
                missionId={context.id}
                sessionKey={s.session_key}
                role={s.role ?? null}
                onGone={onMembershipChanged}
                prefill={
                  draftEdit && draftEdit.sessionKey === s.session_key
                    ? draftEdit
                    : null
                }
                onDraftReplaced={onDraftReplaced}
              />
              {spawn ? (
                <MissionSpawn
                  missionId={context.id}
                  parentKey={s.session_key}
                  engine={spawn.engine}
                  cwd={spawn.cwd}
                  live={spawn.live}
                  cap={spawn.cap}
                  busy={Boolean(busy)}
                  onChanged={spawn.onChanged}
                  onNote={spawn.onNote}
                />
              ) : null}
              {onDetach ? (
                <button
                  type="button"
                  className={styles.objEditBtn}
                  disabled={busy}
                  onClick={() => onDetach(s.session_key)}
                  data-testid="session-detach"
                  aria-label={`Release ${s.session_key} from this mission`}
                >
                  RELEASE
                </button>
              ) : null}
            </div>
          ))}
        </>
      ) : (
        <div className={styles.roster}>No sessions adopted yet.</div>
      )}

      {first ? (
        // A real route, not a handler — the operator can middle-click it, and it survives the
        // console being unmounted.
        <Link
          className={styles.jumpIn}
          to={sessionRoute(first)}
          data-testid="jump-in"
        >
          JUMP IN
        </Link>
      ) : null}
    </div>
  );
}
