/** The terminal socket's deliberate close codes that the PANE reads (#1502), in their own module
 *  so a component can name them without importing the transport (tests replace `termSocket`). The
 *  full taxonomy and the retry policy live in `termSocket.ts`. */

/** No such session (or it has ended): the id did not parse, or nothing can attach or launch it. */
export const CLOSE_NOT_FOUND = 4404;
/** Not a terminal session: the engine runs as `api` / `chat`, so the page drew the wrong pane. */
export const CLOSE_NOT_A_TERMINAL = 4415;
