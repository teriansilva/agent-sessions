#!/usr/bin/env python3
"""Run the speech compatibility check (#1397) against a pair of endpoints and print the report.

    scripts/voice_conformance.py --stt http://host:8771/v1 --tts http://host:8781/v1 --voice atlas

Tokens come from the environment (VOICE_STT_TOKEN / VOICE_TTS_TOKEN), never the command line, so
they do not show up in `ps` on a shared host.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from agent_sessions import speech_conformance, speech_endpoints


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stt", required=True)
    ap.add_argument("--tts", required=True)
    ap.add_argument("--stt-model", default="")
    ap.add_argument("--tts-model", default="")
    ap.add_argument("--voice", required=True)
    args = ap.parse_args()
    stt = speech_endpoints.Endpoint(args.stt, args.stt_model, os.environ.get("VOICE_STT_TOKEN", ""))
    tts = speech_endpoints.Endpoint(args.tts, args.tts_model, os.environ.get("VOICE_TTS_TOKEN", ""))
    rep = asyncio.run(speech_conformance.check(stt, tts, args.voice))
    print(json.dumps(rep.as_dict(), indent=2))
    return 0 if rep.usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
