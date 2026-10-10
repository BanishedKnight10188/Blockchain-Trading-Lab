"""Offline suite denies external network by default; fake transports stay deterministic."""

import pytest

from tests.offline_network import install


@pytest.fixture(autouse=True)
def offline_external_network(monkeypatch):
    install(monkeypatch)
