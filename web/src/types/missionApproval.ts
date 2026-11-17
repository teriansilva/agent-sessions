/** Plan approval (#1951, #1613 P6): the card the server builds on every read. Every line, state
 *  and reason is server text; the client renders it and recomputes nothing. */

export type ApprovalReason = {
  code: string;
  text: string;
  step?: string;
  old?: unknown;
  new?: unknown;
};

/** One step of a rework edge's span (#2033): the target and every step after it, listed before
 *  the tap, each with where it sits relative to the edge and its bound (server text). */
export type ApprovalSpanStep = { step: string; text: string; role: string; bound: string };

export type ApprovalRow = { key: string; text: string; bound?: string; span?: ApprovalSpanStep[] };

export type ApprovalLines = {
  groups: { objective: string; project: string; rows: ApprovalRow[] }[];
  bounds: string;
  not_covered: string;
  agent_steps: number;
};

export type ApprovalCard =
  | ({ ok: boolean; reasons: ApprovalReason[]; digest: string } & ApprovalLines)
  | { ok: false; code: string; reasons: ApprovalReason[]; digest: null };

export type ApprovalInForce = {
  id: string;
  revision: number;
  digest: string;
  created_at: number;
  revoked_at: number | null;
  revoked_reason: string | null;
  state: "in_force" | "stale" | "revoked" | "superseded";
  reasons: ApprovalReason[];
  agent_steps: number;
} & Partial<ApprovalLines>;

export type StandingConsent = {
  id: string;
  approval_id: string;
  lines: string[];
  scope_digest: string;
  expires_at: number;
  created_at: number;
  revoked_at: number | null;
  state: "active" | "expired" | "revoked" | "void" | "absent";
  reasons: ApprovalReason[];
};

export type ConsentOffer = {
  approval_id: string;
  approval_digest: string;
  lines: string[];
  digest: string;
};

export type MissionApproval = {
  mission_id: string;
  revision: number | null;
  card: ApprovalCard | null;
  approval: ApprovalInForce | null;
  consent: StandingConsent | null;
  consent_offer: ConsentOffer | null;
  consent_days: { default: number; max: number };
};
