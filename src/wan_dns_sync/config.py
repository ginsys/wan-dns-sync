"""Configuration loading and validation."""

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path("/etc/wan-dns-sync/config.yaml")


class OPNsenseConfig(BaseModel):
    """OPNsense API configuration."""

    url: str = Field(description="Base URL for OPNsense API (e.g., https://opnsense.local/api)")
    key: str = Field(description="API key")
    secret: str = Field(description="API secret")
    verify_ssl: bool = Field(
        default=True, description="Verify SSL certificates (set False for self-signed)"
    )
    interfaces: dict[str, str] = Field(
        description="Mapping of logical names to OPNsense interface names"
    )


class HetznerConfig(BaseModel):
    """Hetzner Cloud DNS configuration."""

    token: str = Field(description="Hetzner Cloud API token (HCLOUD_TOKEN)")
    zone: str = Field(description="DNS zone name (e.g., example.com)")
    ttl: int = Field(default=300, description="TTL for DNS records in seconds")


class SettingsConfig(BaseModel):
    """Application settings."""

    interval: int = Field(default=300, description="Interval between checks in seconds")
    dry_run: bool = Field(default=False, description="If true, don't make changes")
    health_port: int | None = Field(
        default=None, description="HTTP health endpoint port (optional)"
    )
    verify_delay: float = Field(default=2.0, description="Seconds to wait before DNS verification")


class RecordConfig(BaseModel):
    """DNS record configuration."""

    hostname: str = Field(description="Hostname (without zone suffix)")
    interfaces: list[str] = Field(description="List of interface logical names)")


class KubernetesConfig(BaseModel):
    """Kubernetes integration configuration."""

    enabled: bool = Field(default=False, description="Enable kubernetes annotation updates")
    label_selector: str = Field(
        default="ginsys.net/apex-dns=true",
        description="Label selector for finding Ingress/HTTPRoute resources",
    )
    trigger_hostname: str = Field(
        default="st32",
        description="Only trigger updates when this hostname is synced",
    )


class Config(BaseModel):
    """Root configuration."""

    opnsense: OPNsenseConfig
    hetzner: HetznerConfig
    settings: SettingsConfig = Field(default_factory=SettingsConfig)
    kubernetes: KubernetesConfig = Field(default_factory=KubernetesConfig)
    records: list[RecordConfig]


def _substitute_env_vars(value: str) -> str:
    """Substitute ${VAR_NAME} patterns with environment variables."""
    pattern = re.compile(r"\$\{([^}]+)\}")

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(f"Environment variable '{var_name}' not set")
        return env_value

    return pattern.sub(replacer, value)


def _process_env_vars(obj: object) -> object:
    """Recursively process environment variable substitution in a data structure."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: _process_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_process_env_vars(item) for item in obj]
    return obj


def load_config(path: Path) -> Config:
    """Load and validate configuration from a YAML file."""
    with path.open() as f:
        raw_config = yaml.safe_load(f)

    # Substitute environment variables
    processed_config = _process_env_vars(raw_config)

    return Config.model_validate(processed_config)


def _get_env(name: str, default: str | None = None) -> str:
    """Get environment variable or raise if not set and no default."""
    value = os.environ.get(name, default)
    if value is None:
        raise ValueError(f"Environment variable '{name}' is required")
    return value


def _get_env_int(name: str, default: int) -> int:
    """Get environment variable as int."""
    value = os.environ.get(name)
    return int(value) if value else default


def _get_env_float(name: str, default: float) -> float:
    """Get environment variable as float."""
    value = os.environ.get(name)
    return float(value) if value else default


def _get_env_bool(name: str, default: bool) -> bool:
    """Get environment variable as bool."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes")


def load_config_from_env() -> Config:
    """Load configuration from environment variables.

    Required environment variables:
        OPNSENSE_URL: OPNsense API base URL
        OPNSENSE_API_KEY: API key
        OPNSENSE_API_SECRET: API secret
        OPNSENSE_INTERFACES: Comma-separated interface mappings (e.g., "wan:wan,backup:opt1")
        HCLOUD_TOKEN: Hetzner Cloud API token
        HETZNER_ZONE: DNS zone (e.g., example.com)
        RECORDS: Comma-separated record definitions (e.g., "home:wan,server:wan+backup")

    Optional environment variables:
        OPNSENSE_VERIFY_SSL: Verify SSL (default: true)
        HETZNER_TTL: TTL in seconds (default: 300)
        INTERVAL: Check interval in seconds (default: 300)
        DRY_RUN: Don't make changes (default: false)
        HEALTH_PORT: HTTP health port (default: none)
        VERIFY_DELAY: Seconds before DNS verification (default: 2.0)
    """
    # Parse interface mappings: "wan:wan,backup:opt1" -> {"wan": "wan", "backup": "opt1"}
    interfaces_str = _get_env("OPNSENSE_INTERFACES")
    interfaces = {}
    for mapping in interfaces_str.split(","):
        mapping = mapping.strip()
        if ":" in mapping:
            name, iface = mapping.split(":", 1)
            interfaces[name.strip()] = iface.strip()
        else:
            # If no colon, use same name for both
            interfaces[mapping] = mapping

    # Parse record definitions: "home:wan,server:wan+backup"
    records_str = _get_env("RECORDS")
    records = []
    for record_def in records_str.split(","):
        record_def = record_def.strip()
        if ":" in record_def:
            hostname, ifaces = record_def.split(":", 1)
            iface_list = [i.strip() for i in ifaces.split("+")]
        else:
            raise ValueError(
                f"Invalid record format '{record_def}', expected 'hostname:iface1+iface2'"
            )
        records.append(RecordConfig(hostname=hostname.strip(), interfaces=iface_list))

    # Health port
    health_port_str = os.environ.get("HEALTH_PORT")
    health_port = int(health_port_str) if health_port_str else None

    return Config(
        opnsense=OPNsenseConfig(
            url=_get_env("OPNSENSE_URL"),
            key=_get_env("OPNSENSE_API_KEY"),
            secret=_get_env("OPNSENSE_API_SECRET"),
            verify_ssl=_get_env_bool("OPNSENSE_VERIFY_SSL", True),
            interfaces=interfaces,
        ),
        hetzner=HetznerConfig(
            token=_get_env("HCLOUD_TOKEN"),
            zone=_get_env("HETZNER_ZONE"),
            ttl=_get_env_int("HETZNER_TTL", 300),
        ),
        settings=SettingsConfig(
            interval=_get_env_int("INTERVAL", 300),
            dry_run=_get_env_bool("DRY_RUN", False),
            health_port=health_port,
            verify_delay=_get_env_float("VERIFY_DELAY", 2.0),
        ),
        records=records,
    )


def load_config_auto(config_path: Path | None = None) -> tuple[Config, str]:
    """Load configuration with auto-detection.

    Priority:
    1. Explicit config path (if provided via -c)
    2. Default config file (if exists)
    3. Environment variables only

    Returns:
        Tuple of (Config, source_description) for logging
    """
    if config_path:
        return load_config(config_path), f"config file: {config_path}"

    if DEFAULT_CONFIG_PATH.exists():
        return load_config(DEFAULT_CONFIG_PATH), f"default config: {DEFAULT_CONFIG_PATH}"

    return load_config_from_env(), "environment variables"
