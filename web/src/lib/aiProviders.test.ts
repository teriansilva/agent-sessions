import { expect, test } from "vitest";
import { ackCovers, isLoopback, needsConsent } from "./aiProviders";

test("only literal loopback or localhost is this host (the server's rule)", () => {
  for (const u of [
    "http://localhost:11434/v1",
    "http://127.0.0.1/v1",
    "http://127.9.9.9/v1",
    "http://[::1]:8000/v1",
  ])
    expect(isLoopback(u)).toBe(true);
  for (const u of [
    "http://172.20.1.5/v1",
    "http://gpu.local/v1",
    "http://ai.internal/v1",
    "http://localhost.example.com/v1",
    "https://api.openai.com/v1",
    "nonsense",
  ])
    expect(isLoopback(u)).toBe(false);
  expect(needsConsent("")).toBe(false);
});

test("a stored ack covers exactly its provider and origin", () => {
  const ack = {
    provider: "openai",
    origin: "https://api.openai.com:443",
    at: "t",
    source: "operator",
  };
  expect(ackCovers(ack, "openai", "https://API.openai.com/v2")).toBe(true);
  expect(ackCovers(ack, "custom", "https://api.openai.com/v1")).toBe(false);
  expect(ackCovers(ack, "openai", "https://api.openai.com:8443/v1")).toBe(
    false,
  );
  expect(ackCovers(null, "openai", "https://api.openai.com/v1")).toBe(false);
});

test("the raw host decides, never the browser's normalized form (the server reads it raw)", () => {
  // `new URL()` turns all of these into 127.0.0.1; the server does not, so neither may we.
  for (const u of ["http://127.1:11434/v1", "http://0177.0.0.1/v1", "http://2130706433/v1", "http://127.000.000.001/v1"])
    expect(isLoopback(u)).toBe(false);
  expect(isLoopback("http://[0:0:0:0:0:0:0:1]:8000/v1")).toBe(true);
  // An IDN or an IPv6 host keeps the spelling the server stored the ack under.
  const idn = { provider: "custom", origin: "https://bücher.example:443", at: "t", source: "legacy" };
  expect(ackCovers(idn, "custom", "https://bücher.example/v1")).toBe(true);
  const v6 = { provider: "custom", origin: "http://2001:db8:0::1:8000", at: "t", source: "legacy" };
  expect(ackCovers(v6, "custom", "http://[2001:DB8:0::1]:8000/v1")).toBe(true);
});
