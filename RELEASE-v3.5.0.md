# knarr-thrall v3.5.0 — Vörður (Settlement Identity)

**Date:** 2026-03-05
**Requires:** knarr >= 0.29.1 (tested on v0.37.0)
**Dependencies:** PyNaCl (Ed25519), base58 (optional, for Solana addresses)

---

## What's New

### S1: Warehouse Manager Quarantine Review
Thrall can now review, approve, and reject documents held in the WM quarantine.

- New action verbs: `wm_approve`, `wm_reject`
- New recipe: `wm-review.toml` — fires on `wm.document.held` bus event
- Hotwire rules: auto-approve credit_note/tab_reminder/settlement/payment, hold config_order, reject unknown
- `commerce.py`: `approve_quarantine()`, `reject_quarantine()`, `review_quarantine()`

### S2: BCW Payment Recipes
Early-action recipes that respond to blockchain payment events before and after finality.

- New recipe: `payment-received.toml` — fires on `payment.received.*` (included in block)
- New recipe: `payment-finalized.toml` — fires on `payment.finalized.*` (finality reached)
- Auto-discovered by `_collect_event_patterns()` — no handler wiring needed

### S3: Settlement Execution
Full autonomous on-chain settlement via Solana devnet.

- New skill: `settlement-execute-lite` — derives Solana addresses, checks wallet ceiling, signs and submits transactions
- New recipe: `settlement-execute.toml` — fires on `settlement.accepted`
- `identity.py`: `solana_address` property, `signing_key` accessor
- `commerce.py`: `derive_peer_solana_address()`, `submit_solana_tx()`, `request_devnet_airdrop()`
- Conversion rate: 1 credit = 1,000,000 lamports (0.001 SOL)
- Separation of concerns: thrall signs → BCW observes → no key sharing

---

## Files Changed

| File | Change |
|------|--------|
| `handler.py` | Wire commerce into ActionExecutor and ContextGatherer |
| `thrall_actions.py` | Add `wm_approve`, `wm_reject`, `execute_settlement` action verbs |
| `commerce.py` | Add WM quarantine ops + Solana devnet methods |
| `identity.py` | Add `solana_address` property + `signing_key` accessor |
| `plugin.toml` | Version bump to 3.5.0 |
| `recipes/wm-review.toml` | NEW — WM quarantine review |
| `recipes/payment-received.toml` | NEW — BCW payment received |
| `recipes/payment-finalized.toml` | NEW — BCW payment finalized |
| `recipes/settlement-execute.toml` | NEW — settlement execution trigger |
| `skills/settlement_execute.py` | NEW — on-chain settlement skill |
| `skills/settlement_check.py` | Fix plugin_dir path resolution |

## Recipe Count
18 → 22 recipes (4 new)

## Verification
- Tested on live provider (v0.29.1) — all recipes load, settlement skills execute, wallet debits correctly
- Tested on Docker gate cluster (v0.37.0) — plugin loads, 21 recipes, identity/wallet init, settlement skills pass
- Solana devnet addresses derived correctly (base58 when available, hex fallback)
