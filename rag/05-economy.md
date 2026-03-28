# Economy and Credits

Knarr uses a bilateral credit system. There is no global currency, no blockchain, and no central bank. Each node independently tracks its balance with every peer it interacts with.

## Bilateral Ledger

Every node maintains a local ledger with one entry per peer:

| Field | Description |
|-------|-------------|
| `balance` | Current credit balance with this peer |
| `tasks_provided` | Number of skills executed for this peer |
| `tasks_consumed` | Number of skills consumed from this peer |
| `first_seen` | When the relationship started |
| `last_updated` | Last balance change |

**Balance mechanics:**
- When you **provide** a skill: `balance += skill_price` (peer owes you more)
- When you **consume** a skill: `balance -= skill_price` (you owe the peer more)

Balances are local. Your view of the balance with a peer may differ from their view. This is by design — there is no reconciliation protocol.

## Credit Policy

Three values control the credit window:

| Config | Default | Description |
|--------|---------|-------------|
| `initial_credit` | `3.0` | Starting trust for new peers |
| `min_balance` | `-10.0` | Floor before requests are rejected |
| `tit_for_tat` | `false` | Require reciprocal providing |

**Credit window** = `initial_credit - min_balance` (default: 13.0 credits)

A new peer starts with balance 0.0 but gets `initial_credit` worth of trust. They can consume up to the credit window before being rejected. If they provide skills back, their balance grows and the window is effectively unlimited.

### Policy Check

When a skill request arrives:
1. Look up the requester's ledger entry (create at 0.0 if new)
2. Calculate effective balance: `balance + initial_credit`
3. If effective balance would drop below `min_balance`: **reject**
4. Otherwise: **accept** and deduct `skill_price`

## Pricing

Skills have a `price` field (float, 0.0 to 1000.0):

| Price | Typical Use |
|-------|-------------|
| `0.0` | Free — system skills, demos, utilities |
| `1.0` | Standard skills (default) |
| `3.0-5.0` | Skills that chain other skills |
| `8.0-12.0` | Complex multi-step pipelines |

Price 0.0 is fully supported and does not deduct credits.

## Tit-for-Tat Mode

When enabled (`tit_for_tat = true`):
- Credit only grows through bilateral exchange
- A peer must provide services to earn credit with you
- Prevents free-riding on initial credit alone

This is a provider-side setting. Each node decides its own policy.

## Group Policies

Named groups allow different credit terms for different peers:

```toml
[policy.group.partners]
members = ["node-id-prefix-1", "node-id-prefix-2"]
initial_credit = 100.0
min_balance = -500.0

[policy.group.free-tier]
members = ["node-id-prefix-3"]
initial_credit = 1.0
min_balance = -1.0
```

Groups are matched by node ID prefix. A node can belong to multiple groups — the most permissive policy applies.

### Per-Skill Policies

Override credit policy for specific skills:

```toml
[policy.skill.expensive-skill]
min_balance = -5.0
```

## Configuration

```toml
[commerce]
initial_credit = 3.0
min_balance = -10.0
tit_for_tat = false

[mail]
price = 1.0              # Cost to send mail
```

## Settlement and Netting

When bilateral balances grow large, settlement brings them back toward zero:

**Automatic netting:**
- Triggered when utilization exceeds soft threshold (default 80%)
- Settles balance back to target (default 50% utilization)
- Minimum settlement amount: 10 credits (prevents micro-settlements)

```toml
[settlement]
soft_threshold = 0.8         # Trigger at 80% utilization
soft_target = 0.5            # Settle to 50%
min_settlement_amount = 10.0
```

## Credit Notes

Every skill execution produces a signed credit note — a cryptographic receipt documenting the charge:

- **Debit note** — standard charge for service rendered
- **Credit note** — refund (references parent transaction via hash)
- **Zero note** — complimentary execution (e.g., group member, promotional)

Credit notes are signed with the provider's Ed25519 key and stored in the receipt log.

## Receipts

The receipt system provides a complete audit trail:

- **Execution receipt** — skill ran, success/failure, timing, hashes
- **Credit note** — accounting for the charge
- **Order ACK** — task accepted into queue
- **Mail delivery receipt** — batch delivered to peer
- **Mail receive receipt** — messages stored locally

All signed receipts use W3C Data Integrity format with Ed25519-JCS cryptosuite.

## Key Principles

- **No global ledger.** Each node keeps its own books. No consensus needed.
- **Everyone has a price.** There is no blacklist. Set your credit policy to control access.
- **Credits are not money.** They are units of work exchanged between peers. The credit system tracks reciprocity, not monetary value.
- **Self-balancing.** Nodes that only consume will eventually hit their credit floor. Nodes that provide earn credit. The system naturally incentivizes participation.
