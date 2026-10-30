import { useEngineRoster } from "../../app/engineRoster";
import { getsMemoryTool, memoryAgents } from "../memory/agentAccess";
import styles from "../../routes/Variables.module.css";

export const SECRETS_TOOL_TAG = "Secrets tool";
export const HELPER_TAG = "Helper command";

/** Which agents reach a secret through the secrets tool and which through the helper command
 *  (#1786), derived from the engine roster only: the tool rides the same `tools.mcp` channel as
 *  the memory tool, so an agent without it gets `$BATTLELAB_SECRETS_HELPER` instead. */
export function SecretChannels() {
  const { engines, loaded } = useEngineRoster();
  const agents = memoryAgents(engines);
  if (!loaded || agents.length === 0) return null;
  return <details className={styles.channels}>
    <summary>How agents reach secrets</summary>
    <ul aria-label="How each agent reaches secrets">
      {agents.map(e => <li key={e.id}>
        <span>{e.label}</span>
        <span className={styles.chip}>{getsMemoryTool(e) ? SECRETS_TOOL_TAG : HELPER_TAG}</span>
      </li>)}
    </ul>
    <p className={styles.help}>The secrets tool lists the secrets an agent may use and returns a private file path, never the content. Agents without it run <code>$BATTLELAB_SECRETS_HELPER list</code> or <code>$BATTLELAB_SECRETS_HELPER path NAME</code>.</p>
  </details>;
}
