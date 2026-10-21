/** The Memory page's "Agent access" card (#1506 P2): how each agent reaches the project's memory.
 *
 *  Built from the engine roster, never a list of its own: an agent whose manifest declares
 *  `[tools] mcp` gets BattleLab's memory tool at launch ("memory tool"); every other agent gets the
 *  two environment variables and needs the paragraph pasted into its instruction file. The
 *  paragraph sits behind a disclosure, for those agents only. Off is not revocation: the off copy
 *  says new sessions get nothing and running ones keep access until they restart or stop. */
import { Copy } from "lucide-react";
import { useEngineRoster } from "../../app/engineRoster";
import { MEMORY_GUIDE_URL } from "../../lib/links";
import buttons from "../ui/actionButton.module.css";
import { getsMemoryTool, MEMORY_TOOL_TAG, memoryAgents, OFF_TEXT, PARAGRAPH_TAG, paragraphFiles } from "./agentAccess";
import styles from "./agentAccess.module.css";

export function AgentAccessCard({
  enabled,
  paragraph,
  onCopy,
}: {
  enabled: boolean;
  paragraph: string;
  onCopy: () => void;
}) {
  const roster = useEngineRoster();
  const agents = memoryAgents(roster.engines);
  const files = paragraphFiles(agents);
  return (
    <section className={styles.card} aria-label="Agent access">
      <h2 className={styles.eyebrow}>Agent access</h2>
      {!enabled ? (
        <p className={styles.off}>
          <span className={`hud-led idle ${styles.led}`} aria-hidden="true" />
          <span>{OFF_TEXT}</span>
        </p>
      ) : (
        <>
          <p className={styles.lede}>Agents get the BattleLab memory tool automatically when memory is on.</p>
          {!roster.loaded ? (
            <p className={styles.note}>Loading agents…</p>
          ) : (
            <ul className={styles.agents} aria-label="How each agent reaches memory">
              {agents.map((e) => {
                const tool = getsMemoryTool(e);
                return (
                  <li key={e.id} className={styles.agent} data-tool={tool}>
                    <span className={styles.name}>{e.label}</span>
                    <span className={tool ? styles.toolTag : styles.paragraphTag}>
                      {tool ? MEMORY_TOOL_TAG : PARAGRAPH_TAG}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
          <p className={styles.note}>Sessions already running join after a restart.</p>
          <details className={styles.fallback}>
            <summary>For agents without the tool</summary>
            <div className={styles.fallbackBody}>
              <p className={styles.note}>
                Paste once into the project's{" "}
                {(files.length ? files : ["AGENTS.md"]).map((f, i) => (
                  <span key={f}>
                    {i > 0 && " / "}
                    <code>{f}</code>
                  </span>
                ))}
                . BattleLab never edits your files.
              </p>
              <pre className={styles.paragraph}>{paragraph}</pre>
              <button type="button" className={buttons.ghost} onClick={onCopy}>
                <Copy size={14} aria-hidden="true" /> Copy paragraph
              </button>
            </div>
          </details>
        </>
      )}
      <a className={styles.guideLink} href={MEMORY_GUIDE_URL} target="_blank" rel="noopener noreferrer">
        Which agents can reach it
      </a>
    </section>
  );
}
