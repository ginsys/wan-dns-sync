"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from wan_dns_sync.config import (
    Config,
    _process_env_vars,
    _substitute_env_vars,
    load_config,
)


class TestEnvVarSubstitution:
    """Tests for environment variable substitution."""

    def test_substitute_simple_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test simple variable substitution."""
        monkeypatch.setenv("TEST_VAR", "hello")
        result = _substitute_env_vars("${TEST_VAR}")
        assert result == "hello"

    def test_substitute_var_in_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test variable substitution within a string."""
        monkeypatch.setenv("API_KEY", "secret123")
        result = _substitute_env_vars("Bearer ${API_KEY}")
        assert result == "Bearer secret123"

    def test_substitute_multiple_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test multiple variable substitution."""
        monkeypatch.setenv("USER", "admin")
        monkeypatch.setenv("PASS", "secret")
        result = _substitute_env_vars("${USER}:${PASS}")
        assert result == "admin:secret"

    def test_substitute_missing_var_raises(self) -> None:
        """Test that missing variable raises ValueError."""
        with pytest.raises(ValueError, match="Environment variable 'NONEXISTENT'"):
            _substitute_env_vars("${NONEXISTENT}")

    def test_no_substitution_needed(self) -> None:
        """Test string without variables passes through."""
        result = _substitute_env_vars("plain string")
        assert result == "plain string"


class TestProcessEnvVars:
    """Tests for recursive environment variable processing."""

    def test_process_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test processing nested dict."""
        monkeypatch.setenv("TOKEN", "abc123")
        data = {"key": "${TOKEN}", "nested": {"inner": "${TOKEN}"}}
        result = _process_env_vars(data)
        assert result == {"key": "abc123", "nested": {"inner": "abc123"}}

    def test_process_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test processing list."""
        monkeypatch.setenv("VAL", "test")
        data = ["${VAL}", "static", {"key": "${VAL}"}]
        result = _process_env_vars(data)
        assert result == ["test", "static", {"key": "test"}]

    def test_process_non_string(self) -> None:
        """Test that non-string values pass through."""
        data = {"count": 42, "enabled": True, "ratio": 3.14}
        result = _process_env_vars(data)
        assert result == data


class TestLoadConfig:
    """Tests for loading configuration from YAML."""

    def test_load_valid_config(self, sample_config_yaml: Path) -> None:
        """Test loading a valid configuration file."""
        config = load_config(sample_config_yaml)
        assert isinstance(config, Config)
        assert config.opnsense.url == "https://opnsense.local/api"
        assert config.hetzner.zone == "example.com"
        assert len(config.records) == 2

    def test_load_config_with_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test loading config with environment variable substitution."""
        monkeypatch.setenv("TEST_TOKEN", "my-secret-token")

        config_content = """
opnsense:
  url: "https://opnsense.local/api"
  key: "key"
  secret: "secret"
  interfaces:
    wan: "wan"

hetzner:
  token: "${TEST_TOKEN}"
  zone: "example.com"

records:
  - hostname: test
    interfaces: [wan]
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)

        config = load_config(config_file)
        assert config.hetzner.token == "my-secret-token"

    def test_load_config_defaults(self, tmp_path: Path) -> None:
        """Test that defaults are applied for optional fields."""
        config_content = """
opnsense:
  url: "https://opnsense.local/api"
  key: "key"
  secret: "secret"
  interfaces:
    wan: "wan"

hetzner:
  token: "token"
  zone: "example.com"

records:
  - hostname: test
    interfaces: [wan]
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)

        config = load_config(config_file)

        # Check defaults
        assert config.settings.interval == 300
        assert config.settings.dry_run is False
        assert config.settings.health_port is None
        assert config.settings.verify_delay == 2.0
        assert config.hetzner.ttl == 300
        assert config.opnsense.verify_ssl is True

    def test_load_config_file_not_found(self, tmp_path: Path) -> None:
        """Test that missing config file raises error."""
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_load_config_invalid_yaml(self, tmp_path: Path) -> None:
        """Test that invalid YAML raises error."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("invalid: yaml: content:")

        with pytest.raises((ValueError, Exception)):
            load_config(config_file)
