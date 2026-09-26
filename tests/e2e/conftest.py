"""Shared fixtures for the controlled E2E acceptance suite (tests/e2e)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from harness import NetworkGuard, Recorder

_RECORDERS: list[Recorder] = []


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_network: opt-in real-model gate (RFA_ENV_FILE); the network guard stays "
        "installed and the test allows exactly the configured model host",
    )


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    """No IP socket or DNS except what a test explicitly allows (loopback / one model host)."""
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    assert guard.attempts == [], "network access outside the allowed destinations"


@pytest.fixture(scope="session")
def recorder(tmp_path_factory):
    """Attempt reports go to RFA_E2E_REPORT_DIR, or a pytest temp directory by default."""
    configured = os.environ.get("RFA_E2E_REPORT_DIR")
    directory = Path(configured).resolve() if configured else tmp_path_factory.mktemp("reports")
    instance = Recorder(directory)
    _RECORDERS.append(instance)
    yield instance
    instance.write_summary()


def pytest_terminal_summary(terminalreporter):
    for instance in _RECORDERS:
        if instance.reports:
            terminalreporter.write_line(
                f"RFA E2E controlled attempt reports ({len(instance.reports)}): "
                f"{instance.directory / 'summary.json'}"
            )
