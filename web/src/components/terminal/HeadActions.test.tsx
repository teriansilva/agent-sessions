import { render } from "@testing-library/react";
import { expect, test } from "vitest";
import { HeadActions, type HeadAction } from "./HeadActions";

/** `foldInto: "external"` publishes its overflow AND its full action list to the host's refs,
 *  and clears both on unmount (#1329) — RuntimeGate swaps this pane out when a chat/api roster
 *  entry arrives, and a stale list would make the merged menu omit session actions forever. */

const action = (id: string): HeadAction => ({
  id,
  label: id,
  aria: id,
  title: id,
  icon: null,
  run: () => {},
});

const props = {
  className: "c",
  btnClassName: "b",
  labelClassName: "l",
  foldInto: "external" as const,
};

test("publishes the full list to the host and clears it on unmount (#1329)", () => {
  const overflowRef = { current: [] as HeadAction[] };
  const allRef = { current: [] as HeadAction[] };
  const { unmount } = render(
    <HeadActions
      {...props}
      actions={[action("recap"), action("handoff")]}
      overflowRef={overflowRef}
      allRef={allRef}
    />,
  );
  expect(allRef.current.map((a) => a.id)).toEqual(["recap", "handoff"]);

  unmount();
  expect(allRef.current).toEqual([]);
  expect(overflowRef.current).toEqual([]);
});
