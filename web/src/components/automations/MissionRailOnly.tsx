/** The Missions sidebar on an Automations page (#1201), the way Checklists mounts it (#1221): the
 *  console's own rail, portalled into the shell's sidebar slot. Picking a mission opens it in the
 *  console; "+ New mission" opens the console's composer. */
import { useCallback, useMemo } from "react";
import { useNavigate } from "react-router-dom";

import { MissionConsole } from "../pulse/MissionConsole";
import { MISSION_PATH } from "../../lib/routes";

export function MissionRailOnly() {
  const navigate = useNavigate();
  const onSelect = useCallback(
    (id: string) => navigate(`${MISSION_PATH}?m=${encodeURIComponent(id)}`),
    [navigate],
  );
  const onNewMission = useCallback(() => navigate(MISSION_PATH), [navigate]);
  const railOnly = useMemo(() => ({ onSelect, onNewMission }), [onSelect, onNewMission]);
  return <MissionConsole allCards={[]} configured={false} railOnly={railOnly} />;
}
