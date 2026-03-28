# Configuration Reference

All knarr configuration lives in `knarr.toml`. This file is read at startup and can be partially reloaded at runtime via sentinel reload.

## Configuration Precedence

1. Hardcoded defaults (sensible out-of-box)
2. `knarr.toml` values (override defaults)
3. CLI arguments (override everything)

## Node

```toml
[node]
port = 9000                      # Protocol port (TCP)
host = "0.0.0.0"                 # Bind address
advertise_host = ""              # Public address (auto-detected if empty)
advertise_port = 9000            # Public port (if different from listening port)
storage = "node.db"              # SQLite database path
sidecar_port = 9001              # Asset server port (port+1 default, 0 to disable)
max_asset_size = 104857600       # Max single asset: 100MB
task_slots = 4                   # Concurrent skill execution (1-64)
max_queue_depth = 100            # Pending task queue limit
max_task_timeout = 3600          # Max execution time (seconds, 0=unlimited)
event_bus_size = 256             # Internal event bus buffer
auto_upgrade = false             # Auto-download new versions
```

## Network

```toml
[network]
bootstrap = ["host:port", ...]   # Bootstrap peers to join the network
upnp = false                     # Auto port-mapping via UPnP
max_connections = 50             # Connection pool size
gossip_fanout = 3                # DHT gossip propagation factor
heartbeat_silence_threshold = 90 # Seconds before dedicated heartbeat
peer_dead_timeout = 300          # Remove unresponsive peer (seconds)
min_peers = 8                    # Never prune below this count
```

Bootstrap peers are the entry points to the network. Public bootstraps: `bootstrap1.knarr.network:9000` and `bootstrap2.knarr.network:9000`.

## Cockpit

```toml
[cockpit]
port = 8080                    # HTTP API port
auth_token = "your-secret"     # Bearer token for API authentication
bind = "0.0.0.0"               # Bind address (0.0.0.0 = all interfaces)
tls = "both"                   # TLS mode: "off", "only", or "both"
```

When `tls = "both"`, the cockpit serves on two ports: HTTP on the configured port and HTTPS on port+1.

## Sidecar

```toml
[sidecar]
asset_dir = "./assets"              # Storage directory for sidecar files
asset_ttl = 604800                  # Asset time-to-live in seconds (default: 7 days)
max_total_size = 10737418240        # Maximum total storage in bytes (default: 10 GB)
```

## Policy (Credit Defaults)

```toml
[policy]
initial_credit = 3.0             # Starting balance for new peers
min_balance = -10.0              # Hard limit (blocking threshold)
tit_for_tat = false              # Reciprocity mode
```

## Economy (Advanced Limits)

```toml
[economy]
default_soft_limit = -5.0        # Warning threshold
default_hard_limit = -10.0       # Blocking threshold
```

## Settlement

```toml
[settlement]
soft_threshold = 0.8             # Trigger at 80% utilization
soft_target = 0.5                # Settle to 50%
min_settlement_amount = 10.0     # Minimum settlement
```

## Mail

```toml
[mail]
accept_from = "all"              # "all", "whitelist", "groups", "none"
whitelist = []                   # Allowed senders (node IDs)
accept_groups = []               # Allowed groups
default_ttl_hours = 72           # Message lifetime
max_messages = 10000             # Inbox capacity
pull_interval = 60               # Pull sweep interval (seconds)
max_pull_batch = 5               # Messages per pull
price = 1.0                     # Mail delivery cost
```

## Groups

```toml
[groups.my-group]
description = "Trading partners"
members = ["node_id_1", "node_id_2"]
members_file = "members.txt"     # One node ID per line

[policy.group.my-group]
initial_credit = 50.0
min_balance = -100.0

[policy.skill.expensive-skill]
initial_credit = 100.0
min_balance = -200.0
```

## Skills

Each skill is a TOML table under `[skills]`:

```toml
[skills.my-skill]
handler = "skills/my_skill.py:handle"
description = "What this skill does"
price = 1.0
tags = ["tag1", "tag2"]
visibility = "public"
input_schema = {field1 = "string", field2 = "string"}
output_schema = {status = "string", result = "string"}
slow = false
timeout = 30
```

See the [Skills](03-skills.md) document for detailed field descriptions.

## Expose (Storefront)

Expose blocks create public web forms for skills:

```toml
[expose.my-form]
skill = "my-skill-name"
path = "form-url-path"
mode = "static"
timeout = 30
display = {
    title = "Form Title",
    description = "What this form does",
    result_format = "text"
}
fields = {
    field1 = { label = "Field Label", required = true },
    field2 = { label = "Optional Field", required = false }
}
```

The form is served at `/s/{path}/` on the cockpit port. `result_format` can be `"text"`, `"json"`, or `"asset"`.

**Note:** Adding or changing expose blocks requires a node restart. Sentinel reload does not pick up expose changes.

## Plugins

Plugins are loaded from the `plugins/` directory. Each plugin has its own `plugin.toml`:

```
plugins/
  01-firewall/
    plugin.toml
    handler.py
  05-agent/
    plugin.toml
    handler.py
```

Plugins are loaded in alphabetical order. Plugin configuration is in their own `plugin.toml`, not in `knarr.toml`.

## Peer Overrides

Direct delivery paths for LAN peers:

```toml
[peer_overrides]
abc123def456 = "192.168.1.50:9010"
```

Matched by node ID prefix. Useful for nodes on the same network to avoid routing through the public internet.

## Reload vs. Restart

| Change | Reload | Restart |
|--------|--------|---------|
| Skill handler/config | Yes | Yes |
| Skill visibility/price | Yes | Yes |
| New skill | Yes | Yes |
| Expose block | No | Yes |
| Node port/host | No | Yes |
| Bootstrap peers | No | Yes |
| Commerce policy | No | Yes |
| Plugin config | No | Yes |

Reload: `touch knarr.reload` (or write an empty file named `knarr.reload` in the working directory).
