import { useCallback } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { AgentSetupFlow, type SetupFrame } from "../components/plugins/AgentSetupFlow";
import { AGENTS_PATH } from "../components/plugins/model";
import { WizardShell } from "../components/wizard/WizardShell";
import button from "../components/ui/actionButton.module.css";

/** Settings → Agents → Add agent (`/settings/agents/setup/new`). The flow itself is
 *  `AgentSetupFlow` (#1739), shared with the setup wizard; this route adds what only a page has:
 *  the URL holds the setup query (so a reload or Back reads the same operation, never replays it),
 *  and `WizardShell` guards leaving while something would be lost. */
export default function AgentSetup() {
  const location = useLocation();
  const navigate = useNavigate();
  const replace = useCallback((path: string) => navigate(path, { replace: true }), [navigate]);
  return <AgentSetupFlow search={location.search} onNavigate={replace} frame={PageFrame} />;
}

function PageFrame(f: SetupFrame) {
  return (
    <WizardShell
      kicker="ADD AGENT"
      steps={f.steps}
      current={f.at}
      heading={f.heading}
      onBack={f.onBack}
      onNext={f.onNext}
      nextLabel={f.nextLabel}
      nextDisabled={f.nextDisabled}
      secondary={
        <Link className={button.ghost} to={AGENTS_PATH}>
          {f.enabled ? "Back to agents" : "Cancel"}
        </Link>
      }
      leaveGuard={f.dirty}
      leaveTitle={f.leaveTitle}
      leaveMessage={f.leaveMessage}
      leaveConfirmLabel="Leave setup"
    >
      {f.body}
    </WizardShell>
  );
}
