import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { refreshRemoteShares, useRemoteShares, useRemoteSharesStatus } from "../../app/remoteShares";
import {
  GRANT_ID_RE,
  PEER_ID_RE,
  REMOTE_GONE,
  REMOTE_RETRY_MS,
  shareKind,
  type ShareKind,
} from "../../lib/federation";
import { RemoteFrame } from "./RemoteFrame";
import { RemotePane } from "./RemotePane";
import { RemoteScreenPane } from "./RemoteScreenPane";

/** The route `/remote/:peerId/:grantId` (#2055): a session another instance shares, read only.
 *
 *  It is its own route and component tree. It never goes through the local session route,
 *  `useSessionRow`, or anything keyed by a local session key, and the remote key is not parsed
 *  here: the two route ids go to the server as they are, once their shape is checked.
 *
 *  **The pane is chosen by the mirrored kind (#2060)**: a terminal is read as its screen
 *  (`RemoteScreenPane`), an API session as its conversation (`RemotePane`). The kind is the list
 *  read's own word for this grant, never anything in the URL. A terminal share is never drawn by
 *  `RemotePane` and an API share never by `RemoteScreenPane`. The choice the list made is kept
 *  per target: a share that ends leaves the list, and the pane that was showing it must say so
 *  itself, not turn into the other pane. A choice made WITHOUT the list's word (the list was
 *  read and did not have the share yet) is only a fallback, and gives way the moment the list
 *  names the share's kind. */
export function RemoteSessionView() {
  const { peerId = "", grantId = "" } = useParams<{ peerId: string; grantId: string }>();
  if (!PEER_ID_RE.test(peerId) || !GRANT_ID_RE.test(grantId))
    return <RemoteFrame title="Shared session" peer={null} word="unavailable" empty={REMOTE_GONE} />;
  return <RemoteChoice peerId={peerId} grantId={grantId} />;
}

function RemoteChoice({ peerId, grantId }: { peerId: string; grantId: string }) {
  const target = `${peerId}/${grantId}`;
  const shared = useRemoteShares();
  const { loaded, attempts } = useRemoteSharesStatus();
  const listed = shared.find((r) => r.peer_id === peerId && r.grant_id === grantId) ?? null;
  const [chosen, setChosen] = useState<{ target: string; kind: ShareKind; listed: boolean } | null>(
    null,
  );
  const mine = chosen?.target === target ? chosen : null;
  let kind = mine?.kind ?? null;
  if (listed && !mine?.listed) {
    // The list's word decides, and is kept: nothing chosen yet, or only the fallback below.
    kind = shareKind(listed);
    setChosen({ target, kind, listed: true });
  } else if (kind === null && loaded) {
    // Read and not listed: the conversation pane, which asks the owner's instance as it always
    // did and shows what that answers (a share the mirror does not have is a 404 there, with no
    // call to anybody). If the list names the share later, its kind replaces this.
    kind = "api";
    setChosen({ target, kind, listed: false });
  }
  // The list could not be read yet, so the kind is not known: ask again until it can. Nothing
  // is requested from the owner before the pane is chosen.
  const undecided = kind === null;
  useEffect(() => {
    if (!undecided || attempts === 0) return;
    const timer = setTimeout(() => void refreshRemoteShares(), REMOTE_RETRY_MS);
    return () => clearTimeout(timer);
  }, [undecided, attempts]);

  if (kind === null)
    return <RemoteFrame title="Shared session" peer={null} word="connecting" empty="Loading…" />;
  // A fallback pane is replaced, not reused, when the list's word arrives: what it concluded
  // without the list (a 404 is final there) is not carried into the listed share's pane.
  const by = mine?.listed || listed ? "listed" : "fallback";
  return kind === "terminal" ? (
    <RemoteScreenPane key={by} peerId={peerId} grantId={grantId} />
  ) : (
    <RemotePane key={by} peerId={peerId} grantId={grantId} />
  );
}
