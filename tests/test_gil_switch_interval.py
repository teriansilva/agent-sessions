"""#2435: the served app shortens the GIL switch interval, and gives the old one back."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from agent_sessions import main


def test_the_served_app_shortens_the_gil_switch_interval_and_restores_it(auth_cfg):
    """A request that reads files waits for the GIL after every syscall; at the default 5 ms a
    session-list walk beside one busy thread took 11.1 s on the operator's install, at 1 ms 3.9 s.
    Only the served process changes: the lifespan sets it and restores it on shutdown."""
    before = sys.getswitchinterval()
    assert before != pytest.approx(main.GIL_SWITCH_INTERVAL_S)  # the test proves a change
    with TestClient(main.create_app(auth_cfg), base_url="https://testserver"):
        assert sys.getswitchinterval() == pytest.approx(main.GIL_SWITCH_INTERVAL_S)
    assert sys.getswitchinterval() == pytest.approx(before)
