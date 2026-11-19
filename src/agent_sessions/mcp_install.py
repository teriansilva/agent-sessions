"""Package sources for MCP servers (#2163 P1b): npm and PyPI, pinned and inert.

P1a ships the registry, the broker path, the launcher and the check with REMOTE servers only;
this module is the seam P1b fills. Until then a package source is refused and a stdio server has
no command.
"""

from __future__ import annotations

from .mcp_registry import RegistryError


def validate_source(raw: dict) -> dict:
    raise RegistryError("package sources (npm, PyPI) are not available yet")


def command(rec: dict) -> tuple[str, list[str]]:
    raise RegistryError("this server is not installed")
