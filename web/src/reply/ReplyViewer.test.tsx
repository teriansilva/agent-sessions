import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";

import fixture from "./fixtures/envelopes.json";
import { ReplyViewer } from "./ReplyViewer";

// The viewer opens REAL envelopes (sealed by the Python side, `fixtures/envelopes.json`) with the
// key from the fragment, and asks the server for nothing but the ciphertext (#1956).
const plain = fixture.cases.find((c) => !("password" in c))!;
const withPw = fixture.cases.find((c) => "password" in c)!;

function at(path: string) {
  window.history.replaceState(null, "", path);
}

function serve(body: unknown, status = 200) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  at("/");
});

describe("ReplyViewer (#1956)", () => {
  test("opens the reply, shows the hidden-tools placeholder, and sends no key", async () => {
    const calls = serve(plain.envelope);
    at(`/r/${plain.envelope.id}#k=${plain.key}`);
    render(<ReplyViewer />);
    expect(await screen.findByTestId("reply-open", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("Claude")).toBeInTheDocument();
    expect(screen.getByTestId("reply-tools-hidden")).toHaveTextContent("4 commands & tools hidden");
    expect(screen.getByText(/Split the pools/)).toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`/api/public/reply/${plain.envelope.id}`);
    expect(JSON.stringify(calls)).not.toContain(plain.key);
    expect(calls[0].init?.credentials).toBe("omit");
  });

  test("a password link asks, refuses a wrong one, opens with the right one", async () => {
    serve(withPw.envelope);
    at(`/r/${withPw.envelope.id}#k=${withPw.key}`);
    render(<ReplyViewer />);
    const field = await screen.findByLabelText("Password");
    await userEvent.type(field, "nope");
    await userEvent.click(screen.getByRole("button", { name: "Open reply" }));
    expect(await screen.findByRole("alert", {}, { timeout: 20_000 })).toHaveTextContent("Wrong password.");
    await userEvent.clear(field);
    await userEvent.type(field, (withPw as { password: string }).password);
    await userEvent.click(screen.getByRole("button", { name: "Open reply" }));
    expect(await screen.findByTestId("reply-open", {}, { timeout: 20_000 })).toBeInTheDocument();
  }, 45_000);

  test("revoked, expired or unknown: one 'no longer works'", async () => {
    serve({ detail: "link unavailable" }, 404);
    at(`/r/${plain.envelope.id}#k=${plain.key}`);
    render(<ReplyViewer />);
    expect(await screen.findByTestId("reply-unavailable")).toBeInTheDocument();
  });

  test("a link without its key asks for the whole link and fetches nothing", async () => {
    const calls = serve(plain.envelope);
    at(`/r/${plain.envelope.id}`);
    render(<ReplyViewer />);
    expect(await screen.findByTestId("reply-incomplete")).toBeInTheDocument();
    await waitFor(() => expect(calls).toHaveLength(0));
  });
});
