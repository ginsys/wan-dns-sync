"""Shared pytest fixtures for wan-dns-sync tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from wan_dns_sync.config import (
    Config,
    HetznerConfig,
    OPNsenseConfig,
    RecordConfig,
    SettingsConfig,
)


@pytest.fixture
def sample_config() -> Config:
    """Create a sample configuration for testing."""
    return Config(
        opnsense=OPNsenseConfig(
            url="https://opnsense.local/api",
            key="test-key",
            secret="test-secret",
            verify_ssl=True,
            interfaces={"wan1": "wan", "wan2": "opt1"},
        ),
        hetzner=HetznerConfig(
            token="test-token",
            zone="example.com",
            ttl=300,
        ),
        settings=SettingsConfig(
            interval=60,
            dry_run=False,
            health_port=None,
            verify_delay=1.0,
        ),
        records=[
            RecordConfig(hostname="test", interfaces=["wan1"]),
            RecordConfig(hostname="multi", interfaces=["wan1", "wan2"]),
        ],
    )


@pytest.fixture
def sample_config_yaml(tmp_path: Path) -> Path:
    """Create a sample YAML config file for testing."""
    config_content = """
opnsense:
  url: "https://opnsense.local/api"
  key: "test-key"
  secret: "test-secret"
  verify_ssl: true
  interfaces:
    wan1: "wan"
    wan2: "opt1"

hetzner:
  token: "test-token"
  zone: "example.com"
  ttl: 300

settings:
  interval: 60
  dry_run: false

records:
  - hostname: test
    interfaces: [wan1]
  - hostname: multi
    interfaces: [wan1, wan2]
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)
    return config_file


@pytest.fixture
def mock_opnsense_response() -> dict[str, Any]:
    """Sample OPNsense interface config response."""
    return {
        "wan": {
            "ipv4": [{"ipaddr": "1.2.3.4"}],
            "status": "up",
        },
        "opt1": {
            "ipv4": [{"ipaddr": "5.6.7.8"}],
            "status": "up",
        },
        "lan": {
            "ipv4": [{"ipaddr": "192.168.1.1"}],
            "status": "up",
        },
    }


@pytest.fixture
def mock_hcloud_zone() -> MagicMock:
    """Create a mock hcloud BoundZone."""
    zone = MagicMock()
    zone.id = "zone-123"
    zone.name = "example.com"
    return zone


@pytest.fixture
def mock_hcloud_rrset() -> MagicMock:
    """Create a mock hcloud BoundZoneRRSet."""
    rrset = MagicMock()
    rrset.id = "rrset-123"
    rrset.name = "test"
    rrset.type = "A"
    rrset.ttl = 300

    record1 = MagicMock()
    record1.value = "1.2.3.4"
    rrset.records = [record1]

    return rrset
