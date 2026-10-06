import { Link } from "react-router-dom";

import { CHECKLISTS_PATH, TEMPLATES_PATH } from "../lib/routes";
import styles from "./Ask.module.css";

/** Library → Playbooks (#1294). Playbooks — how a repository works: flows of agents and models,
 *  step checklists, instructions and templates (#1096) — are in the works, and their page is #1192.
 *  Until it lands, the Library entry is here so the operator can see where they will live, and the
 *  page says so plainly and points at the parts that exist today. #1192 replaces this body. */
export default function Playbooks() {
  return (
    <div className={styles.page} data-testid="playbooks-page">
      <div className={styles.placeholder}>
        <div className={styles.kicker}>Library // playbooks</div>
        <h1 className={styles.h1}>Playbooks</h1>
        <p className={styles.sub}>
          A playbook says how a repository works: which agents and models run
          each step, the checklist each step must pass, and the instructions and
          templates they start from. Playbooks are in the works.
        </p>
        <p className={styles.sub}>
          Until then, the parts exist on their own:{" "}
          <Link to={CHECKLISTS_PATH}>Checklists</Link> say what “done” means for a
          mission, and <Link to={TEMPLATES_PATH}>Templates</Link> hold the
          instructions sessions start from.
        </p>
      </div>
    </div>
  );
}
