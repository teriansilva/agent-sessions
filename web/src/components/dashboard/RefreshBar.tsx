import d from "./Dashboard.module.css";

/** The dashboard's refresh bar (#1223): a 2px accent block sweeping a hairline track along the
 *  top edge while retained data is being re-read behind it. Ask's working bar (#1171), without its
 *  step line — there is one question per source here, not one pipeline.
 *
 *  Always in the layout, 2px tall, so starting and stopping never shifts the page; idle it is
 *  transparent and hidden from assistive tech. Only `transform` moves (design §5), and under
 *  reduced motion the block stands still at half opacity. */
export function RefreshBar({ active }: { active: boolean }) {
  return (
    <div
      className={`${d.refreshBar} ${active ? d.refreshBarOn : ""}`}
      data-testid="dashboard-refresh-bar"
      data-active={active ? "true" : "false"}
      {...(active
        ? { role: "progressbar", "aria-label": "Refreshing dashboard" }
        : { "aria-hidden": true })}
    >
      {active ? <span className={d.refreshBlock} /> : null}
    </div>
  );
}
