import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "vitest";
import { SEND_RISK } from "./SharePubliclyModal";

/** The RISKY text the dialog shows before a send link exists is the server's own `SEND_RISK`
 *  (#1404, the operator's decision on #1423) — word for word, so the warning the operator accepts
 *  is the one the server records as accepted. Read from the Python source: a reworded server
 *  string fails here instead of the two drifting apart. */
test("the dialog's send-link risk is the server's SEND_RISK, verbatim", () => {
  const py = readFileSync(
    resolve(__dirname, "../../../../src/agent_sessions/routes/shares.py"),
    "utf8",
  );
  const block = /^SEND_RISK = \(\n([\s\S]*?)\n\)/m.exec(py);
  expect(block).not.toBeNull();
  const server = [...block![1].matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]).join("");
  expect(server).toContain("including reaching saved secret values the agent can see");
  expect(SEND_RISK).toBe(server);
});
