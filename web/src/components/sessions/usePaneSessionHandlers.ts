import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useConfig } from "../../app/config";
import { api } from "../../lib/api";
import { SESSIONS_PATH } from "../../lib/routes";
import {
  announceSessionRowChanged,
  projectAfterMove,
  reviewPatch,
} from "../../lib/sessionRowEvents";
import type { ProjectRef, Session } from "../../types/api";
import type { SessionMenuHandlers } from "./useSessionMenu";

/** What the session menu does when it is opened from the session's OWN pane (#2173).
 *
 *  The same routes the sidebar row and the map call. The pane owns no list, so each change is
 *  ANNOUNCED with the server's echoed values (`sessionRowEvents`): the sidebar's list applies it
 *  to its row exactly as its own handlers do, and the store applies it to the looked-up row of a
 *  session the list does not carry, which is the row this pane reads. Nothing is patched here.
 *
 *  **Archive leaves the pane**, as archiving the open session from the sidebar does
 *  (`SessionList.handleToggleArchive`, #631): the socket must be gone, or a still-mounted
 *  terminal would relaunch into a session that is now archived. The server reaps the runtime
 *  (#523); this only navigates, after the request succeeded. A refused archive throws before
 *  either, so the pane stays. Unarchive stays on the pane.
 *
 *  A failure rejects: the menu's `runBusy` re-enables its items and the row keeps the state the
 *  server still holds. AI-review items are offered only where AI review is configured (#356),
 *  the sidebar's gate. */
export function usePaneSessionHandlers(session: Session): Omit<SessionMenuHandlers, "onEdit"> & {
  onRename: (id: string, title: string) => Promise<void>;
  onSetTag: (id: string, tag: string) => Promise<void>;
} {
  const navigate = useNavigate();
  const aiConfigured = useConfig()?.ai_review?.configured ?? false;
  const cwd = session.cwd;
  return useMemo(
    () => ({
      onRename: async (id: string, title: string) => {
        const r = await api.rename(id, title);
        announceSessionRowChanged(id, { title: r.title });
      },
      onSetTag: async (id: string, tag: string) => {
        const r = await api.setTag(id, tag);
        announceSessionRowChanged(id, { tag: r.tag });
      },
      onToggleFavorite: async (id: string, value: boolean) => {
        const r = await (value ? api.favorite(id) : api.unfavorite(id));
        announceSessionRowChanged(id, { sticky: r.sticky });
      },
      onSetProject: async (id: string, ref: ProjectRef | null) => {
        await api.setSessionProject(id, ref && ref.kind === "project" ? ref.id : null);
        announceSessionRowChanged(id, { project: projectAfterMove(ref, cwd) });
      },
      onToggleArchive: async (id: string, currentlyArchived: boolean) => {
        await (currentlyArchived ? api.unarchive(id) : api.archive(id));
        announceSessionRowChanged(id, { archived: !currentlyArchived });
        if (!currentlyArchived) navigate(SESSIONS_PATH);
      },
      ...(aiConfigured
        ? {
            onReviewNow: async (id: string) => {
              announceSessionRowChanged(id, reviewPatch(await api.reviewNow(id)));
            },
            onToggleReviewExcluded: async (id: string, excluded: boolean) => {
              const r = await api.reviewExclude(id, excluded);
              announceSessionRowChanged(id, { review_excluded: r.review_excluded });
            },
            onToggleOrchestratorExcluded: async (id: string, excluded: boolean) => {
              const r = await api.setOrchestratorExcluded(id, excluded);
              announceSessionRowChanged(id, { orchestrator_excluded: r.orchestrator_excluded });
            },
          }
        : {}),
    }),
    [navigate, aiConfigured, cwd],
  );
}
