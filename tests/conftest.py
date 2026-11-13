"""Shared pytest fixtures for wan-dns-sync tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wan_dns_sync.config import Config


def make_config(**overrides: Any) -> Config:
    """A valid configuration; keyword arguments replace top-level sections."""
    raw: dict[str, Any] = {
        "opnsense": {
            "url": "https://opnsense.local/api",
            "key": "test-key",
            "secret": "test-secret",
            "interfaces": {"wan1": "wan", "wan2": "opt1"},
        },
        "providers": {"fake": {"class": "tests.fakes.FakeProvider"}},
        "zones": {
            "example.com": {
                "providers": ["fake"],
                "ttl": 300,
                "records": [
                    {"name": "test", "interfaces": ["wan1"]},
                    {"name": "multi", "interfaces": ["wan1", "wan2"]},
                ],
            }
        },
        "settings": {"interval": 60, "verify_delay": 0, "reconcile_interval": 3600},
    }
    raw.update(overrides)
    return Config.model_validate(raw)


@pytest.fixture
def sample_config() -> Config:
    return make_config()


@pytest.fixture
def sample_config_yaml(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
opnsense:
  url: "https://opnsense.local/api"
  key: "test-key"
  secret: "test-secret"
  interfaces:
    wan1: "wan"
    wan2: "opt1"

providers:
  fake:
    class: tests.fakes.FakeProvider

zones:
  example.com:
    providers: [fake]
    ttl: 300
    records:
      - name: test
        interfaces: [wan1]
      - name: multi
        interfaces: [wan1, wan2]

settings:
  interval: 60
"""
    )
    return config_file


@pytest.fixture
def mock_opnsense_response() -> dict[str, Any]:
    """Sample OPNsense interface config response."""
    return {
        "wan": {"ipv4": [{"ipaddr": "1.2.3.4"}], "status": "up"},
        "opt1": {"ipv4": [{"ipaddr": "5.6.7.8"}], "status": "up"},
        "lan": {"ipv4": [{"ipaddr": "192.168.1.1"}], "status": "up"},
    }
