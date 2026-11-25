/** Library → Artifacts' two views (#2347 P1): MY ARTIFACTS (`ARTIFACTS_PATH`) and REPOSITORIES
 *  (`ARTIFACT_REPOS_PATH`). Each view is its own route, so a tab is a link the URL keeps; the
 *  shared `Tabs` primitive draws them (its marker, its keys). Choosing a tab navigates, and the
 *  other view's tab bar takes the focus back, so the keyboard stays on the tabs. */
import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { Tabs } from "../hud/Controls";
import { ARTIFACTS_PATH, ARTIFACT_REPOS_PATH } from "../../lib/routes";
import styles from "./artifacts.module.css";

export type ArtifactsView = "mine" | "repos";

const PATHS: Record<ArtifactsView, string> = {
  mine: ARTIFACTS_PATH,
  repos: ARTIFACT_REPOS_PATH,
};

export function ArtifactsViewTabs({ value }: { value: ArtifactsView }) {
  const navigate = useNavigate();
  const location = useLocation();
  const box = useRef<HTMLDivElement | null>(null);
  const fromTab = (location.state as { artifactsTab?: boolean } | null)?.artifactsTab;
  useEffect(() => {
    if (fromTab)
      box.current
        ?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
        ?.focus();
  }, [fromTab]);
  return (
    <div ref={box} className={styles.viewTabs}>
      <Tabs<ArtifactsView>
        label="Artifacts views"
        value={value}
        onChange={(id) => {
          if (id !== value) navigate(PATHS[id], { state: { artifactsTab: true } });
        }}
        tabs={[
          { id: "mine", label: "My artifacts" },
          { id: "repos", label: "Repositories" },
        ]}
      />
    </div>
  );
}
