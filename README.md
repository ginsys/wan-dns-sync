# wan-dns-sync

Syncs OPNsense WAN interface IPs to DNS A records (currently Hetzner Cloud DNS).

## Overview

This tool queries OPNsense for WAN interface IP addresses and updates A records in Hetzner Cloud DNS. It supports:

- Multiple interfaces with logical names
- Multiple A records per hostname (for redundancy)
- Configurable update interval
- Dry-run mode for testing
- Only updates DNS when IPs actually change
- DNS verification after updates
- Rate limiting and retry with exponential backoff
- Optional health endpoints for Kubernetes
- Kubernetes Ingress/HTTPRoute annotation updates for apex domains

## Quick Start

### Using Environment Variables Only

The simplest way to run - no config file needed:

```bash
docker run --rm \
  -e OPNSENSE_URL=https://opnsense.local/api \
  -e OPNSENSE_API_KEY='your-key' \
  -e OPNSENSE_API_SECRET='your-secret' \
  -e OPNSENSE_INTERFACES='wan:igb0' \
  -e OPNSENSE_VERIFY_SSL=false \
  -e HCLOUD_TOKEN='your-token' \
  -e HETZNER_ZONE=example.com \
  -e RECORDS='home:wan' \
  -e DRY_RUN=true \
  ghcr.io/ginsys/wan-dns-sync:main \
  --once --log-level debug
```

### Environment Variables Reference

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `OPNSENSE_URL` | Yes | OPNsense API base URL | `https://192.168.1.1/api` |
| `OPNSENSE_API_KEY` | Yes | API key | |
| `OPNSENSE_API_SECRET` | Yes | API secret | |
| `OPNSENSE_INTERFACES` | Yes | Interface mappings (logical:opnsense) | `wan:igb0,backup:igb1` |
| `OPNSENSE_VERIFY_SSL` | No | Verify SSL certs (default: true) | `false` |
| `HCLOUD_TOKEN` | Yes | Hetzner Cloud API token | |
| `HETZNER_ZONE` | Yes | DNS zone to manage | `example.com` |
| `HETZNER_TTL` | No | TTL in seconds (default: 300) | `300` |
| `RECORDS` | Yes | Record definitions | `home:wan,server:wan+backup` |
| `INTERVAL` | No | Check interval seconds (default: 300) | `300` |
| `DRY_RUN` | No | Don't make changes (default: false) | `true` |
| `HEALTH_PORT` | No | HTTP health endpoint port | `8080` |
| `VERIFY_DELAY` | No | Seconds before DNS verification | `2.0` |

**Interface format**: `logical_name:opnsense_interface_name` - use the actual interface name from OPNsense (e.g., `igb0`, `em0`), not the friendly name.

**Records format**: `hostname:interface1+interface2` - multiple interfaces create multiple A records.

## Configuration

### Configuration Modes

The tool supports three configuration modes with the following priority:

1. **Explicit config file**: `-c /path/to/config.yaml`
2. **Default config file**: Auto-detected at `/etc/wan-dns-sync/config.yaml`
3. **Environment variables only**: If no config file exists

The tool logs which configuration source it uses at startup:

```json
{"source": "default config: /etc/wan-dns-sync/config.yaml", "event": "Configuration loaded", "level": "info"}
```

### Mixing Config File with Environment Variables

When using a config file, you can reference environment variables using `${VAR_NAME}` syntax. This allows you to:
- Keep non-sensitive configuration in version control
- Inject secrets at runtime via environment variables
- Use the same config file across different environments

### Using a Config File

For complex setups, use a YAML config file. Secrets can be injected via environment variables using `${VAR_NAME}` syntax:

```yaml
opnsense:
  url: "https://opnsense.local/api"
  key: "${OPNSENSE_API_KEY}"        # From environment variable
  secret: "${OPNSENSE_API_SECRET}"  # From environment variable
  verify_ssl: false                 # Set false for self-signed certs
  interfaces:
    wan1: "igb1"      # logical name -> OPNsense interface name
    wan2: "vlan0.2"

hetzner:
  token: "${HCLOUD_TOKEN}"          # Hetzner Cloud API token
  zone: "example.com"
  ttl: 300

settings:
  interval: 300       # seconds between checks
  dry_run: false
  health_port: 8080   # Enable health endpoints
  verify_delay: 2.0   # seconds to wait before DNS verification

kubernetes:
  enabled: false              # Set true to enable Kubernetes integration
  label_selector: "ginsys.net/apex-dns=true"
  trigger_hostname: "lb"      # Hostname that triggers annotation updates

records:
  - hostname: home
    interfaces: [wan1]            # Single interface -> 1 A record
  - hostname: office
    interfaces: [wan1, wan2]      # Both interfaces -> 2 A records
```

