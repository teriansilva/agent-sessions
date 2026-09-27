/** Missions → Checklists: the mission playbooks editor as a page of its own.
 *
 *  The editor itself is unchanged (`MissionPlaybooks`, #892). It moved out of Settings → AI because
 *  what "done" means for a mission is mission work, not a setting — it now sits in the nav beside
 *  the console that uses it, and `/settings/ai-playbooks` redirects here. */
import { MissionPlaybooks } from "./MissionPlaybooks";

import styles from "./Templates.module.css";

export default function Checklists() {
  return (
    <div className={styles.page} data-testid="checklists-page">
      <MissionPlaybooks asPage />
    </div>
  );
}
