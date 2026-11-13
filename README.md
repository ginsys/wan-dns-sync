# wan-dns-sync

Keeps DNS A records in step with the WAN addresses of an OPNsense router. DNS is written through
[octoDNS](https://github.com/octodns/octodns) provider classes, so any octoDNS provider works
(deSEC and Hetzner Cloud DNS are the two this project is set up for).

## Overview

The daemon asks the OPNsense API for the IPv4 address of each configured WAN interface and makes
the A records you name match them. It does nothing else to your zone.

- Provider-neutral: a provider is an octoDNS class plus its own options
- A zone can have several providers, all written (use this to move a zone between providers)
- Owns only the records you list: other records in the zone are never changed or deleted
- Refuses to create a missing zone
- Skips the provider while the addresses are unchanged, and re-reads the zone every
  `reconcile_interval` to correct drift
- Publishes only global IPv4 addresses (private and CGNAT addresses are skipped) unless you allow them
- Verifies a change against every authoritative nameserver of the zone
- Dry-run mode, `--once` mode with a meaningful exit code
- Optional health endpoints for Kubernetes, answered from cached state
- Optional external-dns `target` annotation on labelled Ingress and HTTPRoute resources

## Quick start

1. Copy `config.example.yaml` to `config.yaml` and edit it.
2. Run one dry cycle:

```bash
docker run --rm \
  -v ./config.yaml:/etc/wan-dns-sync/config.yaml:ro \
  -e OPNSENSE_API_KEY='your-key' \
  -e OPNSENSE_API_SECRET='your-secret' \
  -e HCLOUD_TOKEN='your-token' \
  ghcr.io/ginsys/wan-dns-sync:main --once --dry-run --log-level debug
```

`--once` exits 0 when the whole cycle succeeded and 1 otherwise. A config file is required; the
default path is `/etc/wan-dns-sync/config.yaml`.

## Configuration

`${VAR}` in any value is replaced from the environment, so secrets stay out of the file. A missing
variable is an error. Unknown keys are errors too, and error messages never print secret values.

```yaml
opnsense:
  url: "https://opnsense.local/api"
  key: "${OPNSENSE_API_KEY}"
  secret: "${OPNSENSE_API_SECRET}"
  verify_ssl: true                  # keep true; for a private CA set ca_file instead of false
  # ca_file: /etc/wan-dns-sync/ca.pem
  interfaces:
    wan1: "igb1"                    # logical name -> OPNsense interface name
    wan2: "vlan0.2"

providers:                          # octoDNS provider classes; keys besides `class` go to the provider
  # Every ${VAR} must be set even for a provider no zone uses, so list only the ones you run.
  desec:
    class: octodns_desec.DesecProvider
    token: "${DESEC_TOKEN}"
  hetzner:
    class: octodns_hetzner.HetznerProvider
    token: "${HCLOUD_TOKEN}"
    backend: hcloud

zones:
  example.com:
    providers: [hetzner]            # every listed provider is written
    ttl: 300
    records:
      - name: home
        interfaces: [wan1]          # one address
      - name: office
        interfaces: [wan1, wan2]    # two addresses, one per interface

settings:
  interval: 300                     # seconds between OPNsense checks
  reconcile_interval: 3600          # seconds between full zone reads while nothing changes
  dry_run: false
  health_port: 8080                 # null disables the health server
  verify_delay: 120                 # seconds before verifying a change (deSEC needs ~2 min)
  allow_private_ips: false

kubernetes:                         # optional
  enabled: false
  # label_selector: "example.com/apex-dns=true"
  # interfaces: [wan1, wan2]
```

A zone must already exist at each of its providers. A record whose interfaces have no usable
address in a cycle is left as it is (it is never deleted); the cycle is reported as failed only if
OPNsense returned nothing at all.

Provider options and their limits (minimum TTL, token scopes) belong to the provider: see
[octodns-desec](https://github.com/octodns/octodns-desec) and
[octodns-hetzner](https://github.com/octodns/octodns-hetzner).

### Moving a zone to another provider

1. Add the new provider next to the old one: `providers: [hetzner, desec]`. Every cycle writes both.
2. Move the zone's NS records at the registrar.
3. Remove the old provider: `providers: [desec]`.

### Finding OPNsense interface names

```bash
curl --user "$OPNSENSE_API_KEY:$OPNSENSE_API_SECRET" \
  https://opnsense.local/api/diagnostics/interface/getInterfaceConfig | jq 'keys'
```

or **Interfaces > Overview**, column **Device**. A wrong name logs the interfaces that do exist.

### Upgrading from 0.x (opnsense-dyndns-hetzner)

1.0 changes the configuration; there is no compatibility mode.

| 0.x | 1.0 |
|---|---|
| environment-only configuration (`OPNSENSE_URL`, `RECORDS`, ...) | removed: use a config file |
| `hetzner: {token, zone, ttl}` | `providers:` entry plus `zones.<zone>: {providers, ttl, records}` |
| `records: [{hostname, interfaces}]` | `zones.<zone>.records: [{name, interfaces}]` |
| `kubernetes.trigger_hostname` | removed: annotations are reconciled every cycle |
| `kubernetes.label_selector` default | no default: set it |
| image `ghcr.io/ginsys/opnsense-dyndns-hetzner` | `ghcr.io/ginsys/wan-dns-sync` |
| config path `/etc/opnsense-dyndns-hetzner/` | `/etc/wan-dns-sync/` |

## Deployment

### Kubernetes

```bash
kubectl create namespace wan-dns-sync
kubectl create secret generic wan-dns-sync-secrets --namespace wan-dns-sync \
  --from-literal=OPNSENSE_API_KEY='your-key' \
  --from-literal=OPNSENSE_API_SECRET='your-secret' \
  --from-literal=HCLOUD_TOKEN='your-token'
```

Edit `k8s/config.yaml`, then `kubectl apply -k k8s/`.

Probes: `/healthz` fails when the loop has not run for `3 x interval + 120` seconds; `/readyz`
fails until a cycle has succeeded and while the last cycle failed. Neither calls OPNsense or a DNS
API.

### Local development

```bash
make venv        # virtual environment with dev dependencies
make check       # ruff, mypy, pytest
make run         # one dry-run cycle with config.example.yaml
make build       # container image
```

## CLI

```
wan-dns-sync [-h] [--config CONFIG] [--dry-run]
             [--log-level {debug,info,warning,error}] [--once]
```

## How it works

Each cycle:

1. Read the WAN addresses from OPNsense. A failure, or no address at all, fails the cycle.
2. Work out the addresses for each owned record.
3. If they differ from the last applied set, or `reconcile_interval` has passed, plan the zone at
   each provider with only the owned records considered, and apply the plan. A plan that touches
   anything else is refused.
4. After a change, ask every authoritative nameserver of the zone for the record.
5. Update the external-dns annotations (if enabled), independent of DNS success.

At most one DNS read per provider and zone per `reconcile_interval` while the addresses are stable,
which matters for providers with a request budget such as deSEC.

### Kubernetes annotations

With `kubernetes.enabled`, Ingress and HTTPRoute resources matching `label_selector` get the
`external-dns.kubernetes.io/target` annotation set to the addresses of `kubernetes.interfaces`,
so external-dns creates A records for apex names. The same patch removes the older
`external-dns.alpha.kubernetes.io/target`. The pod needs a ServiceAccount that may list and patch
`ingresses` (networking.k8s.io) and `httproutes` (gateway.networking.k8s.io); the selector is
applied client-side, so grant this carefully.

### Logging

JSON lines on stdout. INFO: cycles and DNS changes; WARNING: missing interfaces, skipped
addresses, verification mismatches; ERROR: failed syncs.

## API setup

### OPNsense

1. **System > Access > Users**: create a user and generate an API key pair.
2. **System > Access > Groups**: give it the `Diagnostics: Interface` privilege.

### Providers

- **deSEC**: create a token (preferably scoped to the zone). Rate limits apply per account.
- **Hetzner Cloud**: **Security > API Tokens** in the project, read/write. The token covers the
  whole project.

## Development

```bash
make check      # lint, typecheck, test
make format
```

### Releasing

1. Update the version in `pyproject.toml`, commit and push to main.
2. Create and push a tag `vX.Y.Z`. CI runs the checks, pushes the container image with version tags
   (`X.Y.Z`, `X.Y`, `latest`) and creates a GitHub release with generated notes.

## License

MIT
