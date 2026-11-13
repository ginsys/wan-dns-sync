"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import make_config
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
        config = load_config(sample_config_yaml)
        assert isinstance(config, Config)
        assert config.opnsense.url == "https://opnsense.local/api"
        assert list(config.zones) == ["example.com"]
        assert [r.name for r in config.zones["example.com"].records] == ["test", "multi"]

    def test_provider_secret_from_env(
        self, sample_config_yaml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_TOKEN", "my-secret-token")
        text = sample_config_yaml.read_text().replace(
            "class: tests.fakes.FakeProvider",
            'class: tests.fakes.FakeProvider\n    token: "${TEST_TOKEN}"',
        )
        sample_config_yaml.write_text(text)
        assert load_config(sample_config_yaml).providers["fake"]["token"] == "my-secret-token"

    def test_defaults(self) -> None:
        config = make_config(settings={})
        assert config.settings.interval == 300
        assert config.settings.reconcile_interval == 3600
        assert config.settings.dry_run is False
        assert config.settings.health_port is None
        assert config.settings.allow_private_ips is False
        assert config.opnsense.verify_ssl is True
        assert config.kubernetes.enabled is False

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml_error_does_not_quote_the_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("opnsense:\n  secret: SENTINEL_SECRET: broken\n")
        with pytest.raises(ValueError, match="not valid YAML at line 2") as exc:
            load_config(config_file)
        assert "SENTINEL_SECRET" not in str(exc.value)
        assert exc.value.__cause__ is None and exc.value.__suppress_context__


class TestValidation:
    """Structural checks, and that errors never echo secrets."""

    def test_error_does_not_contain_secrets(self) -> None:
        with pytest.raises(ValidationError) as exc:
            make_config(
                opnsense={
                    "url": "https://opnsense.local/api",
                    "key": "SUPER-SECRET-KEY",
                    "secret": "SUPER-SECRET-VALUE",
                    "interfaces": {"wan1": "wan"},
                    "typo_field": 1,
                }
            )
        assert "SUPER-SECRET" not in str(exc.value)

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError, match="settngs"):
            make_config(settngs={"interval": 5})

    def test_secret_not_in_repr(self, sample_config: Config) -> None:
        assert "test-secret" not in repr(sample_config)
        assert sample_config.opnsense.secret.get_secret_value() == "test-secret"

    def test_provider_token_not_in_repr(self) -> None:
        config = make_config(
            providers={"fake": {"class": "tests.fakes.FakeProvider", "token": "SENTINEL_TOKEN"}}
        )
        assert "SENTINEL_TOKEN" not in repr(config)
        assert config.providers["fake"]["token"] == "SENTINEL_TOKEN"

    def test_control_character_in_a_provider_argument_is_rejected_without_echo(self) -> None:
        with pytest.raises(ValidationError, match="control character") as exc:
            make_config(
                providers={"fake": {"class": "tests.fakes.FakeProvider", "token": "SENTINEL\n"}}
            )
        assert "SENTINEL" not in str(exc.value)

    def test_control_character_in_an_opnsense_credential_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="control character"):
            make_config(
                opnsense={
                    "url": "https://opnsense.local/api",
                    "key": "k",
                    "secret": "s\r\n",
                    "interfaces": {"wan1": "wan"},
                }
            )

    def test_record_and_zone_names_are_canonical(self) -> None:
        config = make_config(
            zones={
                "Example.COM.": {
                    "providers": ["fake"],
                    "records": [{"name": " Office ", "interfaces": ["wan1"]}],
                }
            }
        )
        assert list(config.zones) == ["example.com"]
        assert config.zones["example.com"].records[0].name == "office"

    @pytest.mark.parametrize("name", ["office.", "@", "", "a b", "a..b", "-a", "a\x00"])
    def test_malformed_record_name_is_rejected(self, name: str) -> None:
        with pytest.raises(ValidationError):
            make_config(
                zones={
                    "example.com": {
                        "providers": ["fake"],
                        "records": [{"name": name, "interfaces": ["wan1"]}],
                    }
                }
            )

    def test_names_differing_only_by_case_are_a_duplicate(self) -> None:
        with pytest.raises(ValidationError, match="listed twice"):
            make_config(
                zones={
                    "example.com": {
                        "providers": ["fake"],
                        "records": [
                            {"name": "Office", "interfaces": ["wan1"]},
                            {"name": "office", "interfaces": ["wan2"]},
                        ],
                    }
                }
            )

    def test_zone_with_unknown_provider(self) -> None:
        with pytest.raises(ValidationError, match="unknown provider 'nope'"):
            make_config(
                zones={
                    "example.com": {
                        "providers": ["nope"],
                        "records": [{"name": "a", "interfaces": ["wan1"]}],
                    }
                }
            )

    def test_provider_needs_class(self) -> None:
        with pytest.raises(ValidationError, match="needs a 'class'"):
            make_config(providers={"fake": {"token": "x"}})

    def test_record_with_unknown_interface(self) -> None:
        with pytest.raises(ValidationError, match="unknown interfaces"):
            make_config(
                zones={
                    "example.com": {
                        "providers": ["fake"],
                        "records": [{"name": "a", "interfaces": ["wan9"]}],
                    }
                }
            )

    def test_kubernetes_enabled_needs_selector_and_interfaces(self) -> None:
        with pytest.raises(ValidationError, match="label_selector"):
            make_config(kubernetes={"enabled": True})

    def test_kubernetes_unknown_interface(self) -> None:
        with pytest.raises(ValidationError, match="kubernetes.interfaces"):
            make_config(kubernetes={"enabled": True, "label_selector": "a=b", "interfaces": ["x"]})


