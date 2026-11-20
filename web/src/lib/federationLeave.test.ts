import { describe, expect, test } from "vitest";
import { writeFailure } from "./federation";
import {
  LEAVE_CHECK,
  LEAVE_IDLE,
  LEAVE_LABEL,
  leaveDialog,
  leaveFailure,
  leaveKey,
  leaveReduce,
  leaveShown,
  type LeaveState,
} from "./federationLeave";

// Leaving a share (#2201): what each answer proves, the state bound to one revision, the words.

describe("what a Leave's answer is", () => {
  test("only the listed refusals are refusals; everything else is not known", () => {
    expect(leaveFailure(404, "no_grant")).toBe("refused");
    expect(leaveFailure(503, "peer_offline")).toBe("offline");
    // not known: the share may or may not have ended
    for (const [status, code] of [
      [504, "peer_lost"],
      [504, "timeout"],
      [429, "busy"],
      [503, "unavailable"],
      [503, "busy"],
      [503, undefined],
      [502, "bad_answer"],
      [500, "store"],
      [0, undefined], // the network
      [418, "teapot"],
      [404, "something_new"],
      [200, "no_grant"],
    ] as const)
      expect(leaveFailure(status, code), `${status} ${code}`).toBe("unconfirmed");
    // proven refusals of another kind: nothing changed, and the server says why
    for (const [status, code] of [
      [422, "not_possible"],
      [422, "invalid"],
      [401, undefined],
      [403, "csrf"],
      [404, "not_found"],
    ] as const)
      expect(leaveFailure(status, code), `${status} ${code}`).toBe("rejected");
  });

  test("it is the shared table, not a second one", () => {
    for (const status of [0, 200, 401, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504])
      for (const code of [undefined, "no_grant", "peer_offline", "peer_lost", "busy", "timeout", "held", "x"]) {
        const shared = writeFailure(status, code);
        const mine = leaveFailure(status, code);
        if (shared === "unconfirmed" || shared === "refused" || shared === "offline") expect(mine).toBe(shared);
        else expect(mine).toBe("rejected");
      }
  });

  test("an uncertain answer is never a refusal and never success", () => {
    for (const code of ["peer_lost", "timeout"]) {
      expect(leaveFailure(504, code)).not.toBe("refused");
      expect(leaveFailure(504, code)).not.toBe("offline");
      expect(leaveFailure(504, code)).not.toBe("rejected");
    }
    expect(leaveFailure(429, "busy")).toBe("unconfirmed");
  });
});

const share = (revision: number, grant_id = "g1", peer_id = "p1") => ({ peer_id, grant_id, revision });

describe("the dialog's state is for one share at one revision", () => {
  const failed = (failure: LeaveState["phase"], revision = 4): LeaveState =>
    leaveReduce(leaveReduce(LEAVE_IDLE, { type: "sent" }), {
      type: "failed",
      key: leaveKey(share(revision)),
      failure: failure as never,
      message: "that isn't possible for this kind of share",
    });

  test("the key names the peer, the grant and the revision", () => {
    expect(leaveKey(share(4))).not.toBe(leaveKey(share(5)));
    expect(leaveKey(share(4))).not.toBe(leaveKey(share(4, "g2")));
    expect(leaveKey(share(4))).not.toBe(leaveKey(share(4, "g1", "p2")));
    expect(leaveKey(share(4))).toBe(leaveKey({ ...share(4) }));
  });

  test("a state shows only for the key it was made for", () => {
    for (const phase of ["offline", "refused", "rejected", "unconfirmed"] as const) {
      const state = failed(phase);
      expect(leaveShown(state, leaveKey(share(4))).phase).toBe(phase);
      // the owner changed the share: nothing carries over, and it is the plain question again
      expect(leaveShown(state, leaveKey(share(5)))).toEqual({ ...LEAVE_IDLE, sent: true });
      // another share
      expect(leaveShown(state, leaveKey(share(4, "g2"))).phase).toBe("confirm");
      // not listed at all
      expect(leaveShown(state, null).phase).toBe("confirm");
    }
    expect(leaveShown(LEAVE_IDLE, null)).toEqual(LEAVE_IDLE);
  });

  test("opening the dialog again starts clean, for the same share too", () => {
    expect(leaveReduce(failed("unconfirmed"), { type: "open" })).toBe(LEAVE_IDLE);
  });

  test("only a proven refusal that is not no_grant keeps the server's sentence", () => {
    expect(failed("rejected").message).toBe("that isn't possible for this kind of share");
    for (const phase of ["offline", "refused", "unconfirmed"] as const) expect(failed(phase).message).toBeNull();
  });

  test("Check again: the same revision keeps the state and says what the read found", () => {
    const state = failed("unconfirmed");
    const key = leaveKey(share(4));
    expect(leaveReduce(state, { type: "checked", key, read: true })).toMatchObject({
      phase: "unconfirmed",
      checked: "listed",
    });
    expect(leaveReduce(state, { type: "checked", key, read: false })).toMatchObject({
      phase: "unconfirmed",
      checked: "unread",
    });
  });

  test("Check again that finds another revision ends the state: that Leave did not end the share", () => {
    const next = leaveReduce(failed("unconfirmed"), { type: "checked", key: leaveKey(share(5)), read: true });
    expect(next).toEqual({ ...LEAVE_IDLE, sent: true });
  });
});

