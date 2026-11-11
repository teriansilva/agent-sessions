/** The operator's own disc (#1931), kept out of `Avatar.tsx`: the component file exports only
 *  components (fast refresh), and the share bundle (which imports `Avatar`) must never carry the
 *  operator's routes (`shareBundle.test.ts`). Operator surfaces only. */
import type { AvatarSpec } from "./Avatar";
import { operatorInitials } from "../shell/operatorInitials";

/** The operator's own disc, from `/api/config` (`avatar`, `username`): their image, else their
 *  initial on the accent (`Y` for "you" where there is no login name). */
export function operatorAvatarSpec(
  version: string | null | undefined,
  username: string | null | undefined,
): AvatarSpec {
  const initial = username ? ([...operatorInitials(username)][0] ?? "Y") : "Y";
  return version
    ? { kind: "image", src: `/api/profile/avatar?v=${encodeURIComponent(version)}`, fallback: initial }
    : { kind: "accent", initial };
}
