# ruff: noqa: E501, E402 — a fixture recorder: literal frames, and the repo import after setup
"""Copy a recorded frame log into a committed fixture: drop noise frames, blank host identity."""

import json
import sys

src, dst = sys.argv[1], sys.argv[2]
DROP_METHODS = {"account/rateLimits/updated", "thread/tokenUsage/updated"}
out = []
for line in open(src):
    x = json.loads(line)
    f = x["frame"]
    if f.get("type") in {"stream_event", "rate_limit_event"}:
        continue
    if f.get("type") == "system" and f.get("subtype") == "thinking_tokens":
        continue
    if f.get("method") in DROP_METHODS:
        continue
    if f.get("method") == "remoteControl/status/changed":
        f["params"]["serverName"] = "host"
        f["params"]["installationId"] = "00000000-0000-0000-0000-000000000000"
    if f.get("type") == "control_response" and isinstance(
        f.get("response", {}).get("response"), dict
    ):
        r = f["response"]["response"]
        if "account" in r:
            r["account"] = {}
    out.append(json.dumps(x, ensure_ascii=False))
open(dst, "w").write("\n".join(out) + "\n")
print(dst, len(out))
