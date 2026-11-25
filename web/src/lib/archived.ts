/** An archived session is frozen (#2064): no processes, and every start path refuses it until it
 *  is unarchived. The server's refusal of a send, a decision or a start to such a session is HTTP
 *  409 with exactly this detail — the backend's `archive_gate.ARCHIVED_DETAIL`. One constant, so a
 *  reworded server string fails `archived.test.ts` instead of silently becoming a generic refusal. */
export const ARCHIVED_DETAIL = "this session is archived — unarchive it to continue";

/** Was `e` the server refusing a request because the session is archived? Duck-typed on the
 *  `ApiError` shape (`status` + `message` = the server's `detail`) rather than `instanceof`, so a
 *  test that replaces the api module cannot make this throw. */
export function isArchivedRefusal(e: unknown): boolean {
  if (typeof e !== "object" || e === null) return false;
  const { status, message } = e as { status?: unknown; message?: unknown };
  return status === 409 && message === ARCHIVED_DETAIL;
}
