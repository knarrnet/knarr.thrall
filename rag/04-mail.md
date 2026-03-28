# Mail

Knarr mail is a store-and-forward messaging system between nodes. Any node can send structured messages to any other node on the network. Mail is delivered asynchronously — if the recipient is offline, messages are queued and delivered when the recipient reconnects.

## Sending Mail

Mail is sent via the cockpit API or programmatically from skills:

```bash
curl -X POST http://localhost:8080/api/messages/send \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "to": "recipient-node-id",
    "body": {"type": "text", "content": "Hello from my node"},
    "msg_type": "text"
  }'
```

### Message Types

| Type | Purpose |
|------|---------|
| `text` | General messages, conversations |
| `offer` | Skill or service proposals |
| `delivery` | Delivery confirmations |
| `ack` | Acknowledgments |
| `error` | Error notifications |

## Receiving Mail

Poll the inbox via the cockpit API:

```bash
curl http://localhost:8080/api/messages \
  -H "Authorization: Bearer your-token"
```

This returns all messages in the inbox. Messages include:
- `from_node` — sender's node ID
- `msg_type` — message type
- `body` — message content (dict)
- `timestamp` — when the message was sent
- `session_id` — optional conversation threading ID

## Store-and-Forward

Mail uses a store-and-forward architecture:

1. **Sender** composes a message and submits it to their local node
2. **Outbox** queues the message for delivery
3. **SyncEngine** attempts delivery on the next heartbeat cycle
4. **If recipient is online:** message is pushed and acknowledged
5. **If recipient is offline:** message stays in outbox, retried on subsequent heartbeats
6. **Recipient** receives the message in their inbox when they come online

Messages have a default TTL of 72 hours (maximum 168 hours). Undelivered messages are automatically purged after TTL expires.

## Attachments

Mail supports attachments via sidecar assets:

```json
{
  "to": "recipient-node-id",
  "body": {"type": "text", "content": "See attached report"},
  "attachments": [
    {"uri": "knarr-asset://abc123hash", "name": "report.pdf"}
  ]
}
```

**Limits:**
- Maximum 10 attachments per message
- Maximum 10 MB per inline attachment
- Assets referenced by `knarr-asset://` URI are resolved from the local sidecar

## Sessions

Messages can be threaded using `session_id`:

```json
{
  "to": "recipient-node-id",
  "body": {"type": "text", "content": "Follow-up to our discussion"},
  "session_id": "project-alpha-review"
}
```

Session IDs are freeform strings. They help nodes group related messages for context in conversations and workflows.

## Mail as a Skill

Mail delivery is implemented as a system skill (`knarr-mail`). This means:
- Mail costs credits (configurable via `[mail] price`, default 1.0)
- Mail is subject to the same credit policy as skill calls
- Nodes can set different pricing for mail vs. skill execution

## Peer Overrides

For nodes on the same local network, you can configure direct delivery paths:

```toml
[peer_overrides]
abc123def456 = "192.168.1.50:9010"
```

This bypasses DNS/public routing for known peers, reducing latency for LAN deployments.

## Encryption

Mail supports optional end-to-end encryption via X25519 sealed boxes:
- Sender encrypts with recipient's public key
- Only the recipient's private key can decrypt
- Encrypted bodies stored as base64 in transit
- Decryption happens automatically on receive

## System Messages

System messages are prefixed with `knarr/commerce/` and automatically routed to dedicated handlers:
- `knarr/commerce/receipt` — execution receipt
- `knarr/commerce/credit_note` — settlement document
- `knarr/commerce/settle_request` — settlement initiation
- `knarr/commerce/settlement_confirmation` — settlement completion
- `knarr/commerce/tab_reminder` — balance notification

These are processed by commerce handlers, not stored in the regular inbox.

## Delivery Resilience

The SyncEngine handles reliable delivery with:
- **Push:** Direct delivery attempt on heartbeat cycle
- **Retry:** Exponential backoff on failure with circuit breaker
- **Pull:** Recipient periodically pulls from peers it hasn't heard from (configurable interval)
- **Circuit breaker:** After sustained failures, peer marked unreachable until seen alive again

Outbox capacity: 5000 items. Delivery is batched for efficiency.

## Mailbox Configuration

```toml
[mail]
accept_from = "all"              # "all", "whitelist", "groups", "none"
whitelist = []                   # Allowed senders (node IDs)
accept_groups = []               # Allowed groups
default_ttl_hours = 72           # Message lifetime
max_messages = 10000             # Inbox capacity
pull_interval = 60               # Pull sweep interval (seconds)
max_pull_batch = 5               # Messages per pull
stale_inbox_hours = 24           # Stale message alert threshold
```

## Best Practices

- Keep mail bodies concise. For large content, upload to sidecar and attach as asset reference.
- Use session IDs for multi-message conversations to help recipients maintain context.
- Acknowledge received messages to let senders know delivery succeeded.
- Set appropriate TTL for time-sensitive messages.
