/** A judged objective on the row (#1088): the state word, the tag, the evidence as PLAIN TEXT, and
 *  "Not met — judge again" sent for the episode the row was drawn at. The real-browser half is
 *  `e2e/mission-judged-objectives.spec.ts`. */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, test, vi } from "vitest";

import type { MissionObjective, MissionSupervisor } from "../../types/api";

import { MissionObjectives } from "./MissionObjectives";

const T = "transcript:claude:5f3c0000-0000-0000-0000-0000000000a1";

function judged(over: Partial<MissionObjective> = {}, obs: Record<string, unknown> = {}) {
  return {
    mission_id: "m",
    key: "finding",
    ord: 0,
    title: "A finding is written down",
    probe: "supervisor_judged",
    probe_args: null,
    gate: true,
    state: "met",
    met_at: 100,
    source: "playbook",
    observed: {
      at: 100,
      detail: "the finding names a cause",
      value: true,
      judged: {
        met: true,
        confidence: 0.93,
        threshold: 0.9,
        evidence: [{ source: T, quote: "<b>Root cause</b>: the lock order." }],
        fingerprint: "f",
        checked_at: 100,
      },
      ...obs,
    },
    ...over,
  } as MissionObjective;
}

function sup(episode = 2): MissionSupervisor {
  return {
    objectives: [
      {
        key: "finding",
        title: "A finding is written down",
        gate: true,
        state: "met",
        met: true,
        current: true,
        episode,
        stood_down: false,
        spent: 0,
        remaining: 3,
        may_nudge: false,
        why_not: "",
      },
    ],
  } as unknown as MissionSupervisor;
}

function renderList(o: MissionObjective, onOps = vi.fn().mockResolvedValue(true)) {
  render(
    <MemoryRouter>
      <MissionObjectives objectives={[o]} onOps={onOps} supervisor={sup()} />
    </MemoryRouter>,
  );
  return onOps;
}

test("a judged row reads 'judged met (0.93)' with a judged tag — never 'observed'", () => {
  renderList(judged());
  expect(screen.getByTestId("objective-state")).toHaveTextContent("judged met (0.93)");
  expect(screen.getByTestId("objective-tag")).toHaveTextContent("judged");
});

test("a probe-settled row reads 'met' with an observed tag", () => {
  renderList(
    judged({ probe: "forge_checks", title: "Checks are green" }, { judged: undefined }),
  );
  expect(screen.getByTestId("objective-tag")).toHaveTextContent("observed");
  expect(screen.getByTestId("objective-state").textContent).toMatch(/^met /);
});

test("the evidence opens on request and renders quotes as PLAIN TEXT", async () => {
  renderList(judged());
  expect(screen.queryByTestId("objective-evidence")).toBeNull();
  const toggle = screen.getByTestId("objective-evidence-toggle");
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  await userEvent.click(toggle);
  const panel = screen.getByTestId("objective-evidence");
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  const quote = within(panel).getByTestId("objective-evidence-quote");
  // The markup in the quote is TEXT, not an element.
  expect(quote.textContent).toBe("<b>Root cause</b>: the lock order.");
  expect(quote.querySelector("b")).toBeNull();
  expect(panel).toHaveTextContent("transcript · claude:5f3c…a1");
  expect(panel).toHaveTextContent("Why: the finding names a cause");
  expect(panel).toHaveTextContent(/Holds while the session is unchanged/);
});

test("'Not met — judge again' sends reject_judgment for the episode the row was drawn at", async () => {
  const onOps = renderList(judged());
  await userEvent.click(screen.getByTestId("objective-evidence-toggle"));
  await userEvent.click(screen.getByTestId("objective-reject-judgment"));
  expect(onOps).toHaveBeenCalledWith([
    { op: "reject_judgment", key: "finding", episode: 2 },
  ]);
});

