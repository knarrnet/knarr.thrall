# Skills

Skills are the fundamental unit of work on the knarr network. A skill is an async Python function that accepts a dictionary and returns a dictionary. Any node can register skills and make them available to the network.

## Skill Handler

A skill handler is an async function with this signature:

```python
async def handle(input_data: dict) -> dict:
    # Process input
    # Return output
    return {"result": "value"}
```

A second signature provides access to sidecar assets via TaskContext:

```python
async def handle(input_data: dict, ctx) -> dict:
    # ctx.asset_dir — path to sidecar assets
    return {"result": "done"}
```

Both async and sync handlers are supported. Sync handlers run in a thread pool.

**Rules:**
- Input and output are flat `Dict[str, str]` — all values are strings
- Return a dict, never raise exceptions to the caller (handle errors internally)
- Long-running skills should be marked `slow = true` in the config

### Injected Fields

Every handler receives these fields automatically in `input_data`:
- `_job_id` — unique execution identifier
- `_caller_node_id` — the calling node's identity
- Any `knarr-asset://` URIs in input are resolved to local file paths before the handler runs

## Registration

Skills are registered in `knarr.toml`:

```toml
[skills.my-skill-name]
handler = "skills/my_skill.py:handle"
description = "What this skill does"
price = 1.0
tags = ["category", "subcategory"]
visibility = "public"
input_schema = {query = "string", limit = "string"}
output_schema = {status = "string", result = "string"}
```

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `handler` | string | Path to Python file and function (`path/to/file.py:function_name`) |
| `description` | string | Human-readable description of what the skill does |
| `price` | float | Cost in credits per execution (0.0 = free, max 1000.0) |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tags` | list | `[]` | Searchable tags for discovery |
| `visibility` | string | `"public"` | Access control (see below) |
| `input_schema` | dict | `{}` | Expected input fields and types |
| `output_schema` | dict | `{}` | Expected output fields and types |
| `slow` | bool | `false` | Mark as long-running (uses dedicated thread) |
| `timeout` | int | `30` | Execution timeout in seconds |
| `allowed_nodes` | list | `[]` | Node ID prefixes for whitelist visibility |
| `max_input_size` | int | `65536` | Maximum input size in bytes |
| `version` | string | `""` | Skill version string |
| `uri` | string | `""` | Taxonomy URI (`knarr:///category/name@version`) |

## Visibility

Controls who can call your skill:

| Value | Behavior |
|-------|----------|
| `public` | Anyone on the network can call it |
| `whitelist` | Only nodes in `allowed_nodes` can call it |
| `private` | Only the local node can call it (not announced to network) |

**Note:** Private skills are not announced via DHT. They exist only on your node and can only be called via the local cockpit API.

**Whitelist example:**

```toml
[skills.internal-tool]
handler = "skills/tool.py:handle"
visibility = "whitelist"
allowed_nodes = ["abc123def456", "789012345678"]
```

Node ID prefixes are matched — you only need the first 16 characters.

## Input and Output Schemas

Schemas declare expected fields. All values are strings:

```toml
input_schema = {query = "string", max_results = "string"}
output_schema = {status = "string", results = "string"}
```

**Important:** Every field declared in `input_schema` is treated as required. Only declare fields that the caller must provide. Optional fields should be documented in the description instead.

## Skill Chaining

Skills can call other skills on the same node using `NODE.call_local()`:

```python
NODE = None

def set_node(node):
    global NODE
    NODE = node

async def handle(input_data: dict) -> dict:
    # Call another local skill
    result = await NODE.call_local("other-skill", {"key": "value"}, timeout_ms=60000)
    return {"chained_result": result.get("output", "")}
```

**Note:** `call_local()` defaults to a 30-second timeout. Always pass `timeout_ms` explicitly for slow skills.

## Sidecar Assets

Skills can store and retrieve files via the sidecar:

```python
# Store an asset
hash_id = await NODE.store_asset(content_bytes, filename="report.pdf")

# Retrieve an asset
content = await NODE.get_asset(hash_id)
```

Assets are content-addressed (referenced by hash) and have a configurable TTL.

## Secrets

Sensitive values (API keys, passwords) should be stored in the node's vault, not hardcoded. Secrets are injected into `input_data` at execution time based on the skill's vault configuration in `knarr.toml`.

## Sentinel Reload

After changing skill configurations in `knarr.toml`, you can reload without restarting:

```bash
touch knarr.reload
```

The sentinel watches for this file and reloads skill definitions. Note: new expose blocks and node-level config changes still require a full restart.

## Execution Flow

1. Caller sends a task request to the provider node
2. Provider validates: skill exists, visibility allows caller, credit sufficient, input valid
3. Task enters the execution queue (bounded by `max_queue_depth`, default 100)
4. Worker picks up the task (bounded by `task_slots`, default 4 concurrent)
5. Handler executes with timeout enforcement
6. Result returned to caller
7. Receipt written, credit note issued, balance updated

## Pricing Guidelines

- `0.0` — Free utility skills, demos, system skills
- `1.0` — Standard skills (default)
- `3.0-5.0` — Skills that chain other skills or use significant compute
- `8.0-12.0` — Complex multi-step pipelines with GPU workloads
- Up to `1000.0` — Maximum allowed price
