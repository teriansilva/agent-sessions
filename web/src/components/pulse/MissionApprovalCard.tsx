/** PLAN APPROVAL (#1951, #1613 P6 D3; mockups `docs/mockups/plan-approval-1951/`).
 *
 * Approving the enumerated plan is the tap. The card shows what an approval of the latest plan
 * revision would pin, in the server's own words (each step's specialist, effective model and grade,
 * the equipment, the allowed actions, the bounds and what is NOT covered), and one **Approve plan**
 * button that sends the revision and digest the card showed. Once approved, the card shows the
 * approval re-checked on this read: in force, or stale with each change old → new.
 *
 * Standing consent (inert until an automation uses it) is given here too, on the server's scope
 * lines, with an expiry of at most 30 days, and revoked here.
 *
 * **The client decides nothing.** Every line, state and reason is server text from
 * `GET …/plan/approval`; the server re-checks every pin on approve and refuses a stale digest.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import { settingsPath } from "../../routes/settingsTabs";
import type {
  ApprovalLines,
  ApprovalReason,
  MissionApproval,
  StandingConsent,
} from "../../types/missionApproval";
import { Disclosure } from "../hud/Controls";
import action from "../ui/actionButton.module.css";
import styles from "./missionApproval.module.css";

const PLANNING = new Set(["draft", "planned"]);
const GRADE_CODES = new Set(["grade_below_min", "ungraded"]);
/** Codes the Team card above can resolve (a specialist, its model or its grade). */
const TEAM_CODES = new Set([
  "specialist_version_changed",
  "specialist_archived",
  "model_changed",
  "model_unavailable",
  "grade_below_min",
  "ungraded",
  "skill_pin_stale",
  "unassigned",
]);

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function short(digest: string): string {
  return `${digest.slice(0, 19)}…${digest.slice(-4)}`;
}

function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const body = (e.record ?? {}) as { detail?: string };
    return body.detail || e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

function oldNew(r: ApprovalReason) {
  const show = (v: unknown) =>
    typeof v === "string" || typeof v === "number" ? String(v) : null;
  const o = show(r.old);
  const n = show(r.new);
  if (o === null || n === null || GRADE_CODES.has(r.code)) return null;
  return (
    <p className={styles.chg}>
      <s>{o}</s>
      <span className={styles.arrow} aria-label="changed to">
        →
      </span>
      {n}
    </p>
  );
}

function Reasons({ reasons }: { reasons: ApprovalReason[] }) {
  return (
    <ul className={styles.pins}>
      {reasons.map((r, i) => (
        <li key={`${r.code}-${r.step ?? ""}-${i}`} className={`${styles.pin} ${styles.bad}`}>
          <span className={styles.k}>
            {r.step ?? (r.code === "equipment_changed" ? "equipment" : "plan")}
          </span>
          <span className={styles.v}>{r.text}</span>
          <span className={styles.code}>{r.code}</span>
          {oldNew(r)}
        </li>
      ))}
    </ul>
  );
}

