# Troubleshooting

Common issues and their solutions when running a knarr node.

## Connection Issues

### Node won't join the network
- **Check port forwarding.** Your protocol port must be reachable from the internet. Test with an external port checker.
- **Check `advertise_host`.** Must be your public IP or hostname, not `localhost` or `127.0.0.1`.
- **Check bootstrap peers.** Ensure at least one bootstrap peer is reachable. Try `knarr info` to see current peer count.
- **Firewall rules.** The protocol port uses plain TCP. Ensure your firewall allows inbound connections.

### Peers connect but skills aren't discovered
- **Wait for gossip.** Skill announcements propagate via DHT gossip. It may take 30-60 seconds after joining.
- **Check visibility.** Private skills are not announced. Whitelist skills only appear to listed nodes.
- **Check `knarr.toml` syntax.** A malformed skill entry may prevent registration. Check logs for parsing errors.

## Skill Execution Issues

### Skill returns timeout
- **Default timeout is 30 seconds.** Set `timeout = 300` (or appropriate value) in the skill config for slow skills.
- **Mark slow skills.** Add `slow = true` to the skill config so it runs in a dedicated thread instead of the event loop.
- **Check `call_local()` timeout.** When chaining skills, the default `call_local()` timeout is also 30 seconds. Pass `timeout_ms=60000` explicitly.

### Skill returns ACCESS_DENIED
- **Check visibility.** If set to `whitelist`, the caller must be in `allowed_nodes`.
- **Private + slow combination.** Private skills marked as `slow = true` can fail because the async job system routes through the cockpit, which checks visibility. Use `whitelist` with your own node ID instead of `private` for slow skills.
- **Check credit balance.** The caller may have insufficient credit. Check their balance in `/api/economy`.

### Async job stuck in "running"
- **Check the handler.** A skill that never returns will hold the job forever. Ensure all code paths return a dict.
- **Dedup guard.** The async jobs table prevents re-running the same input. If a previous run failed, the old failed job may block retries. The operator may need to clear the failed job entry.
- **Cockpit timeout.** The cockpit async handler defaults to 30s. Pass `"timeout": 600` in the execute request for slow skills.

### input_schema validation failures
- **All schema fields are required.** Every field declared in `input_schema` is treated as mandatory. Only declare truly required fields. Document optional fields in the description instead.

## Mail Issues

### Mail not delivering
- **Check peer connectivity.** Mail delivery requires an active connection to the recipient. Check `/api/peers` to verify the recipient is in your peer table.
- **Check credit balance.** Mail costs credits (default 1.0). Insufficient credit prevents delivery.
- **TTL expiry.** Messages have a 72-hour default TTL. If the recipient was offline for longer, the message was purged.
- **Check outbox.** Pending messages sit in the outbox until the next heartbeat cycle delivers them. Use `/api/messages` to check status.

### Mail body format
- **Body must be a dict, not a string.** The `body` field should be a raw JSON object like `{"type": "text", "content": "message"}`, not a JSON-encoded string.

## Configuration Issues

### Changes not taking effect
- **Sentinel reload limitations.** Skill changes reload via `touch knarr.reload`. But expose blocks, node ports, bootstrap peers, and commerce policy require a full restart.
- **Clear bytecode cache.** After editing Python plugin files, delete `__pycache__` directories before restarting to avoid stale bytecode.

### Expose form not showing
- **Restart required.** Expose blocks are only loaded at startup, not on sentinel reload.
- **Check the path.** The form is served at `/s/{path}/` on the cockpit port. Ensure the path matches your config.
- **Check skill name.** The `skill` field in the expose block must exactly match a registered skill name.

## Credit Issues

### Requests being rejected
- **Check `min_balance`.** The caller's effective balance (`balance + initial_credit`) must stay above `min_balance`.
- **New peer default.** New peers start at balance 0.0 with `initial_credit` worth of trust. Default credit window is 13 credits.
- **Group policy.** If the peer is in a group, the group policy overrides the global policy.

### Credits not accumulating
- **Check pricing.** Skills with `price = 0.0` do not affect balances.
- **Bilateral view.** Your view of the balance with a peer is local. The peer's view may differ.

## Performance Issues

### Event loop blocking
- **Mark slow skills.** Any skill taking more than a few seconds should have `slow = true`. Without it, the skill runs on the main event loop and blocks all other operations.
- **Limit concurrent inference.** If using LLM backends, gate concurrency to prevent resource exhaustion.

### High memory usage
- **Sidecar cleanup.** Large accumulated assets consume disk and memory. Set appropriate `asset_ttl` and `max_total_size`.
- **Peer table growth.** Large networks accumulate peer entries. The ledger is capped at 10,000 entries per node.

## Getting Help

- **GitHub Issues:** Report bugs and request features on the knarr repository
- **Network mail:** Send a message to any active node for community support
- **Documentation:** The docs in this bundle cover the core protocol, skills, mail, economy, and deployment
