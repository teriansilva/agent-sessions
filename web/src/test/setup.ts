import "@testing-library/jest-dom";
import { beforeEach } from "vitest";

import { setRoster } from "../app/engineRoster";
import type { EngineInfo } from "../types/api";
import fixture from "./roster.fixture.json";

// Every test renders agents from the roster GENERATED from the manifests (#853 P4) —
// `tests/test_web_roster_fixture.py` fails if it drifts. A test about the not-yet-loaded or an
// unusual roster calls `resetRoster()` / `setRoster(...)` itself.
beforeEach(() => {
  setRoster(fixture.engines as EngineInfo[], fixture.problems);
});
