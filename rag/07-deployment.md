# Deployment Guide

## Minimal Deployment

A knarr node needs:
- Python 3.11+
- A public IP or hostname (for other nodes to reach you)
- An open TCP port (default 9000)
- Disk space for the database and sidecar assets

```bash
pip install knarr
knarr init
knarr serve
```

## Docker Deployment

Knarr can be run in Docker:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install .
EXPOSE 9000 8080
CMD ["knarr", "serve", "--config", "knarr.toml"]
```

**Volume mounts** to preserve state across container restarts:
- `node.db` — peer table, ledger, mail inbox
- `key.pem` — node identity (lose this, lose your identity)
- `knarr.toml` — configuration
- `assets/` — sidecar storage
- `plugins/` — plugin directory

## Port Requirements

| Port | Protocol | Purpose |
|------|----------|---------|
| 9000 | TCP | Protocol port (node-to-node communication) |
| 8080 | HTTP | Cockpit API (local management) |
| 8081 | HTTPS | Cockpit API (TLS, when `tls = "both"`) |

**Minimum for network participation:** Protocol port must be reachable from the internet. Cockpit port can be local-only.

## NAT and Port Forwarding

If behind a NAT router:

1. Forward your chosen protocol port (e.g., 9010) to your machine
2. Set `advertise_host` to your public hostname or IP
3. Set `advertise_port` to the public-facing port

```toml
[node]
port = 9010                          # Local listening port
advertise_host = "your.ddns.org"     # Public hostname
advertise_port = 9010                # Public port
```

A dynamic DNS service works well for home deployments.

## TLS

The cockpit API supports TLS:

```toml
[cockpit]
tls = "both"     # Serve HTTP and HTTPS
```

Options:
- `"off"` — HTTP only
- `"only"` — HTTPS only
- `"both"` — HTTP on configured port, HTTPS on port+1

For the protocol port (node-to-node): this is plain TCP, not TLS. Message integrity is guaranteed by Ed25519 signatures on every message.

## Bootstrap Configuration

Every node needs at least one bootstrap peer to join the network:

```toml
[bootstrap]
peers = [
    "bootstrap1.knarr.network:9000",
    "bootstrap2.knarr.network:9000",
]
```

After initial discovery, your node maintains a peer cache and can reconnect without bootstrap on subsequent starts.

## Production Hardening

### Cockpit Security
- Always set a strong `auth_token`
- Bind cockpit to `127.0.0.1` if only accessed locally
- Use TLS for cockpit if exposing to the network

### Credit Policy
- Set conservative `initial_credit` and `min_balance` for public nodes
- Use group policies for trusted partners
- Enable `tit_for_tat` to prevent free-riding

### Resource Limits
- Set `max_total_size` on sidecar to cap disk usage
- Set `timeout` on skills to prevent runaway execution
- Use `slow = true` for skills that take more than a few seconds

### Monitoring
- The cockpit `/api/status` endpoint returns node health
- Check `/api/economy` for credit balances
- Check `/api/peers` for network connectivity
- Log files capture all protocol activity

## Custom Serve Script

For advanced deployments (plugins, custom initialization), use a custom serve script instead of `knarr serve`:

```python
import asyncio
from knarr.dht.node import KnarrNode

async def main():
    node = KnarrNode("knarr.toml")
    # Register custom skills, load plugins, etc.
    await node.start()

asyncio.run(main())
```

This gives you full control over initialization order, plugin loading, and skill registration.
