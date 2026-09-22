import { Link } from "react-router-dom";

import { AskConsole } from "../components/ask/AskConsole";
import { useConfig } from "../app/config";
import { settingsPath } from "./settingsTabs";
import styles from "./Ask.module.css";

/** ASK — its own top-level route since #1058, beside Sessions / Missions / Map / Templates.
 *
 *  It was a mode of the mission composer, which meant a question about SESSIONS could only be asked
 *  from inside the Missions section, and could not be linked to at all. The page is thin on purpose:
 *  the console below it is the same box, the same turns and the same `POST /api/pulse/ask` the
 *  composer sent, so the only thing this file adds is the page it now has.
 *
 *  `configured` is the AI endpoint's own flag (`/api/config → pulse.configured`), read here rather
 *  than inside the console so the page can say what to DO about a missing endpoint — a disabled
 *  field with a placeholder is a symptom, and the operator needs the route to Settings. The console
 *  still disables itself on the same flag; this is the explanation, not the guard.
 */
export default function Ask() {
  const configured = useConfig()?.pulse?.configured ?? false;

  return (
    <div className={styles.page} data-testid="ask-page">
      <div className={styles.inner}>
        <div className={styles.kicker}>Ask // your work</div>
        <h1 className={styles.h1}>Ask about your work</h1>
        <p className={styles.sub}>
          Find a session by what happened in it, or ask what you did and when.
          Answers are read from the transcripts this install can already see.
        </p>
        {!configured ? (
          <div className={styles.needsEndpoint} data-testid="ask-needs-endpoint">
            Ask needs an AI endpoint — it has no local fallback. Set one up in{" "}
            <Link to={settingsPath("ai-endpoint")}>
              Settings → Endpoint &amp; model
            </Link>
            , then come back.
          </div>
        ) : null}
        <AskConsole configured={configured} />
      </div>
    </div>
  );
}
