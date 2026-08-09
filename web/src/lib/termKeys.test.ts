import { describe, expect, test } from "vitest";
import { isCopyShortcut, isPasteShortcut } from "./termKeys";

type KE = {
  type: string;
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
};
const ev = (p: Partial<KE>): KE => ({
  type: "keydown",
  key: "v",
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  ...p,
});

describe("isPasteShortcut (#209)", () => {
  test("Ctrl+V is the paste shortcut off macOS, but not on macOS", () => {
    expect(isPasteShortcut(ev({ ctrlKey: true }), false)).toBe(true);
    expect(isPasteShortcut(ev({ ctrlKey: true }), true)).toBe(false); // Cmd is paste on mac
  });

  test("Cmd+V is the paste shortcut on macOS, but not off macOS", () => {
    expect(isPasteShortcut(ev({ metaKey: true }), true)).toBe(true);
    expect(isPasteShortcut(ev({ metaKey: true }), false)).toBe(false);
  });

  test("uppercase V (caps/shift) still counts", () => {
    expect(isPasteShortcut(ev({ key: "V", ctrlKey: true }), false)).toBe(true);
  });

  test("plain v, Ctrl+C, and Alt+Ctrl+V are not the paste shortcut", () => {
    expect(isPasteShortcut(ev({}), false)).toBe(false);
    expect(isPasteShortcut(ev({ key: "c", ctrlKey: true }), false)).toBe(false);
    expect(isPasteShortcut(ev({ ctrlKey: true, altKey: true }), false)).toBe(
      false,
    );
  });

  test("only keydown — keyup/keypress are ignored (no double-handling)", () => {
    expect(isPasteShortcut(ev({ type: "keyup", ctrlKey: true }), false)).toBe(
      false,
    );
    expect(
      isPasteShortcut(ev({ type: "keypress", ctrlKey: true }), false),
    ).toBe(false);
  });
});

describe("isCopyShortcut (#536)", () => {
  test("Ctrl+C and Ctrl+Shift+C on keydown are the copy shortcut", () => {
    expect(isCopyShortcut(ev({ ctrlKey: true, key: "c" }))).toBe(true);
    expect(isCopyShortcut(ev({ ctrlKey: true, key: "C" }))).toBe(true); // shifted variant
  });
  test("keyup never copies (double-fire guard)", () => {
    expect(isCopyShortcut(ev({ ctrlKey: true, key: "c", type: "keyup" }))).toBe(
      false,
    );
  });
  test("plain c / other modifiers are not the copy shortcut", () => {
    expect(isCopyShortcut(ev({ key: "c" }))).toBe(false);
    expect(isCopyShortcut(ev({ ctrlKey: true, key: "x" }))).toBe(false);
    expect(isCopyShortcut(ev({ ctrlKey: true, altKey: true, key: "c" }))).toBe(
      false,
    );
    // Cmd+C copies natively via the mirrored DOM selection — never intercepted.
    expect(isCopyShortcut(ev({ metaKey: true, key: "c" }))).toBe(false);
    expect(isCopyShortcut(ev({ ctrlKey: true, metaKey: true, key: "c" }))).toBe(
      false,
    );
  });
});