**Run with mounted config (auto-detected at default path):**

```bash
docker run --rm \
  -v ./config.yaml:/etc/wan-dns-sync/config.yaml:ro \
  -e OPNSENSE_API_KEY='your-key' \
  -e OPNSENSE_API_SECRET='your-secret' \
  -e HCLOUD_TOKEN='your-token' \
  ghcr.io/ginsys/wan-dns-sync:main --once
```

**Run with explicit config path:**

```bash
docker run --rm \
  -v ./config.yaml:/app/config.yaml:ro \
  -e OPNSENSE_API_KEY='your-key' \
  -e OPNSENSE_API_SECRET='your-secret' \
  -e HCLOUD_TOKEN='your-token' \
  ghcr.io/ginsys/wan-dns-sync:main -c /app/config.yaml --once
```

### Finding OPNsense Interface Names

To find the correct interface names for your OPNsense configuration:

#### Option 1: Via OPNsense API

```bash
curl -k -u "API_KEY:API_SECRET" \
  https://opnsense.local/api/diagnostics/interface/getInterfaceConfig \
  | jq 'keys'
```

This returns the actual interface names:
```json
["igb0", "igb1", "igb2", "lo0", "ovpns1"]
```

#### Option 2: Via OPNsense Web UI

1. Go to **Interfaces > Overview**
2. Note the **Device** column (e.g., `igb0`, `em0`)

#### Option 3: Run with Debug Logging

If you use the wrong interface name, the tool will show available interfaces:

```json
{"logical_name": "wan", "opnsense_name": "wan", "available_interfaces": ["igb0", "igb1", "lo0"], "event": "Interface not found in OPNsense", "level": "warning"}
```

## Deployment

### Kubernetes

1. Create the namespace and secret:

```bash
kubectl create namespace wan-dns-sync

kubectl create secret generic wan-dns-sync-secrets \
  --namespace wan-dns-sync \
  --from-literal=OPNSENSE_API_KEY='your-key' \
  --from-literal=OPNSENSE_API_SECRET='your-secret' \
  --from-literal=HCLOUD_TOKEN='your-token'
```

2. Edit `k8s/config.yaml` with your settings

3. Apply with kustomize:

```bash
kubectl apply -k k8s/
```

### Local Development

Using the Makefile:

```bash
# Create virtual environment and install dependencies
make venv

# Activate (or use mise for auto-activation)
source .venv/bin/activate

# Run all checks
make check

# Run with example config
make run
```

Or manually:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Docker

```bash
# Build locally
make build

# Or pull from registry
docker pull ghcr.io/ginsys/wan-dns-sync:main
```

## CLI Options

```
usage: wan-dns-sync [-h] [--config CONFIG] [--dry-run]
           [--log-level {debug,info,warning,error}] [--once]

Dynamic DNS updater for Hetzner Cloud DNS using OPNsense WAN IPs

options:
  -h, --help            show this help message and exit
  --config CONFIG, -c CONFIG
                        Path to config file (default: /etc/wan-dns-sync/config.yaml
                        if exists, else env vars)
  --dry-run             Don't make changes, just log what would be done
  --log-level {debug,info,warning,error}
                        Logging level (default: info)
  --once                Run once and exit (don't loop)
```

## How It Works

1. **Query OPNsense**: Fetches current IP addresses for configured WAN interfaces via the OPNsense REST API
2. **Query Hetzner DNS**: Fetches current A records for each configured hostname
3. **Compare**: Checks if current DNS records match desired IPs
4. **Update if needed**: Creates/updates/deletes A records to match desired state using RRSet operations
5. **Verify**: Queries Hetzner authoritative nameservers to confirm changes propagated
6. **Sleep**: Waits for the configured interval before repeating

### Features

#### Rate Limiting

API requests are rate-limited to 30 requests per minute to avoid hitting Hetzner API limits.

#### Retry with Backoff

Transient API failures (429, 5xx) are automatically retried with exponential backoff up to 3 times.

#### DNS Verification

