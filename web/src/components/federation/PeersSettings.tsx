import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { ApiError, api } from "../../lib/api";
import {
  formatCountdown,
  groupCode,
  groupPeerId,
  inviteCardKind,
  pairingCardKind,
  peerRowState,
  pollInterval,
  reachableLabel,
  settledMessage,
  trackPairings,
  INVITE_EXPIRED,
  INVITE_GONE,
  type CreatedInvite,
  type FederationPeer,
  type FederationState,
  type TrackedPairing,
} from "../../lib/federation";
import { nowSeconds } from "../../lib/shareFormat";
import { Loader } from "../hud/Loader";
import { Led, Readout, Stamp, type LedTone } from "../hud/Meters";
import { Callsign, Panel, type PanelTone } from "../hud/Panel";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import btn from "../ui/actionButton.module.css";
import styles from "./Peers.module.css";
import { qrShape } from "./qr";
import { PeerShares } from "./PeerShares";
import { useRemoteShares } from "../../app/remoteShares";

const NO_ADDRESS =
  "This instance has no address another instance can reach. Set a public address for it to create invites. You can still accept an invite from an instance that has one.";
const NOT_OWNER =
  "Another BattleLab process on this machine is running federation. Pairing happens there. You can still revoke a peer here.";
const COMPARE =
  "The other instance shows this code too. Confirm only if both screens show the same digits.";
const NOTHING_YET = "Nothing is stored until both operators confirm the same code.";

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

function PeerId({ id }: { id: string }) {
  return (
    <span className={styles.peerId} data-testid="peer-id">
      {groupPeerId(id).map((g, i) => (
        <span key={i}>{g}</span>
      ))}
    </span>
  );
}

function Notice({
  tone,
  led,
  head,
  role = "note",
  id,
  testId,
  children,
}: {
  tone?: "warn" | "down" | "ok";
  led: LedTone;
  head: string;
  role?: "note" | "status" | "alert";
  id?: string;
  testId?: string;
  children: ReactNode;
}) {
  const toneCls =
    tone === "warn"
      ? styles.noticeWarn
      : tone === "down"
        ? styles.noticeDown
        : tone === "ok"
          ? styles.noticeOk
          : "";
  return (
    <div className={`${styles.notice} ${toneCls}`.trim()} role={role} id={id} data-testid={testId}>
      <span className={styles.noticeHead}>
        <Led tone={led} />
        {head}
      </span>
      {children}
    </div>
  );
}

/** One card of the page: the quiet armour panel under a callsign heading. `arrive` moves focus to
 *  the heading when the card mounts, for the flow cards that replace the control that had focus
 *  (never to a button: the operator must choose **They match**, not find it under their thumb). */
function Card({
  title,
  value,
  tone = "quiet",
  arrive = false,
  testId,
  children,
}: {
  title: string;
  value?: ReactNode;
  tone?: PanelTone;
  arrive?: boolean;
  testId?: string;
  children: ReactNode;
}) {
  const id = useId();
  const head = useRef<HTMLDivElement>(null);
  // On mount only (`arrive` never changes for a card): a card that changes state in place keeps
  // the focus where the operator put it.
  useEffect(() => {
    if (arrive) head.current?.focus();
  }, [arrive]);
  return (
    <Panel tone={tone} className={styles.card} aria-labelledby={id} data-testid={testId}>
      <div className={styles.pad}>
        <div className={styles.head} ref={head} tabIndex={arrive ? -1 : undefined}>
          <Callsign as="h2" id={id} title={title} value={value} className={styles.callsign} />
        </div>
        {children}
      </div>
    </Panel>
  );
}

function InviteQr({ text }: { text: string }) {
  const shape = useMemo(() => qrShape(text), [text]);
  if (!shape) return null;
  return (
    <svg
      className={styles.qr}
      viewBox={`0 0 ${shape.size} ${shape.size}`}
      role="img"
      aria-label="The invite as a QR code"
      shapeRendering="crispEdges"
      data-testid="invite-qr"
    >
      <rect className={styles.qrGround} width={shape.size} height={shape.size} />
      <path d={shape.path} />
    </svg>
  );
}

