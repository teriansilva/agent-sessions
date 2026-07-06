"""Run the Home Free agent: ``python -m agent_sessions.homefree``.

Configuration comes from the environment (set by the installer's systemd unit).
The agent's ``run_once`` returns when the relay control connection drops; this
entrypoint wraps it in an outer reconnect loop with exponential backoff + jitter
so a relay restart or network blip is retried instead of exiting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections.abc import Mapping
from pathlib import Path

from .agent import AgentConfig, HomeFreeAgent

log = logging.getLogger("battlelab.homefree")

_MAX_BACKOFF = 60.0


class ConfigError(ValueError):
    """A required piece of agent configuration is missing."""


def _value(env: Mapping[str, str], inline_key: str, file_key: str) -> str | None:
    """Prefer a 0600 file (``*_FILE``) over an inline env value."""
    path = env.get(file_key)
    if path:
        return Path(path).read_text().strip()
    inline = env.get(inline_key)
    return inline.strip() if inline else None


def build_config_from_env(env: Mapping[str, str] | None = None) -> AgentConfig:
    """Assemble an :class:`AgentConfig` from the environment. Unit-testable."""
    env = os.environ if env is None else env

    relay_url = env.get("AGENT_SESSIONS_RELAY_URL") or env.get("HOMEFREE_RELAY_URL")
    if not relay_url:
        raise ConfigError("AGENT_SESSIONS_RELAY_URL is not set")

    name = _value(env, "HOMEFREE_CONSOLE_NAME", "HOMEFREE_CONSOLE_NAME_FILE")
    if not name:
        raise ConfigError("console name is not configured (HOMEFREE_CONSOLE_NAME[_FILE])")

    access_key = _value(env, "HOMEFREE_ACCESS_KEY", "HOMEFREE_ACCESS_KEY_FILE")
    if not access_key:
        raise ConfigError("access key is not configured (HOMEFREE_ACCESS_KEY[_FILE])")

    identity = env.get("HOMEFREE_IDENTITY_PATH")
    if not identity:
        raise ConfigError("HOMEFREE_IDENTITY_PATH is not set")

    return AgentConfig(
        relay_url=relay_url.strip(),
        console_name=name,
        access_key=access_key,
        identity_path=Path(identity),
    )


async def _run_forever(config: AgentConfig) -> None:
    agent = HomeFreeAgent(config)
    backoff = 1.0
    while True:
        try:
            await agent.run_once()  # returns when the control connection closes
        except Exception as exc:
            # Top-level supervisor: any relay/network error is logged and retried.
            log.warning("relay connection error: %r", exc)
        # Exponential backoff with full jitter so many agents don't reconnect in lockstep.
        delay = min(backoff, _MAX_BACKOFF) * (0.5 + secrets.randbelow(1000) / 1000.0)
        log.info("reconnecting in %.1fs", delay)
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, _MAX_BACKOFF)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = build_config_from_env()
    except ConfigError as exc:
        log.error("home free agent misconfigured: %s", exc)
        raise SystemExit(2) from exc
    asyncio.run(_run_forever(config))


if __name__ == "__main__":
    main()