test("the overrule is in the row's ⋯ menu too", async () => {
  const onOps = renderList(judged());
  await userEvent.click(screen.getByTestId("objective-menu"));
  await userEvent.click(screen.getByTestId("objective-menu-reject-judgment"));
  expect(onOps).toHaveBeenCalledWith([
    { op: "reject_judgment", key: "finding", episode: 2 },
  ]);
});

test("a stale judgment says so and offers the evidence it was made on — and no overrule of it", async () => {
  renderList(
    judged({ state: "pending", met_at: null }, { stale: true, reason: "the session output changed", value: undefined }),
  );
  expect(screen.getByTestId("objective-judged-stale")).toHaveTextContent(/stale/);
  const toggle = screen.getByTestId("objective-evidence-toggle");
  expect(toggle.textContent).toMatch(/^Show evidence from /);
  await userEvent.click(toggle);
  expect(screen.queryByTestId("objective-reject-judgment")).toBeNull();
});

test("below the threshold the panel says so and the row does not claim met", async () => {
  renderList(
    judged(
      { state: "pending", met_at: null },
      {
        value: false,
        judged: {
          met: true,
          confidence: 0.62,
          threshold: 0.9,
          evidence: [{ source: T, quote: "I think" }],
          fingerprint: "f",
          checked_at: 100,
        },
      },
    ),
  );
  expect(screen.getByTestId("objective-state")).toHaveTextContent("judged not yet (0.62)");
  await userEvent.click(screen.getByTestId("objective-evidence-toggle"));
  expect(screen.getByTestId("objective-evidence")).toHaveTextContent(/below your 0\.90/i);
});

test("no AI endpoint: the row says it cannot be judged and where to fix it", () => {
  renderList(
    judged(
      { state: "pending", met_at: null },
      {
        stale: true,
        reason: "cannot be judged — no AI endpoint is configured",
        judged: { attempted_fp: "f", transient: true },
        value: undefined,
      },
    ),
  );
  const why = screen.getByTestId("objective-judged-unknown");
  expect(why).toHaveTextContent(/Cannot be judged — no AI endpoint is configured/);
  expect(within(why).getByRole("link", { name: "Settings → AI" })).toBeInTheDocument();
});

test("no AI endpoint on an OPTIONAL goal: no claim that the mission cannot propose (Hermes 5159)", () => {
  const unknown = {
    stale: true,
    reason: "cannot be judged — no AI endpoint is configured",
    judged: { attempted_fp: "f", transient: true },
    value: undefined,
  };
  const first = render(
    <MemoryRouter>
      <MissionObjectives
        objectives={[judged({ state: "pending", met_at: null, gate: false }, unknown)]}
        onOps={vi.fn().mockResolvedValue(true)}
        supervisor={sup()}
      />
    </MemoryRouter>,
  );
  const why = screen.getByTestId("objective-judged-unknown");
  expect(why).toHaveTextContent(/Cannot be judged — no AI endpoint is configured/);
  expect(why.textContent).not.toMatch(/cannot propose itself finished/);
  first.unmount();
  // …while a REQUIRED row does block the proposal, and says so.
  renderList(judged({ state: "pending", met_at: null, gate: true }, unknown));
  expect(screen.getByTestId("objective-judged-unknown")).toHaveTextContent(/cannot propose itself finished/);
});

test("a met row whose last attempt failed reads 'not current' — no 'met' for a screen reader either", () => {
  renderList(
    judged(
      {},
      {
        stale: true,
        reason: "the judge could not answer: timeout",
        judged: { attempted_fp: "f", transient: true },
        value: undefined,
        last: { value: true, confidence: 0.93, at: 1 },
      },
    ),
  );
  const state = screen.getByTestId("objective-state");
  expect(state).toHaveTextContent("judged earlier · not current");
  expect(screen.getByTestId("objective").textContent).not.toMatch(/\bmet\b/);
});

