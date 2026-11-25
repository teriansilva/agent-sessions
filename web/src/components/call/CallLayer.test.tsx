/** The minimised on-call bar (#1664): its caption is the newest HQ line the operator actually
 *  heard. A queued (not yet spoken) or cut (interrupted) line is muted in the expanded panel, so
 *  the bar must not quote it as if HQ said it. */
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { IDLE, type CallSnapshot, type Caption } from "./callClient";
import { CallContext, type CallContextValue } from "./callContext";
import { CallLayer } from "./CallLayer";

const noop = () => undefined;

function bar(snap: Partial<CallSnapshot>) {
  const value: CallContextValue = {
    snap: { ...IDLE, phase: "connected", serverState: "listening", connectedAt: Date.now(), ...snap },
    caps: null,
    refreshCaps: noop,
    start: noop,
    end: noop,
    setMuted: noop,
    cardDecided: noop,
    askNeedsYou: noop,
    stopTurn: noop,
    dismiss: noop,
    minimized: true,
    setMinimized: noop,
    captionsOn: true,
    setCaptionsOn: noop,
    levels: () => ({ input: 0, output: 0 }),
    audioDevices: () => null,
  };
  render(
    <CallContext.Provider value={value}>
      <CallLayer />
    </CallContext.Provider>,
  );
  return screen.getByTestId("call-bar");
}

function hq(id: string, text: string, status: Caption["status"]): Caption {
  return { id, who: "hq", text, kind: "answer", status };
}

test("a queued line after a spoken one is not quoted: the bar keeps the newest spoken line", () => {
  const b = bar({
    captions: [hq("h1", "Two auth tests fail.", "spoken"), hq("h2", "And one more.", "queued")],
  });
  expect(b).toHaveTextContent("Listening · HQ: “Two auth tests fail.”");
  expect(b).not.toHaveTextContent("And one more.");
});

test("a cut line is not quoted as spoken", () => {
  const b = bar({ captions: [hq("h1", "I was saying something.", "cut")] });
  expect(b).toHaveTextContent("Listening");
  expect(b).not.toHaveTextContent("I was saying something.");
  expect(b).not.toHaveTextContent("HQ:");
});

test("with no spoken HQ line the bar shows its state only", () => {
  const b = bar({ captions: [hq("h1", "Not said yet.", "queued")] });
  expect(b).toHaveTextContent("Listening");
  expect(b).not.toHaveTextContent("Not said yet.");
  expect(b).not.toHaveTextContent("HQ:");
});