/** Settings → Peers (#1246 P2, #2052): pair this BattleLab with another one, see the instances it
 *  is paired with, and rename or revoke them. The UI of `GET /api/federation` and its writes
 *  (docs/invariants/federation.md).
 *
 *  - **One read.** Everything on the page is that read, re-read every 2 s while an invite or a
 *    pairing is open (the OTHER operator's action changes it), every 15 s otherwise, and not at all
 *    while the page is hidden.
 *  - **The invite is a short-lived secret.** It lives in this component's state only: never in
 *    storage, never in a URL, never logged. Leaving the page drops it.
 *  - **The pairing card is the server's state**, never a guess: an open pairing that a read no
 *    longer lists is shown as no longer open, with what the peer list says about its instance.
 *  - **What it says about this instance** is what the read carries (name, peer id, how it is
 *    reachable). It never derives an address from the browser's own location. */
export function PeersSettings() {
  const [data, setData] = useState<FederationState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [invite, setInvite] = useState<CreatedInvite | null>(null);
  const [inviteEnd, setInviteEnd] = useState<"expired" | "gone" | null>(null);
  const [tracked, setTracked] = useState<TrackedPairing[]>([]);
  const [paired, setPaired] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [paste, setPaste] = useState("");
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const [renaming, setRenaming] = useState<{ peerId: string; value: string } | null>(null);
  const [revoking, setRevoking] = useState<{
    peer: FederationPeer;
    verb: "Revoke" | "Remove";
    from: HTMLElement | null;
  } | null>(null);
  const [now, setNow] = useState(nowSeconds);
  // What peers share with this instance (#2055): the same read the session list shows.
  const remote = useRemoteShares();

  // The same values as refs, for the read path: it must fold a read into the LATEST followed
  // pairings and invite without being re-created (and so restarting the poll) on every change.
  const trackedRef = useRef<TrackedPairing[]>([]);
  const inviteRef = useRef<CreatedInvite | null>(null);
  const alive = useRef(true);
  // Bumped by every finished write. A read that started before it is dropped: it may not list the
  // invite or pairing the write just made, and would show them as gone.
  const epoch = useRef(0);
  const focusAfter = useRef<string | null>(null);
  const inviteCode = useRef<HTMLElement>(null);

  const follow = useCallback((next: TrackedPairing[]) => {
    trackedRef.current = next;
    setTracked(next);
  }, []);
  const holdInvite = useCallback((next: CreatedInvite | null) => {
    inviteRef.current = next;
    setInvite(next);
    setCopied(null);
  }, []);
  const setError = useCallback((key: string, text: string | null) => {
    setErrors((prev) => {
      const { [key]: _old, ...rest } = prev;
      void _old;
      return text ? { ...rest, [key]: text } : rest;
    });
  }, []);

  const apply = useCallback(
    (state: FederationState) => {
      const nowS = nowSeconds();
      let next = trackPairings(trackedRef.current, state.pairings, state.peers);
      const held = inviteRef.current;
      if (held) {
        const kind = inviteCardKind(held, state, nowS);
        if (kind === "taken") {
          // The other instance used it: the pairing carries the invite's id. Follow it whatever
          // state it is already in.
          const taken = state.pairings.find((p) => p.id === held.id);
          if (taken && !next.some((p) => p.id === taken.id)) next = [...next, taken];
          holdInvite(null);
        } else if (kind !== "open") {
          holdInvite(null);
          setInviteEnd(kind);
        }
      }
      const done = next.filter((p) => p.state === "done" && !p.gone);
      if (done.length) {
        // Stored on both sides: the peer is in the list now, and the card has nothing left to say.
        setPaired(done[done.length - 1].name);
        next = next.filter((p) => !(p.state === "done" && !p.gone));
      }
      follow(next);
      setData(state);
      setNow(nowS);
      setLoadError(null);
    },
    [follow, holdInvite],
  );

  const reload = useCallback(async () => {
    const at = epoch.current;
    try {
      const state = await api.federation();
      if (alive.current && at === epoch.current) apply(state);
    } catch (e) {
      if (alive.current && at === epoch.current) setLoadError(message(e, "Couldn’t load the peers."));
    }
  }, [apply]);

  useEffect(() => {
    alive.current = true;
    void reload();
    return () => {
      alive.current = false;
    };
  }, [reload]);

  // The poll. Fast only while the other operator can change something; silent while hidden; one
  // immediate read when the page comes back.
  const interval = pollInterval(invite !== null, tracked);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;
    const arm = () => {
      timer = setTimeout(async () => {
        if (!document.hidden) await reload();
        if (!stopped) arm();
      }, interval);
    };
    arm();
    const onVisible = () => {
      if (!document.hidden) void reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [interval, reload]);

  // The invite's countdown, once a second while one is shown. At zero the card says so at once;
  // the server drops the invite by itself.
  useEffect(() => {
    if (!invite) return;
    const tick = setInterval(() => {
      const nowS = nowSeconds();
      setNow(nowS);
      if (nowS >= invite.expires_at) {
        holdInvite(null);
        setInviteEnd("expired");
      }
    }, 1000);
    return () => clearInterval(tick);
  }, [invite, holdInvite]);

  // Focus returns to a row's own action when its inline rename closes.
  useEffect(() => {
    const key = focusAfter.current;
    if (!key || renaming) return;
    focusAfter.current = null;
    document.querySelector<HTMLElement>(`[data-peer-rename="${key}"]`)?.focus();
  }, [renaming]);

  /** Run one write. Its failure is shown beside the control that caused it (`errorKey`), in the
   *  server's own words; either way the page re-reads, so a refusal that changes what this process
   *  may do (`409 not_owner`) turns the panel read-only on that read. */
  async function write<T>(
    key: string,
    errorKey: string,
    fallback: string,
    run: () => Promise<T>,
    onOk: (result: T) => void,
  ): Promise<boolean> {
    setBusy(key);
    setError(errorKey, null);
    let ok = false;
    try {
      const result = await run();
      epoch.current++;
      if (alive.current) onOk(result);
      ok = true;
    } catch (e) {
      epoch.current++;
      if (alive.current) setError(errorKey, message(e, fallback));
    }
    if (alive.current) {
      setBusy(null);
      await reload();
    }
    return ok;
  }

  const createInvite = () =>
    write("create", "create", "Couldn’t create an invite.", api.createFederationInvite, (made) => {
      holdInvite(made);
      setInviteEnd(null);
      setPaired(null);
      setNow(nowSeconds());
    });

  const withdrawInvite = (held: CreatedInvite) =>
    write(
      "withdraw",
      "invite",
      "Couldn’t withdraw the invite.",
      () => api.withdrawFederationInvite(held.id),
      (state) => {
        holdInvite(null);
        apply(state);
      },
    );

  const acceptInvite = (e: FormEvent) => {
    e.preventDefault();
    const text = paste.trim();
    if (!text) return;
    void write(
      "accept",
      "accept",
      "Couldn’t start pairing.",
      () => api.acceptFederationInvite(text),
      (pairing) => {
        follow([...trackedRef.current.filter((p) => p.id !== pairing.id), pairing]);
        setPaste("");
        setInviteEnd(null);
        setPaired(null);
      },
    );
  };

  const confirmPairing = (id: string) =>
    write(
      `confirm:${id}`,
      `pairing:${id}`,
      "Couldn’t confirm the pairing.",
      () => api.confirmFederationPairing(id),
      (pairing) => follow(trackedRef.current.map((p) => (p.id === id ? pairing : p))),
    );

  const dismissPairing = useCallback(
    (id: string) => {
      follow(trackedRef.current.filter((p) => p.id !== id));
      setError(`pairing:${id}`, null);
    },
    [follow, setError],
  );

  const denyPairing = (id: string) =>
    write(
      `deny:${id}`,
      `pairing:${id}`,
      "Couldn’t cancel the pairing.",
      () => api.denyFederationPairing(id),
      // Cancelled here, by this operator: there is nothing to tell them, so the card just leaves.
      () => dismissPairing(id),
    );

  const saveRename = (e: FormEvent) => {
    e.preventDefault();
    if (!renaming) return;
    const { peerId, value } = renaming;
    void write(
      `rename:${peerId}`,
      `rename:${peerId}`,
      "Couldn’t rename that peer.",
      () => api.renameFederationPeer(peerId, value.trim()),
      (state) => {
        focusAfter.current = peerId;
        setRenaming(null);
        apply(state);
      },
    );
  };
  const cancelRename = () => {
    if (!renaming) return;
    setError(`rename:${renaming.peerId}`, null);
    focusAfter.current = renaming.peerId;
    setRenaming(null);
  };

  const revoke = (peer: FederationPeer) =>
    write(
      `revoke:${peer.peer_id}`,
      "revoke",
      "Couldn’t revoke that peer.",
      () => api.revokeFederationPeer(peer.peer_id),
      (state) => {
        setRevoking(null);
        apply(state);
      },
    );

  const copyInvite = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied("ok");
    } catch {
      // No clipboard here (an insecure origin, a denied permission): select the string instead.
      const el = inviteCode.current;
      if (el) window.getSelection()?.selectAllChildren(el);
      setCopied("failed");
    }
  };

  if (!data) {
    return (
      <Card title="Peers" testId="peers-settings">
        {loadError ? (
          <p className="nj-error" role="alert">
            {loadError}
          </p>
        ) : (
          <Loader kind="skeleton" label="Loading the peers" />
        )}
      </Card>
    );
  }

  const owner = data.owner;
  const reachable = reachableLabel(data.self.reachable_by);
  const noAddress = owner && !reachable;
  const flowOpen = invite !== null || inviteEnd !== null || tracked.length > 0;
  const startAgain = () => {
    setInviteEnd(null);
    setError("invite", null);
  };

  return (
    <div data-testid="peers-settings" data-owner={owner ? "yes" : "no"}>
      {loadError && (
        <p className="nj-error" role="alert">
          {loadError}
        </p>
      )}

      <Card title="This instance" value={data.self.name} testId="peers-self">
        <dl className={styles.kv}>
          <dt>Name</dt>
          <dd>{data.self.name}</dd>
          <dt>Peer id</dt>
          <dd>
            <PeerId id={data.self.peer_id} />
          </dd>
          <dt>Reachable</dt>
          <dd data-testid="peers-reachable">
            <span className={styles.state}>
              <Led tone={reachable ? "up" : "att"} />
              {reachable ?? <span className={styles.wordWarn}>no address</span>}
            </span>
          </dd>
        </dl>
      </Card>

      <Card title="Paired instances" value={data.peers.length} testId="peers-list">
        {!owner && (
          <Notice led="idle" head="Pairing elsewhere" testId="peers-not-owner">
            {NOT_OWNER}
          </Notice>
        )}
        {paired !== null && (
          <Notice tone="ok" led="up" head="Paired" role="status" testId="peers-paired">
            {paired ? `${paired} is now a paired instance.` : "The other instance is now paired."}{" "}
            Pairing shares nothing by itself.
          </Notice>
        )}
        {data.peers.length === 0 ? (
          <div className={styles.empty}>
            <b>No paired instances yet.</b>
            <p className={styles.rowMeta}>
              Create an invite here, or accept one from another instance. Pairing shares nothing by
              itself.
            </p>
          </div>
        ) : (
          <>
          <p className={`${styles.rowMeta} ${styles.listLede}`}>
            Each peer lists the sessions shared with it and the ones it shares with this instance.
            Revoking a share ends it at once.
          </p>
          <ul className={styles.rows}>
            {data.peers.map((peer) => {
              const row = peerRowState(peer, owner, now);
              const editing = renaming?.peerId === peer.peer_id;
              const renameError = errors[`rename:${peer.peer_id}`];
              if (editing)
                return (
                  <li
                    key={peer.peer_id}
                    className={`${styles.row} ${styles.rowEditing}`}
                    data-testid="peer-row"
                    data-peer={peer.peer_id}
                    data-state={row.kind}
                  >
                    <form className={styles.rowMain} onSubmit={saveRename}>
                      <label className={`nj-label ${styles.fieldLabel}`} htmlFor={`rn-${peer.peer_id}`}>
                        Name on this instance
                      </label>
                      <div className={styles.rename}>
                        <input
                          id={`rn-${peer.peer_id}`}
                          className="nj-field"
                          type="text"
                          value={renaming.value}
                          maxLength={64}
                          autoComplete="off"
                          autoFocus
                          aria-describedby={`rn-hint-${peer.peer_id}`}
                          aria-invalid={renameError ? true : undefined}
                          onChange={(e) => setRenaming({ peerId: peer.peer_id, value: e.target.value })}
                          onKeyDown={(e) => {
                            if (e.key === "Escape") {
                              e.preventDefault();
                              cancelRename();
                            }
                          }}
                        />
                        <button
                          type="submit"
                          className={`${btn.primary} ${btn.compact}`}
                          disabled={busy !== null || !renaming.value.trim()}
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          className={`${btn.ghost} ${btn.compact}`}
                          onClick={cancelRename}
                        >
                          Cancel
                        </button>
                      </div>
                      {renameError && (
                        <p className="nj-error" role="alert">
                          {renameError}
                        </p>
                      )}
                      <PeerId id={peer.peer_id} />
                      <span className={styles.rowMeta} id={`rn-hint-${peer.peer_id}`}>
                        Only a label here. The other instance does not see it.
                      </span>
                    </form>
                  </li>
                );
              return (
                <li
                  key={peer.peer_id}
                  className={`${styles.row} ${styles.rowShares} ${row.kind === "revoked" ? styles.revoked : ""}`.trim()}
                  data-testid="peer-row"
                  data-peer={peer.peer_id}
                  data-state={row.kind}
                >
                  <div className={styles.rowMain}>
                    <div className={styles.rowTop}>
                      <span className={styles.rowTitle}>{peer.name}</span>
                      {row.kind === "revoked" ? (
                        <Stamp>Revoked</Stamp>
                      ) : (
                        <span className={`${styles.state} ${styles.rowMeta}`} data-testid="peer-state">
                          <Led tone={row.led} />
                          <span
                            className={
                              row.kind === "connected"
                                ? styles.wordOk
                                : row.kind === "refused"
                                  ? styles.wordWarn
                                  : undefined
                            }
                          >
                            {row.word}
                          </span>
                        </span>
                      )}
                    </div>
                    <PeerId id={peer.peer_id} />
                    {row.kind === "revoked" && (
                      <span className={styles.rowMeta}>
                        Its key stays refused. Pair again to restore it.
                      </span>
                    )}
                    {row.kind === "refused" && (
                      <span className={styles.rowMeta}>
                        This instance was removed by the other side. It no longer accepts a connection
                        from here.
                      </span>
                    )}
                    {(row.kind === "connected" || row.kind === "offline") &&
                      peer.reachable_by.length > 0 && (
                        <span className={styles.rowMeta}>
                          Reachable by: {peer.reachable_by.join(", ")}
                        </span>
                      )}
                  </div>
                  {row.revokeLabel && (
                    <div className={styles.rowActions} data-testid="peer-actions">
                      {row.canRename && (
                        <button
                          type="button"
                          className={`${btn.ghost} ${btn.compact}`}
                          data-peer-rename={peer.peer_id}
                          aria-label={`Rename ${peer.name}`}
                          onClick={() => setRenaming({ peerId: peer.peer_id, value: peer.name })}
                        >
                          Rename
                        </button>
                      )}
                      <button
                        type="button"
                        className={`${btn.danger} ${btn.compact}`}
                        aria-label={`${row.revokeLabel} ${peer.name}`}
                        onClick={(e) => {
                          setError("revoke", null);
                          setRevoking({ peer, verb: row.revokeLabel!, from: e.currentTarget });
                        }}
                      >
                        {row.revokeLabel}
                      </button>
                    </div>
                  )}
                  {/* #2055: what is shared with this peer and by it. A revoked peer has no
                      shares in either direction (its revoke ended them). */}
                  {peer.state === "trusted" && (
                    <PeerShares peerId={peer.peer_id} peerName={peer.name} remote={remote} />
                  )}
                </li>
              );
            })}
          </ul>
          </>
        )}
      </Card>

      {invite && (
        <Card title="Invite" value="one-time" arrive testId="peers-invite">
          <div className={styles.flow}>
            <div className={styles.invite}>
              <code ref={inviteCode} data-testid="invite-string">
                {invite.invite}
              </code>
              <button
                type="button"
                className={`${btn.ghost} ${btn.compact}`}
                onClick={() => void copyInvite(invite.invite)}
              >
                {copied === "ok" ? "Copied" : "Copy"}
              </button>
            </div>
            {copied === "failed" && (
              <p className="nj-hint" role="status">
                Couldn’t reach the clipboard. The invite is selected: copy it by hand.
              </p>
            )}
            <InviteQr text={invite.invite} />
            <p className={`${styles.state} ${styles.countdown}`} data-testid="invite-countdown">
              <Led />
              <span>
                Valid for <b>{formatCountdown(invite.expires_at, now)}</b> · works once
              </span>
            </p>
            <p className={styles.lede}>Send it to the other operator through a channel you trust.</p>
            {errors.invite && (
              <p className="nj-error" role="alert">
                {errors.invite}
              </p>
            )}
            <div className={styles.actions}>
              <button
                type="button"
                className={btn.ghost}
                disabled={busy !== null}
                onClick={() => void withdrawInvite(invite)}
              >
                {busy === "withdraw" ? "Withdrawing…" : "Withdraw"}
              </button>
            </div>
          </div>
        </Card>
      )}

      {inviteEnd && (
        <Card title="Invite" arrive testId="peers-invite-ended">
          <div className={styles.flow}>
            <Notice led="idle" head={inviteEnd === "expired" ? "Expired" : "No longer open"} role="status">
              {inviteEnd === "expired"
                ? INVITE_EXPIRED
                : INVITE_GONE}
            </Notice>
            {errors.invite && (
              <p className="nj-error" role="alert">
                {errors.invite}
              </p>
            )}
            <div className={styles.actions}>
              <button type="button" className={btn.ghost} onClick={startAgain}>
                {owner ? "Start again" : "Dismiss"}
              </button>
            </div>
          </div>
        </Card>
      )}

      {tracked.map((p) => {
        const kind = pairingCardKind(p);
        const error = errors[`pairing:${p.id}`];
        const acting = busy === `confirm:${p.id}` || busy === `deny:${p.id}`;
        if (kind === "dialing")
          return (
            <Card key={p.id} title="Accept an invite" arrive testId="peers-pairing">
              <div className={styles.flow} data-state={kind}>
                <Loader kind="acquire" label="Contacting the other instance…" />
                <p className={styles.lede}>{NOTHING_YET}</p>
                {error && (
                  <p className="nj-error" role="alert">
                    {error}
                  </p>
                )}
                <div className={styles.actions}>
                  <button
                    type="button"
                    className={btn.ghost}
                    disabled={acting}
                    onClick={() => void denyPairing(p.id)}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </Card>
          );
        if (kind === "confirm" || kind === "waiting") {
          const groups = groupCode(p.code);
          return (
            <Card
              key={p.id}
              title="Compare the code"
              tone={kind === "confirm" ? "hero" : "quiet"}
              arrive
              testId="peers-pairing"
            >
              <div className={styles.flow} data-state={kind}>
                <div className={styles.codeWrap}>
                  <Readout
                    className={styles.code}
                    testId="pairing-code"
                    value={
                      <>
                        <span className="nj-sr-only">Pairing code </span>
                        {groups.map((g, i) => (
                          <span key={i}>{g}</span>
                        ))}
                      </>
                    }
                  />
                </div>
                <p className={`${styles.lede} ${styles.strong}`}>{COMPARE}</p>
                <dl className={`${styles.kv} ${styles.kvStacked}`}>
                  <dt>Suggested name</dt>
                  <dd data-testid="pairing-name">{p.name || "not sent yet"}</dd>
                  <dt>Peer id</dt>
                  <dd>
                    <PeerId id={p.peer_id} />
                  </dd>
                </dl>
                {kind === "waiting" && (
                  <div className={styles.confirmed} role="status" data-testid="pairing-waiting">
                    <span className={styles.state}>
                      <Led tone="up" />
                      <span>
                        <b className={styles.wordOk}>Confirmed here</b> · waiting for the other
                        operator
                      </span>
                    </span>
                    <Loader kind="steps" label="Nothing is stored yet" />
                  </div>
                )}
                {error && (
                  <p className="nj-error" role="alert">
                    {error}
                  </p>
                )}
                <div className={styles.actions}>
                  {kind === "confirm" && (
                    <button
                      type="button"
                      className={btn.primary}
                      disabled={acting}
                      onClick={() => void confirmPairing(p.id)}
                    >
                      They match
                    </button>
                  )}
                  <button
                    type="button"
                    className={btn.danger}
                    disabled={acting}
                    onClick={() => void denyPairing(p.id)}
                  >
                    Cancel pairing
                  </button>
                </div>
              </div>
            </Card>
          );
        }
        // Settled without a peer: failed (red, the one failure), or quietly over.
        const failed = kind === "failed";
        return (
          <Card key={p.id} title="Pairing" arrive testId="peers-pairing">
            <div className={styles.flow} data-state={kind}>
              <Notice
                tone={failed ? "down" : undefined}
                led={failed ? "down" : "idle"}
                head={
                  failed
                    ? "Pairing failed"
                    : kind === "expired"
                      ? "Expired"
                      : kind === "denied"
                        ? "Pairing cancelled"
                        : "No longer open"
                }
                role={failed ? "alert" : "status"}
                testId="pairing-settled"
              >
                {settledMessage(p)}
              </Notice>
              {kind === "gone" && error && (
                <p className="nj-error" role="alert">
                  {error}
                </p>
              )}
              <div className={styles.actions}>
                <button type="button" className={btn.ghost} onClick={() => dismissPairing(p.id)}>
                  {!owner ? "Dismiss" : failed ? "Try again" : kind === "gone" ? "Start again" : "Pair again"}
                </button>
              </div>
            </div>
          </Card>
        );
      })}

      {(!flowOpen || !owner) && (
        <Card title="Pair with another instance" testId="peers-pair">
          <p className={styles.lede}>
            Pairing lets two BattleLab instances recognise each other. It shares nothing by itself.
          </p>
          {noAddress && (
            <Notice tone="warn" led="att" head="No address" id="peers-no-address" testId="peers-no-address">
              {NO_ADDRESS}
            </Notice>
          )}
          <div className={styles.paths}>
            <div className={styles.path}>
              <h3>Create invite</h3>
              <p className={`nj-hint ${styles.pathHint}`}>
                Make a one-time invite and send it to the other operator. Valid for 10 minutes, works
                once.
              </p>
              {errors.create && (
                <p className="nj-error" role="alert">
                  {errors.create}
                </p>
              )}
              <button
                type="button"
                className={btn.primary}
                disabled={!owner || noAddress || busy !== null}
                aria-describedby={noAddress ? "peers-no-address" : undefined}
                onClick={() => void createInvite()}
              >
                {busy === "create" ? "Creating…" : "Create invite"}
              </button>
            </div>
            <div className={styles.path}>
              <h3>Accept an invite</h3>
              <p className={`nj-hint ${styles.pathHint}`}>
                Paste the invite the other operator sent you.
              </p>
              <form className={styles.acceptForm} onSubmit={acceptInvite}>
                <label className={`nj-label ${styles.fieldLabel}`} htmlFor="peers-invite-in">
                  Invite
                </label>
                <div className={styles.fieldRow}>
                  <input
                    id="peers-invite-in"
                    className="nj-field is-mono"
                    type="text"
                    placeholder="blfed1:…"
                    value={paste}
                    disabled={!owner}
                    autoComplete="off"
                    autoCapitalize="off"
                    autoCorrect="off"
                    spellCheck={false}
                    aria-invalid={errors.accept ? true : undefined}
                    aria-describedby={errors.accept ? "peers-accept-error" : undefined}
                    onChange={(e) => setPaste(e.target.value)}
                  />
                  <button
                    type="submit"
                    className={btn.primary}
                    disabled={!owner || busy !== null || !paste.trim()}
                  >
                    {busy === "accept" ? "Pairing…" : "Pair"}
                  </button>
                </div>
                {errors.accept && (
                  <p className="nj-error" role="alert" id="peers-accept-error">
                    {errors.accept}
                  </p>
                )}
              </form>
            </div>
          </div>
        </Card>
      )}

      {revoking && (
        <ConfirmDialog
          tag={`${revoking.verb.toUpperCase()} PEER`}
          title={`${revoking.verb} ${revoking.peer.name}?`}
          confirmLabel={revoking.verb}
          danger
          role="alertdialog"
          busy={busy === `revoke:${revoking.peer.peer_id}`}
          returnFocusTo={revoking.from}
          markers={{ dialog: { "data-testid": "peers-revoke-dialog" } }}
          onCancel={() => setRevoking(null)}
          onConfirm={() => void revoke(revoking.peer)}
        >
          <p>
            It can no longer connect to this instance, and everything shared with it ends. You can
            pair again later.
          </p>
          {errors.revoke && (
            <p className="nj-error" role="alert">
              {errors.revoke}
            </p>
          )}
        </ConfirmDialog>
      )}
    </div>
  );
}
