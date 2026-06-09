import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfigProvider } from "./ConfigContext";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

const realLocation = window.location;
afterEach(() => {
  vi.unstubAllGlobals();
  Object.defineProperty(window, "location", { configurable: true, value: realLocation });
});

describe("ConfigProvider forced first-login change (#95)", () => {
  it("routes to /change-password when /api/config says must_change_password", async () => {
    // jsdom's location.assign can't be spied on directly — swap in a minimal stub.
    const assign = vi.fn();
    Object.defineProperty(window, "location", { configurable: true, value: { assign } });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          csrf: "t",
          new_session_engines: [],
          terminal_backend: "ws",
          must_change_password: true,
        }),
      ),
    );
    render(
      <ConfigProvider>
        <div>app</div>
      </ConfigProvider>,
    );
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/change-password"));
  });
});
