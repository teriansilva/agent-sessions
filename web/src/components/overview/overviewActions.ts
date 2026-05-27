import { createContext, useContext } from "react";

/** Lets a custom React Flow node (which can't take arbitrary props) call back into the
 *  canvas — e.g. a cluster header toggling its own collapsed state (#144). */
export const OverviewActions = createContext<{ toggle: (cwd: string) => void }>({
  toggle: () => {},
});

export const useOverviewActions = () => useContext(OverviewActions);
