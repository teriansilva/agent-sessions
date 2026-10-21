import { CornerDownLeft } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import styles from "./filePanel.module.css";

/** How long the "it landed" flash stays lit. Long enough to register, short enough not to look
 *  like a persistent state the row is now in. */
const FLASH_MS = 900;

/** The per-row "put this path in the message box" control (#792).
 *
 *  A sibling of the row's open-the-viewer button, never nested inside it: two interactive
 *  elements inside one another is invalid HTML and gives touch two overlapping targets.
 *
 *  It confirms at the point of ACTION rather than at the point of effect. On mobile the compose
 *  box is behind the panel sheet, so a change there is invisible at the moment of the tap — a
 *  confirmation the user cannot see is not a confirmation.
 */
export function SendPath({
  path,
  name,
  onSendPath,
}: {
  path: string;
  name: string;
  onSendPath: (path: string) => void;
}) {
  const [flash, setFlash] = useState(false);
  const timer = useRef<number | null>(null);

  // The flash is a timer, and a row can unmount while it is running (a refresh, a collapsed
  // parent). Clearing on unmount keeps it from setting state on a gone component.
  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  return (
    <button
      type="button"
      data-send-path={path}
      className={`${styles.send} ${flash ? styles.sendFlash : ""}`}
      // The row's own title names the file; this one has to name the ACTION, or a screen reader
      // hears two controls that sound identical.
      aria-label={`Add ${name} to the message`}
      title={`Add ${name} to the message`}
      onClick={() => {
        // No `stopPropagation` here, deliberately. The first version had one, with a comment
        // claiming it stopped the row behind from also opening the viewer — but removing it
        // changed no test, because the restructure made this control a SIBLING of the row
        // button rather than a child. There is nothing to bubble into. The sibling structure is
        // what prevents the double-fire; a guard that does nothing would just imply otherwise.
        onSendPath(path);
        setFlash(true);
        if (timer.current !== null) window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setFlash(false), FLASH_MS);
      }}
    >
      <CornerDownLeft size={13} aria-hidden="true" />
    </button>
  );
}