test("aria-controls names the evidence panel only while it exists", async () => {
  renderList(judged());
  const toggle = screen.getByTestId("objective-evidence-toggle");
  expect(toggle).not.toHaveAttribute("aria-controls");
  await userEvent.click(toggle);
  const id = toggle.getAttribute("aria-controls");
  expect(id).toBeTruthy();
  expect(document.getElementById(id!)).toBe(screen.getByTestId("objective-evidence"));
});

// ---- review round 4 (#1097, Hermes 5040) ----------------------------------------------------

test("waived AFTER a verdict: the waiver is the state; the judgment is history only", async () => {
  renderList(
    judged(
      { state: "waived", met_at: null },
      {
        value: false,
        judged: {
          met: false,
          confidence: 0.98,
          threshold: 0.9,
          evidence: [{ source: T, quote: "the finding was never written down" }],
          fingerprint: "f",
          checked_at: 100,
        },
      },
    ),
  );
  expect(screen.getByTestId("objective-state")).toHaveTextContent(/^waived$/);
  expect(screen.getByTestId("objective").textContent).not.toMatch(/judged not yet/);
  const toggle = screen.getByTestId("objective-evidence-toggle");
  expect(toggle).toHaveTextContent("Show the earlier judgment");
  await userEvent.click(toggle);
  expect(screen.getByTestId("objective-evidence")).toHaveTextContent(
    /Earlier judgment — you marked this not required/,
  );
  expect(screen.queryByTestId("objective-reject-judgment")).toBeNull();
});

test("reject → waive: the waiver replaces the rejected label", () => {
  renderList(judged({ state: "waived", met_at: null }, { value: false, rejected_at: 200 }));
  expect(screen.getByTestId("objective-state")).toHaveTextContent(/^waived$/);
  expect(screen.queryByTestId("objective-judged-rejected")).toBeNull();
  expect(screen.getByTestId("objective").textContent).not.toMatch(/rejected/);
});

test("a NEGATIVE verdict above the threshold says 'judged not met', not 'below your 0.90'", async () => {
  renderList(
    judged(
      { state: "pending", met_at: null },
      {
        value: false,
        judged: {
          met: false,
          confidence: 0.97,
          threshold: 0.9,
          evidence: [{ source: T, quote: "no finding appears in the transcript" }],
          fingerprint: "f",
          checked_at: 100,
        },
      },
    ),
  );
  await userEvent.click(screen.getByTestId("objective-evidence-toggle"));
  const panel = screen.getByTestId("objective-evidence");
  expect(panel).toHaveTextContent(/judged not met/);
  expect(panel).not.toHaveTextContent(/below your/);
});

test("the stale message follows the structured stale_kind, not the reason's wording", () => {
  const stale = (kind: string, reason: string) =>
    judged({ state: "met" }, { stale: true, stale_kind: kind, reason, value: undefined });
  const { unmount } = render(
    <MemoryRouter>
      <MissionObjectives objectives={[stale("criterion", "anything at all")]} supervisor={sup()} />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("objective")).toHaveTextContent("You changed this objective since the judgment.");
  unmount();
  // The OLD prose, with the input kind: the wording must not decide it.
  render(
    <MemoryRouter>
      <MissionObjectives
        objectives={[stale("input", "the objective changed since the judgment")]}
        supervisor={sup()}
      />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("objective")).toHaveTextContent("Session output changed since the judgment.");
});

test("unknown → waive: the waiver wins and the no-endpoint warning goes (Hermes 5104)", () => {
  renderList(
    judged(
      { state: "waived", met_at: null },
      {
        stale: true,
        stale_kind: "unknown",
        reason: "cannot be judged — no AI endpoint is configured",
        judged: { attempted_fp: "f", transient: true },
        value: undefined,
      },
    ),
  );
  expect(screen.getByTestId("objective-state")).toHaveTextContent(/^waived$/);
  expect(screen.queryByTestId("objective-judged-unknown")).toBeNull();
  expect(screen.getByTestId("objective").textContent).not.toMatch(/cannot propose itself finished/);
});