After each update, the tool queries Hetzner's authoritative nameservers directly to verify the changes have propagated:
- `helium.ns.hetzner.de`
- `hydrogen.ns.hetzner.com`
- `oxygen.ns.hetzner.com`

#### Health Endpoints

When `health_port` is configured, the following endpoints are available:

| Endpoint | Purpose | Success | Failure |
|----------|---------|---------|---------|
| `/healthz` | Liveness probe | 200 OK | - |
| `/readyz` | Readiness probe | 200 OK (APIs reachable) | 503 Service Unavailable |

#### Kubernetes Integration

The tool can automatically update external-dns annotations on Kubernetes Ingress and HTTPRoute resources when DNS records change. This is useful for apex domains that require A records instead of CNAMEs.

**Configuration:**

```yaml
kubernetes:
  enabled: true                          # Enable kubernetes integration
  label_selector: "ginsys.net/apex-dns=true"  # Resources with this label will be updated
  trigger_hostname: "lb"                 # Only trigger when this hostname's DNS changes
```

**How it works:**

1. When the specified `trigger_hostname` DNS record is updated, the tool queries Kubernetes for Ingress and HTTPRoute resources matching `label_selector`
2. Sets their `external-dns.kubernetes.io/target` annotation (read by external-dns >= 0.22) to the new IP addresses, in one patch per resource. The same patch removes `external-dns.alpha.kubernetes.io/target`, which 0.3.0 also wrote
3. external-dns then creates A records instead of CNAME records for apex domains

**Requirements:**

- ServiceAccount with permissions to list and patch Ingress and HTTPRoute resources
- Resources must be labeled with the configured label selector
- Requires `kubernetes>=30.0` Python package (included)

**Example Ingress:**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-app
  labels:
    ginsys.net/apex-dns: "true"  # Will be updated by wan-dns-sync
  annotations:
    # annotation will be set automatically - no need to specify in manifest
spec:
  ingressClassName: nginx-external
  rules:
    - host: example.com  # apex domain
```

**RBAC Example:**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: wan-dns-sync
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: wan-dns-sync
rules:
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses"]
  verbs: ["get", "list", "patch"]
- apiGroups: ["gateway.networking.k8s.io"]
  resources: ["httproutes"]
  verbs: ["get", "list", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: wan-dns-sync
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: wan-dns-sync
subjects:
- kind: ServiceAccount
  name: wan-dns-sync
  namespace: default
```

### Logging

Output is JSON-formatted for easy parsing:

```json
{"zone": "example.com", "interval": 300, "dry_run": false, "event": "Starting wan-dns-sync", "level": "info"}
{"hostname": "test", "current": [], "desired": ["1.2.3.4"], "event": "Syncing A records", "level": "info"}
{"hostname": "test", "ips": ["1.2.3.4"], "event": "Created A record", "level": "info"}
```

Log levels:
- **DEBUG**: Detailed API calls, skipped updates when no changes needed
- **INFO**: Update cycles, DNS record changes, startup/shutdown
- **WARNING**: Missing interfaces, DNS verification mismatches
- **ERROR**: API failures

## API Setup

### OPNsense API Setup

1. Go to **System > Access > Users**
2. Create a new user (or edit existing)
3. Generate an API key pair
4. Go to **System > Access > Groups**
5. Ensure the user has the `Diagnostics: Interface` privilege

### Hetzner Cloud API Setup

1. Go to [Hetzner Cloud Console](https://console.hetzner.cloud/)
2. Select your project
3. Go to **Security > API Tokens**
4. Create a new token with read/write permissions

**Note**: This tool uses the new Hetzner Cloud API (`hcloud`), not the legacy DNS console API (`dns.hetzner.com`).

## Development

### Running Tests

```bash
make venv
make check   # runs lint, typecheck, and tests
```

Or individually:

```bash
make lint       # ruff check
make typecheck  # mypy
make test       # pytest
```

### Code Formatting

```bash
make format
```

### Releasing

To create a new release:

1. Update version in `pyproject.toml`
2. Commit and push to main
3. Create and push a tag:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

The CI workflow will automatically:
- Run tests and linting
- Build and push container images with version tags
- Create a GitHub release with auto-generated notes

Container tags created for `v0.1.0`:
- `ghcr.io/ginsys/wan-dns-sync:0.1.0`
- `ghcr.io/ginsys/wan-dns-sync:0.1`
- `ghcr.io/ginsys/wan-dns-sync:latest`

## License

MIT
