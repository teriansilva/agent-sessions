/** The operator's own disc (#1931), kept out of `Avatar.tsx`: the component file exports only
 *  components (fast refresh), and the share bundle (which imports `Avatar`) must never carry the
 *  operator's routes (`shareBundle.test.ts`). Operator surfaces only. */
import type { AvatarSpec } from "./Avatar";
import { operatorInitials } from "../shell/operatorInitials";

/** The operator's own disc: their image once its object URL is loaded (`useOperatorAvatarSrc`,
 *  through the fetch seam so it works over the Home Free relay), else their initial on the
 *  accent (`Y` for "you" where there is no login name). */
export function operatorAvatarSpec(
  src: string | null | undefined,
  username: string | null | undefined,
): AvatarSpec {
  const initial = username ? ([...operatorInitials(username)][0] ?? "Y") : "Y";
  return src ? { kind: "image", src, fallback: initial } : { kind: "accent", initial };
}
