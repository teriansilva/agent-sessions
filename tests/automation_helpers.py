"""Seed current server proposals explicitly; legacy tests use raw ledger fixtures."""

from agent_sessions import automation
from agent_sessions import orchestrator_ledger as ledger


def current_action(rec):
    return {**rec, "authority": automation.capture(rec["session_id"], rec.get("mission_id"))}


def append_current_action(rec, path=None):
    return ledger.append(current_action(rec), path)