describe("the dialog's words", () => {
  const of = (phase: LeaveState["phase"], over: Partial<LeaveState> = {}) =>
    leaveDialog({ ...LEAVE_IDLE, key: "k", phase, sent: phase !== "confirm", ...over }, "gpu-box", "Migrate the billing tables");

  test("the question names the session and the peer, and Leave is the destructive action", () => {
    const view = of("confirm");
    expect(view.question).toEqual({ title: "Migrate the billing tables", peer: "gpu-box" });
    expect(view.sub).toContain("The share ends on both instances now.");
    expect(view.sub).toContain("gpu-box is told that you left and can share it with you again.");
    expect(view).toMatchObject({ cancel: "Cancel", confirm: LEAVE_LABEL, action: "leave", disabled: false, danger: true });
  });

  test("offline: it says so, nothing changed, and Leave cannot be pressed", () => {
    const view = of("offline");
    expect(view.lead).toBe("gpu-box is not connected.");
    expect(view.sub).toContain("Nothing changed.");
    expect(view.sub).toContain("revoke the peer in Settings → Peers");
    expect(view).toMatchObject({ cancel: "Close", confirm: LEAVE_LABEL, disabled: true });
  });

  test("refused: the share is still there, and Leave is offered again", () => {
    const view = of("refused");
    expect(view.lead).toBe("gpu-box did not accept that.");
    expect(view.sub).toContain("it is still in your list");
    expect(view).toMatchObject({ cancel: "Close", confirm: LEAVE_LABEL, action: "leave", disabled: false });
  });

  test("uncertain: neither left nor refused, and the one action READS", () => {
    const view = of("unconfirmed");
    expect(view.lead).toBe("We could not confirm that you left.");
    expect(view.sub).toContain("may or may not have ended");
    expect(view.sub).toContain("nothing is sent again by itself");
    expect(view).toMatchObject({ cancel: "Close", confirm: LEAVE_CHECK, action: "check", danger: false });
    expect(view.lead).not.toMatch(/did not accept|not connected/);
    expect(view.note).toBeNull();
    expect(of("unconfirmed", { checked: "listed" }).note).toBe("Checked: it is still in your list.");
    expect(of("unconfirmed", { checked: "unread" }).note).toContain("could not be read");
  });

  test("another proven refusal shows the server's sentence as text and claims nothing else", () => {
    const view = of("rejected", { message: "that isn't possible for this kind of share" });
    expect(view.sub).toBe(
      "That isn't possible for this kind of share. Nothing changed: the share is still in your list.",
    );
    expect(of("rejected").sub).toBe("Nothing changed: the share is still in your list.");
    expect(view.action).toBe("leave");
  });
});