function Lines({ lines }: { lines: ApprovalLines }) {
  return (
    <>
      {lines.groups.map((g) => (
        <div key={g.objective} className={styles.sect}>
          <p className={styles.sh}>
            Objective {g.objective} · {g.project}
          </p>
          <ul className={styles.pins}>
            {g.rows.map((r, i) => (
              <li key={`${r.key}-${i}`} className={styles.pin}>
                <span className={styles.k}>{r.key}</span>
                <span className={styles.v}>{r.text}</span>
                <span className={styles.b}>{r.bound ?? ""}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
      <div className={styles.sect}>
        <p className={styles.sh}>Bounds</p>
        <p className={styles.tsum}>{lines.bounds}</p>
      </div>
      <p className={styles.cover} data-testid="approval-not-covered">
        <b>Not covered.</b> {lines.not_covered}
      </p>
    </>
  );
}

function ConsentState({
  consent,
  busy,
  onRevoke,
}: {
  consent: StandingConsent;
  busy: boolean;
  onRevoke: () => void;
}) {
  const label = {
    active: "ACTIVE",
    expired: "EXPIRED",
    revoked: "REVOKED",
    void: "VOID",
    absent: "NONE",
  }[consent.state];
  const text =
    consent.state === "active"
      ? `Expires ${when(consent.expires_at)}. An automation may launch within its lines.`
      : consent.state === "expired"
        ? `Expired ${when(consent.expires_at)}. A launch from it is refused.`
        : consent.state === "revoked"
          ? `Revoked ${consent.revoked_at ? when(consent.revoked_at) : ""}. A launch from it is refused.`
          : "The plan changed since you consented. Give consent again to use it.";
  return (
    <div className={styles.sect} data-testid="consent-state">
      <div className={styles.thead}>
        <span className={styles.ttl}>Standing consent</span>
        <span
          className={`${styles.stamp} ${consent.state === "active" ? styles.live : styles.warnStamp}`}
        >
          {label}
        </span>
      </div>
      <p className={styles.tsum}>{text}</p>
      {consent.state !== "active" && consent.reasons.length ? (
        <Reasons reasons={consent.reasons} />
      ) : null}
      {consent.state === "active" ? (
        <div className={styles.foot}>
          <span className={styles.digest}>scope {short(consent.scope_digest)}</span>
          <button
            type="button"
            className={`${action.ghost} ${styles.btn}`}
            disabled={busy}
            onClick={onRevoke}
          >
            Revoke consent
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function MissionApprovalCard({
  missionId,
  state,
  refresh = 0,
}: {
  missionId: string;
  state: string;
  /** Bumped by the parent after a team write: the card re-reads. */
  refresh?: number;
}) {
  const [data, setData] = useState<MissionApproval | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [consenting, setConsenting] = useState(false);
  // The operator's expiry choice; null until they pick one (then a re-read never resets it).
  const [days, setDays] = useState<number | null>(null);
  const [reads, setReads] = useState(0);

  useEffect(() => {
    let alive = true;
    api
      .missionApproval(missionId)
      .then((r) => {
        if (!alive) return;
        setData(r);
        setLoadError("");
      })
      .catch((e: unknown) => alive && setLoadError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [missionId, reads, refresh]);

  const act = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      setConsenting(false);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      setReads((n) => n + 1);
    }
  }, []);

  if (loadError) {
    return (
      <section className={styles.appr} aria-label="Plan approval">
        <p className={styles.alert} role="alert">
          The plan approval could not be read: {loadError}
        </p>
      </section>
    );
  }
  if (!data || data.revision === null || !data.card) return null;
  const card = data.card;
  const approval = data.approval;
  const planning = PLANNING.has(state);
  const live = approval && approval.revoked_at === null ? approval : null;
  const current = live && live.revision === data.revision;
  const showOffer = planning && !(current && live.state === "in_force");
  const offer = data.consent_offer;
  const max = data.consent_days?.max ?? 30;
  const grade = [...(live?.reasons ?? []), ...card.reasons].some((r) => GRADE_CODES.has(r.code));
  const chosenDays = days ?? data.consent_days?.default ?? 7;
  const last = approval && approval.revoked_at !== null ? approval : null;

  return (
    <section className={styles.appr} aria-label="Plan approval" data-testid="mission-approval">
      <div className={styles.thead}>
        <span className={styles.ttl}>Plan approval</span>
        {live ? (
          <span
            className={`${styles.stamp} ${live.state === "in_force" ? styles.live : styles.warnStamp}`}
            data-testid="approval-state"
          >
            {live.state === "in_force" ? "APPROVED" : "STALE"}
          </span>
        ) : null}
        <span className={styles.rev}>
          revision {live ? live.revision : data.revision} ·{" "}
          {live ? `approved ${when(live.created_at)}` : "not approved"}
        </span>
      </div>
      {last ? (
        <p className={styles.tsum} data-testid="approval-last">
          Your last approval (revision {last.revision}) was{" "}
          {last.state === "superseded" ? "replaced by a newer one" : "revoked"}
          {last.revoked_at ? ` ${when(last.revoked_at)}` : ""}. Nothing launches from it.
        </p>
      ) : null}

      {error ? (
        <p className={styles.alert} role="alert" data-testid="approval-error">
          <b>REFUSED</b>
          {error}
        </p>
      ) : null}

      {live && live.state === "in_force" ? (
        <>
          <p className={styles.okline}>
            <b>IN FORCE</b>All {live.agent_steps} agent steps still match what you approved.
            Checked on this read.
          </p>
          <p className={styles.tsum}>
            HQ may launch exactly what is listed. Each launch and each gate checks it again first; a
            change refuses that launch and asks you. A session already running keeps running; Stop
            is always yours.
          </p>
          {live.groups ? (
            <Disclosure
              className={styles.alt}
              summary={<span className={styles.altSummary}>What you approved</span>}
            >
              <Lines lines={live as ApprovalLines} />
            </Disclosure>
          ) : null}
        </>
      ) : null}

      {live && live.state === "stale" ? (
        <div data-testid="approval-stale" className={styles.sect}>
          <p className={styles.alert}>
            <b>{grade ? "BELOW GRADE" : "PLAN CHANGED"}</b>
            {grade
              ? "A model's grade fell after you approved. Launches of these steps are refused until it qualifies again."
              : "Plan changed since you approved it: review it again. New launches and gates are refused until you do; nothing is remapped."}
          </p>
          <Reasons reasons={live.reasons} />
          {grade ? (
            <p className={styles.ann}>
              Grade the model again in Settings → Agents and the same approval launches: grades
              are checked live, never stored on the approval.
            </p>
          ) : null}
        </div>
      ) : null}

      {showOffer ? (
        card.digest !== null && "groups" in card ? (
          <>
            <p className={styles.tsum}>
              Approving is the tap. HQ may then launch exactly these specialists on exactly these
              steps, at revision {data.revision}, and nothing else. Every launch and every gate
              checks this list again; any change asks you again.
            </p>
            <Lines lines={card} />
            {!card.ok ? (
              <>
                <p className={styles.tsum} data-testid="approval-blocked">
                  {card.reasons.some((r) => TEAM_CODES.has(r.code))
                    ? "It can't be approved as it stands: change the team above (or re-grade the model in Settings → Agents), then approve the new revision."
                    : "It can't be approved as it stands. Each line below says what changed."}
                </p>
                <Reasons reasons={card.reasons} />
              </>
            ) : null}
            <div className={styles.foot}>
              <span className={styles.digest}>digest {short(card.digest)}</span>
              {grade ? (
                <Link className={`${action.ghost} ${styles.btn}`} to={settingsPath("agents")}>
                  Settings → Agents
                </Link>
              ) : null}
              <button
                type="button"
                className={`${action.primary} ${styles.btn}`}
                disabled={busy || !card.ok}
                onClick={() =>
                  void act(() =>
                    api.approvePlan(missionId, {
                      revision: data.revision as number,
                      digest: card.digest,
                      operation_id: crypto.randomUUID(),
                    }),
                  )
                }
              >
                {live ? "Approve again" : "Approve plan"}
              </button>
            </div>
          </>
        ) : (
          <div className={styles.sect} data-testid="approval-blocked">
            <p className={styles.tsum}>This plan can't be approved yet:</p>
            <Reasons reasons={card.reasons} />
          </div>
        )
      ) : null}

      {live ? (
        <div className={styles.foot}>
          <span className={styles.digest}>approved {short(live.digest)}</span>
          {offer && !consenting && data.consent?.state !== "active" ? (
            <button
              type="button"
              className={`${action.ghost} ${styles.btn}`}
              disabled={busy}
              onClick={() => setConsenting(true)}
            >
              Standing consent
            </button>
          ) : null}
          <button
            type="button"
            className={`${action.ghost} ${styles.btn}`}
            disabled={busy}
            onClick={() => void act(() => api.revokeApproval(missionId, live.id))}
          >
            Revoke approval
          </button>
        </div>
      ) : null}

      {consenting && offer ? (
        <div className={styles.sect} data-testid="consent-offer">
          <div className={styles.thead}>
            <span className={styles.ttl}>Standing consent</span>
            <span className={styles.rev}>for automations · inert until one uses it</span>
          </div>
          <p className={styles.tsum}>
            An automation may start this mission from your approval without asking you each time,
            only within these lines:
          </p>
          <ol className={styles.scope}>
            {offer.lines.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ol>
          <div className={styles.field}>
            <label htmlFor={`consent-days-${missionId}`}>Expires after</label>
            <select
              id={`consent-days-${missionId}`}
              className={styles.select}
              value={chosenDays}
              onChange={(e) => setDays(Number(e.target.value))}
            >
              {[1, 3, 7, 14, 30]
                .filter((n) => n <= max)
                .map((n) => (
                  <option key={n} value={n}>
                    {n} day{n === 1 ? "" : "s"}
                  </option>
                ))}
            </select>
            <span className={styles.code}>at most {max} days</span>
          </div>
          <div className={styles.foot}>
            <span className={styles.digest}>scope {short(offer.digest)}</span>
            <button
              type="button"
              className={`${action.ghost} ${styles.btn}`}
              onClick={() => setConsenting(false)}
            >
              Cancel
            </button>
            <button
              type="button"
              className={`${action.primary} ${styles.btn}`}
              disabled={busy}
              onClick={() =>
                void act(() =>
                  api.giveConsent(missionId, {
                    approval_id: offer.approval_id,
                    scope_digest: offer.digest,
                    days: chosenDays,
                    operation_id: crypto.randomUUID(),
                  }),
                )
              }
            >
              Give consent
            </button>
          </div>
        </div>
      ) : null}

      {data.consent ? (
        <ConsentState
          consent={data.consent}
          busy={busy}
          onRevoke={() =>
            void act(() => api.revokeConsent(missionId, (data.consent as StandingConsent).id))
          }
        />
      ) : null}
    </section>
  );
}
