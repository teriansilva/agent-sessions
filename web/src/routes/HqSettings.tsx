/** One settings home for HQ (#2037). Existing controls keep their own save/lifecycle logic and
 * anchors. A hash jump is local navigation, so it never unmounts another editor with a save pending. */
import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { HqContactGallery } from "../components/hq/HqContactGallery";
import { ConnectorsSettings } from "./ConnectorsSettings";
import { ForgeSettings } from "./ForgeSettings";
import { HqAuthoritySettings } from "./HqAuthoritySettings";
import { HqCallsSettings } from "./HqCallsSettings";
import { HqReportsSettings } from "./HqReportsSettings";
import { HqRouteSettings } from "./HqRouteSettings";
import { HqModelsSettings } from "./HqModelsSettings";
import { MemorySearchSettings } from "./MemorySearchSettings";
import { OrchestratorSettings } from "./OrchestratorSettings";
import { PulseSettings } from "./PulseSettings";
import { settingsPath } from "./settingsTabs";
import styles from "./Settings.module.css";

const GROUPS = [
  ["hq-authority-h", "Authority"], ["hq-contact", "Contact"], ["hq-models-h", "HQ models"], ["hq-route-h", "Question routing"],
  ["memory-search-h", "Memory search"], ["orch-h", "Autonomy"],
  ["hq-reports-h", "Reports"], ["pulse-h", "Recent window"], ["hq-forge", "Mission checks"],
  ["hq-calls", "Calls"], ["hq-connectors", "Connectors"],
] as const;

export function HqSettings() {
  const { hash } = useLocation();
  useEffect(() => {
    if (!hash) return;
    const frame = requestAnimationFrame(() => {
      document.getElementById(hash.slice(1))?.scrollIntoView?.({ block: "start" });
    });
    return () => cancelAnimationFrame(frame);
  }, [hash]);
  return (
    <>
      <nav className={styles.hqGroups} aria-label="HQ settings groups">
        {GROUPS.map(([id, label]) => (
          <Link className={styles.secBtnGhost} to={settingsPath("ai-hq", id)} key={id}>{label}</Link>
        ))}
      </nav>
      <HqAuthoritySettings />
      <section className={styles.section} aria-labelledby="hq-contact-h">
        <HqContactGallery subheadClass={styles.subhead} hintClass={styles.hint} />
      </section>
      <HqModelsSettings />
      <HqRouteSettings />
      <MemorySearchSettings />
      <div id="orch-h" className={styles.hqPolicies}>
        <OrchestratorSettings scope="session" />
        <OrchestratorSettings scope="mission" />
      </div>
      <HqReportsSettings />
      <PulseSettings />
      <div id="hq-forge"><ForgeSettings /></div>
      <div id="hq-calls"><HqCallsSettings /></div>
      <div id="hq-connectors"><ConnectorsSettings /></div>
    </>
  );
}
