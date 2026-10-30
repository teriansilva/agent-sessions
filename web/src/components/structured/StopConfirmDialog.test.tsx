import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { StructuredStopInventory, StructuredWorkerEnd } from "../../types/api";
import { StopConfirmDialog } from "./StopConfirmDialog";
import { hasBackgroundWork } from "./structuredView";
import { WorkerEnds } from "./WorkerEnds";

let root: HTMLDivElement;
beforeEach(() => {
  root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
});
afterEach(() => root.remove());

const now = Math.floor(Date.now() / 1000);
const inventory = (over: Partial<StructuredStopInventory> = {}): StructuredStopInventory => ({
  worker: "w1",
  digest: "d".repeat(64),
  scan: "ok",
  tasks: [{ task_id: "t1", description: "pytest -x", started_at: now - 240 }],
  processes: [{ pid: 4121, start: 9, comm: "node", started_at: now - 960 }],
  more: 0,
  observed_at: now,
  ...over,
});

function open(over: Partial<React.ComponentProps<typeof StopConfirmDialog>> = {}) {
  const onKeep = vi.fn();
  const onStop = vi.fn();
  render(
    <StopConfirmDialog inventory={inventory()} previous={null} busy={false} onKeep={onKeep} onStop={onStop} {...over} />,
    { container: root },
  );
  return { onKeep, onStop };
}

test("only an inventory with work (or an unreadable scan) asks first", () => {
  expect(hasBackgroundWork(null)).toBe(false);
  expect(hasBackgroundWork(inventory({ tasks: [], processes: [] }))).toBe(false);
  expect(hasBackgroundWork(inventory({ tasks: [] }))).toBe(true);
  expect(hasBackgroundWork(inventory({ tasks: [], processes: [], more: 2 }))).toBe(true);
  expect(hasBackgroundWork(inventory({ tasks: [], processes: [], scan: "unknown" }))).toBe(true);
});

test("lists agent tasks and processes apart, by program and pid only; Keep running holds focus", async () => {
  const { onKeep, onStop } = open();
  const dialog = screen.getByTestId("structured-stop-confirm");
  expect(within(dialog).getByText(/Stopping ends 2 background jobs/)).toBeTruthy();
  expect(within(screen.getByTestId("structured-stop-tasks")).getByText("pytest -x")).toBeTruthy();
  expect(within(screen.getByTestId("structured-stop-processes")).getByText(/node · pid 4121/)).toBeTruthy();
  expect(document.activeElement?.textContent).toBe("Keep running");
  await userEvent.click(screen.getByTestId("structured-stop-confirm-go"));
  expect(onStop).toHaveBeenCalledWith(false);
  await userEvent.keyboard("{Escape}");
  expect(onKeep).toHaveBeenCalledTimes(1);
});

test("a refreshed list offers Stop everything beside the exact Stop, with its disclosure", async () => {
  const { onStop } = open({ previous: inventory({ processes: [] }) });
  const everything = screen.getByTestId("structured-stop-everything");
  expect(everything.textContent).toBe("Stop everything");
  expect(screen.getByTestId("structured-stop-everything-disclosure").textContent).toMatch(/not listed here/);
  expect(screen.getByTestId("structured-stop-confirm-go").textContent).toBe("Stop and end jobs");
  expect(document.activeElement?.textContent).toBe("Keep running");
  await userEvent.click(everything);
  expect(onStop).toHaveBeenLastCalledWith(true);
  await userEvent.click(screen.getByTestId("structured-stop-confirm-go"));
  expect(onStop).toHaveBeenLastCalledWith(false);
});

test("a list over the display limits shows the counts and offers only Stop everything", () => {
  open({ inventory: inventory({ more: 12, engine_more: 3 }) });
  expect(screen.getByTestId("structured-stop-confirm").getAttribute("data-kind")).toBe("everything");
  expect(screen.getByTestId("structured-stop-more-processes").textContent).toBe("and 12 more processes");
  expect(screen.getByTestId("structured-stop-more-tasks").textContent).toBe("and 3 more tasks");
  expect(screen.getByTestId("structured-stop-confirm-go").textContent).toBe("Stop everything");
  expect(screen.queryByTestId("structured-stop-everything")).toBeNull();
  expect(screen.getByTestId("structured-stop-everything-disclosure")).toBeTruthy();
});

test("a complete, unchanged list is confirmed exactly, with no Stop everything", () => {
  open();
  expect(screen.getByTestId("structured-stop-confirm").getAttribute("data-kind")).toBe("exact");
  expect(screen.queryByTestId("structured-stop-everything")).toBeNull();
  expect(screen.queryByTestId("structured-stop-everything-disclosure")).toBeNull();
});

test("a refreshed list says it changed and stamps the new process", () => {
  const before = inventory();
  open({
    previous: before,
    inventory: inventory({
      processes: [...before.processes, { pid: 4188, start: 30, comm: "cargo", started_at: now - 20 }],
    }),
  });
  expect(screen.getByTestId("structured-stop-changed").textContent).toMatch(/changed since you opened this/);
  const rows = within(screen.getByTestId("structured-stop-processes")).getAllByRole("listitem");
  expect(rows.map((r) => r.hasAttribute("data-new"))).toEqual([false, true]);
});

