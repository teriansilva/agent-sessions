/** Missions → Checklists: the mission playbooks editor as a page of its own.
 *
 *  It moved out of Settings → AI because what "done" means for a mission is mission work, not a
 *  setting — it sits in the nav beside the console that uses it, and `/settings/ai-playbooks`
 *  redirects here. Since #1221 it is a Missions page in the shell too: the sidebar lists MISSIONS,
 *  from the console's own rail, and picking one opens it in the console.
 *
 *  **Leaving with unsaved checklists asks first (#1221).** The sidebar made leaving one tap away —
 *  a mission row, "+ New mission" — and leaving unmounts the editor and its draft with it. Same
 *  guard as the template editor: the router's blocker for every in-app exit, `beforeunload` for
 *  the one it cannot see. Switching between the list and one checklist is not a route change and
 *  never asks. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useBlocker, useNavigate } from "react-router-dom";

import { MissionConsole } from "../components/pulse/MissionConsole";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { MISSION_PATH } from "../lib/routes";
import { MissionPlaybooks } from "./MissionPlaybooks";

import styles from "./Templates.module.css";

export default function Checklists() {
  const navigate = useNavigate();
  const onSelect = useCallback(
    (id: string) => navigate(`${MISSION_PATH}?m=${encodeURIComponent(id)}`),
    [navigate],
  );
  const onNewMission = useCallback(() => navigate(MISSION_PATH), [navigate]);
  const railOnly = useMemo(
    () => ({ onSelect, onNewMission }),
    [onSelect, onNewMission],
  );

  const [leave, setLeave] = useState<"clean" | "dirty" | "saving">("clean");
  const unsaved = leave !== "clean";
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      unsaved && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (!unsaved) return;
    const onBefore = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", onBefore);
    return () => window.removeEventListener("beforeunload", onBefore);
  }, [unsaved]);

  return (
    <div className={styles.page} data-testid="checklists-page">
      <MissionConsole allCards={[]} configured={false} railOnly={railOnly} />
      <MissionPlaybooks asPage onUnsavedChange={setLeave} />
      {blocker.state === "blocked" && (
        <ConfirmDialog
          tag={leave === "saving" ? "Save in progress" : "Unsaved changes"}
          title="Mission checklists"
          cancelLabel="Keep editing"
          confirmLabel={
            leave === "saving" ? "Leave anyway" : "Discard and leave"
          }
          danger
          onCancel={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
        >
          {/* A SAVE ALREADY ON THE WIRE IS NOT CANCELLED by leaving (Hermes on #1221): the server
              may accept it. So the dialog does not promise a discard it cannot deliver. */}
          <p>
            {leave === "saving"
              ? "A save is still in flight. If you leave, it may still be applied — you just won't see the result here."
              : "Your checklist edits are not saved. Leave and discard them, or stay and save."}
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
