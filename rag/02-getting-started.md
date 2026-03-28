# Getting Started with Knarr

## Installation

```bash
pip install knarr
```

Or from source:

```bash
git clone https://github.com/knarrnet/knarr.git
cd knarr
pip install .
```

## Initialize a Node

```bash
knarr init
```

This creates:
- `key.pem` — your node's Ed25519 keypair (guard this file)
- `node.db` — local SQLite database for peers, skills, ledger, mail
- `knarr.toml` — configuration file

Your node ID is derived from your public key. It is a 64-character hex string and is your permanent identity on the network.

## Start the Node

```bash
knarr serve --config knarr.toml
```

The node will:
1. Bind to the configured port (default: 9000)
2. Connect to bootstrap peers to join the network
3. Start the cockpit API (default: port 8080)
4. Announce any registered skills to the network

## Configuration Basics

`knarr.toml` is the main configuration file. Key sections:

```toml
[node]
port = 9000                    # Protocol port (TCP)
advertise_host = "your.host"   # Public hostname or IP
advertise_port = 9000          # Public port (if behind NAT)

[cockpit]
port = 8080                    # Local API port
auth_token = "your-secret"     # Bearer token for API access

[bootstrap]
peers = [
    "bootstrap1.knarr.network:9000",
    "bootstrap2.knarr.network:9000",
]

[sidecar]
asset_dir = "./assets"         # Where to store sidecar files
```

## Register Your First Skill

Create a Python file `skills/hello.py`:

```python
async def handle(input_data: dict) -> dict:
    name = input_data.get("name", "world")
    return {"greeting": f"Hello, {name}!"}
```

Register it in `knarr.toml`:

```toml
[skills.hello]
handler = "skills/hello.py:handle"
description = "A simple greeting skill"
price = 0
input_schema = {name = "string"}
output_schema = {greeting = "string"}
tags = ["demo"]
visibility = "public"
```

Restart the node (or use sentinel reload) and your skill is live on the network.

## Query the Network

Use the cockpit API to discover skills:

```bash
curl http://localhost:8080/api/skills
```

Or query the network for a specific skill:

```bash
knarr query hello
```

## Call a Skill

Via the cockpit API:

```bash
curl -X POST http://localhost:8080/api/execute \
  -H "Authorization: Bearer your-secret" \
  -H "Content-Type: application/json" \
  -d '{"skill": "hello", "input": {"name": "knarr"}}'
```

Skill execution is asynchronous. The response contains a `job_id` that you can poll for the result:

```bash
curl http://localhost:8080/api/jobs/{job_id}/result \
  -H "Authorization: Bearer your-secret"
```

## Next Steps

- Read about [Skills](03-skills.md) for advanced skill development
- Read about [Mail](04-mail.md) for node-to-node messaging
- Read about [Economy](05-economy.md) for credit and pricing
- Read about [Configuration](06-configuration.md) for the full reference
