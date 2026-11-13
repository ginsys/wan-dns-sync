"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("/etc/wan-dns-sync/config.yaml")


class _Strict(BaseModel):
    """Reject unknown keys, and never echo input values in validation errors.

    pydantic prints the offending input by default, which would put API
    secrets from sibling fields into the log.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class OPNsenseConfig(_Strict):
    """OPNsense API configuration."""

    url: str = Field(description="Base URL for OPNsense API (e.g., https://opnsense.local/api)")
    key: SecretStr = Field(description="API key")
    secret: SecretStr = Field(description="API secret")
    verify_ssl: bool = Field(default=True, description="Verify TLS certificates")
    ca_file: Path | None = Field(
        default=None, description="CA bundle to verify the OPNsense certificate against"
    )
    interfaces: dict[str, str] = Field(
        description="Mapping of logical names to OPNsense interface names"
    )

    @field_validator("key", "secret")
    @classmethod
    def _no_control_chars(cls, value: SecretStr) -> SecretStr:
        if _has_control_char(value.get_secret_value()):
            raise ValueError("credential contains a control character")
        return value


_LABEL = re.compile(r"^(\*|[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)$")


def _has_control_char(value: str) -> bool:
    """True for a newline or other control character, which no header or token may hold."""
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def _normalise_name(value: str, what: str) -> str:
    """Lower-case a DNS name without its trailing dot, and reject a malformed one.

    The message never echoes the value.
    """
    name = value.strip().lower().removesuffix(".")
    if not name or not all(_LABEL.match(label) for label in name.split(".")):
        raise ValueError(f"{what} is not a valid DNS name")
    return name


class RecordConfig(_Strict):
    """One A record, built from the addresses of one or more interfaces."""

    name: str = Field(min_length=1, description="Record name relative to the zone")
    interfaces: list[str] = Field(min_length=1, description="Logical interface names")

    @field_validator("name")
    @classmethod
    def _canonical_name(cls, value: str) -> str:
        if value.strip().endswith("."):
            raise ValueError("record name must be relative to the zone, without a trailing dot")
        return _normalise_name(value, "record name")


class ZoneConfig(_Strict):
    """A DNS zone, the providers that host it, and the records this tool owns in it."""

    providers: list[str] = Field(
        min_length=1, description="Provider names; several providers are all written"
    )
    ttl: int = Field(default=300, gt=0, description="TTL for the records, in seconds")
    records: list[RecordConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> ZoneConfig:
        names = [r.name for r in self.records]
        if len(names) != len(set(names)):
            raise ValueError("a record name is listed twice in one zone")
        return self


class SettingsConfig(_Strict):
    """Application settings."""

    interval: int = Field(default=300, gt=0, description="Seconds between OPNsense checks")
    reconcile_interval: int = Field(
        default=3600,
        gt=0,
        description="Seconds between full DNS reads when the addresses did not change",
    )
    dry_run: bool = Field(default=False, description="Plan changes but do not apply them")
    health_port: int | None = Field(default=None, description="HTTP health endpoint port")
    verify_delay: float = Field(default=2.0, ge=0, description="Seconds before DNS verification")
    allow_private_ips: bool = Field(
        default=False, description="Publish addresses that are not globally routable"
    )


class KubernetesConfig(_Strict):
    """Optional external-dns target annotation maintenance."""

    enabled: bool = False
    label_selector: str | None = Field(
        default=None, description="Label selector for Ingress/HTTPRoute resources"
    )
    interfaces: list[str] = Field(
        default_factory=list, description="Logical interfaces whose addresses become the target"
    )

    @model_validator(mode="after")
    def _require_when_enabled(self) -> KubernetesConfig:
        if self.enabled and not (self.label_selector and self.interfaces):
            raise ValueError("kubernetes.label_selector and kubernetes.interfaces are required")
        return self


class Config(_Strict):
    """Root configuration."""

    opnsense: OPNsenseConfig
    # repr=False: the arguments hold provider tokens.
    providers: dict[str, dict[str, Any]] = Field(
        repr=False,
        description="octoDNS provider name -> {class: dotted.path, ...provider arguments}",
    )
    zones: dict[str, ZoneConfig] = Field(min_length=1)
    settings: SettingsConfig = Field(default_factory=SettingsConfig)
    kubernetes: KubernetesConfig = Field(default_factory=KubernetesConfig)

    @field_validator("zones")
    @classmethod
    def _canonical_zones(cls, zones: dict[str, ZoneConfig]) -> dict[str, ZoneConfig]:
        canonical: dict[str, ZoneConfig] = {}
        for zone, zone_config in zones.items():
            name = _normalise_name(zone, "zone name")
            if name in canonical:
                raise ValueError("a zone is listed twice")
            canonical[name] = zone_config
        return canonical

    @model_validator(mode="after")
    def _check_references(self) -> Config:
        for name, args in self.providers.items():
            if not isinstance(args.get("class"), str):
                raise ValueError(f"provider '{name}' needs a 'class' (dotted import path)")
            for key, value in args.items():
                if isinstance(value, str) and _has_control_char(value):
                    # A newline in a token makes the HTTP library fail with a message
                    # that holds the whole token, and the provider logs that message.
                    raise ValueError(
                        f"provider '{name}' argument '{key}' contains a control character"
                    )
        known_interfaces = set(self.opnsense.interfaces)
        for zone, zone_config in self.zones.items():
            for provider in zone_config.providers:
                if provider not in self.providers:
                    raise ValueError(f"zone '{zone}' uses unknown provider '{provider}'")
            for record in zone_config.records:
                unknown = set(record.interfaces) - known_interfaces
                if unknown:
                    raise ValueError(
                        f"record '{record.name}' in zone '{zone}' uses unknown "
                        f"interfaces {sorted(unknown)}"
                    )
        unknown = set(self.kubernetes.interfaces) - known_interfaces
        if unknown:
            raise ValueError(f"kubernetes.interfaces uses unknown interfaces {sorted(unknown)}")
        return self


_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _substitute_env_vars(value: str) -> str:
    """Substitute ${VAR_NAME} patterns with environment variables."""

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(f"Environment variable '{var_name}' not set")
        return env_value

    return _ENV_PATTERN.sub(replacer, value)


def _process_env_vars(obj: object) -> object:
    """Recursively process environment variable substitution in a data structure."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _process_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_process_env_vars(item) for item in obj]
    return obj


def load_config(path: Path) -> Config:
    """Load and validate configuration from a YAML file."""
    with path.open() as f:
        try:
            raw_config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            # The parser's own message quotes the offending line, which can be a secret.
            mark = getattr(e, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            raise ValueError(f"{path} is not valid YAML{where}") from None

    return Config.model_validate(_process_env_vars(raw_config))
