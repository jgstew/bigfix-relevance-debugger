"""Shared test scaffolding.

The unit suite runs offline: no Docker, qna, SSH or network.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_compat_memory(tmp_path, monkeypatch):
    """Keep bigfix-remote-client-relevance's fallback memory out of the real state dir.

    Evaluation tests run the real orchestrator against fake transports, and
    it persists which qna builds are too new for which target.
    """
    from bigfix_remote_client_relevance.bootstrap import compat_memory

    monkeypatch.setattr(compat_memory, "default_path", lambda: tmp_path / "compat_memory.json")