test("an unreadable scan is disclosed, never an empty list, and only Stop everything is offered", () => {
  open({ inventory: inventory({ scan: "unknown", processes: [] }) });
  expect(screen.getByTestId("structured-stop-unknown").textContent).toMatch(/can’t list this session’s processes/);
  expect(screen.getByTestId("structured-stop-everything-disclosure").textContent).toMatch(
    /ends everything the agent started in this session — including work started after this list or not listed here/,
  );
  expect(screen.getByText("Can’t be listed")).toBeTruthy();
  expect(screen.getByTestId("structured-stop-confirm-go").textContent).toBe("Stop everything");
  expect(screen.queryByText(/None running/)).toBeNull();
});

test("ended-with-worker rows name the job, the reason and the time; an idle exit says how long", () => {
  const ends: StructuredWorkerEnd[] = [
    {
      worker: "w0", reason: "stop", at: now, settled: true, after_turn: "t", scan: "ok",
      tasks: [{ task_id: "t1", description: "pytest -x" }],
      processes: [{ pid: 4121, start: 9, comm: "node", started_at: null }], more: 0,
    },
    { worker: "w1", reason: "oom", at: now, settled: false, after_turn: "t", scan: "unknown", tasks: [], processes: [], more: 0 },
    { worker: "w2", reason: "idle", at: now, settled: true, after_turn: "t", idle_for: 1800 },
  ];
  render(<WorkerEnds ends={ends} />, { container: root });
  const rows = screen.getAllByTestId("structured-worker-end").map((r) => r.textContent);
  expect(rows[0]).toMatch(/Ended with worker.*pytest -x · operator stop/);
  expect(rows[1]).toMatch(/node · pid 4121 · operator stop/);
  expect(rows[2]).toMatch(/couldn’t be listed · out of memory.*last seen/);
  expect(rows[3]).toMatch(/Idle exit.*Stopped after 30 min idle — no background work/);
});

test("a worker that could not answer is disclosed, keeps its known tasks, and offers only Stop everything", () => {
  open({ inventory: inventory({ reach: "unreachable" }) });
  expect(screen.getByTestId("structured-stop-confirm").getAttribute("data-reach")).toBe("unreachable");
  expect(screen.getByTestId("structured-stop-unreachable").textContent).toMatch(/can’t reach this session’s worker/);
  expect(screen.queryByTestId("structured-stop-unknown")).toBeNull();
  expect(screen.getByTestId("structured-stop-everything-disclosure").textContent).toMatch(
    /ends everything the agent started in this session — including work started after this list or not listed here/,
  );
  expect(within(screen.getByTestId("structured-stop-tasks")).getByText("pytest -x")).toBeTruthy();
  expect(screen.getByText("the worker didn’t answer")).toBeTruthy();
  expect(screen.getByTestId("structured-stop-confirm-go").textContent).toBe("Stop everything");
  expect(document.activeElement?.textContent).toBe("Keep running");
});

test("an unreadable scan is a visible row that offers Stop", async () => {
  const { BackgroundWork } = await import("./BackgroundWork");
  const onStop = vi.fn();
  render(
    <BackgroundWork
      native={{ native_id: "n", worker: "w1", background_active: false, background_tasks: [],
        stop_inventory: inventory({ scan: "unknown", tasks: [], processes: [] }) }}
      onStop={onStop}
    />,
    { container: root },
  );
  expect(screen.getByTestId("structured-scan-unknown").textContent).toMatch(/won’t idle out/);
  await userEvent.click(screen.getByRole("button", { name: "Stop…" }));
  expect(onStop).toHaveBeenCalledTimes(1);
});

test("the exact Stop says anything these jobs start while it runs also ends", () => {
  open();
  expect(screen.getByTestId("structured-stop-also-ends").textContent).toBe(
    "Anything these jobs start while Stop runs also ends.",
  );
});

test("a process that started while stopping is named so in its ended row", () => {
  render(
    <WorkerEnds ends={[{
      worker: "w0", reason: "stop", at: now, settled: true, after_turn: "t", scan: "ok", tasks: [],
      processes: [
        { pid: 41, start: 9, comm: "sh", started_at: null },
        { pid: 42, start: 10, comm: "node", started_at: null, started_during_stop: true },
      ],
      more: 0,
    }]} />,
    { container: root },
  );
  const rows = screen.getAllByTestId("structured-worker-end").map((r) => r.textContent);
  expect(rows[0]).toMatch(/sh · pid 41 · operator stop/);
  expect(rows[1]).toMatch(/node · pid 42 · started while stopping · operator stop/);
});

test("jobs an interrupted Stop could not resume are said out loud, with Stop everything", async () => {
  const { BackgroundWork } = await import("./BackgroundWork");
  const onStopEverything = vi.fn();
  render(
    <BackgroundWork
      native={{ native_id: "n", worker: "w1", background_active: false, background_tasks: [],
        stop_inventory: inventory(), paused_unresumed: 2 }}
      onStopEverything={onStopEverything}
    />,
    { container: root },
  );
  expect(screen.getByTestId("structured-paused-unresumed").textContent).toMatch(
    /2 jobs were paused by an interrupted Stop and could not be resumed — Stop everything ends them/,
  );
  await userEvent.click(screen.getByRole("button", { name: "Stop everything…" }));
  expect(onStopEverything).toHaveBeenCalledTimes(1);
});
