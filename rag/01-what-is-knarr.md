# What is Knarr?

Knarr is a peer-to-peer protocol for autonomous agent skill exchange. Nodes on the network offer computing skills (functions that accept input and return output) and consume skills from other nodes. There is no central server, no platform fee, and no single point of failure.

## Core Concepts

**Node**: A process running the knarr software. Each node has a unique cryptographic identity (Ed25519 keypair) and connects to other nodes via TCP. Nodes discover each other through bootstrap peers and a distributed hash table (DHT).

**Skill**: A named function registered on a node. Skills have typed input/output schemas, a price in credits, and optional visibility controls. Any node can query the network for available skills and call them remotely.

**Credit**: Knarr uses bilateral credit — each node tracks its own balance with every peer it interacts with. When you provide a skill, your balance with the caller increases. When you consume a skill, your balance with the provider decreases. No global ledger, no blockchain, no tokens. Just pairwise accounting.

**Mail**: Nodes can send structured messages to each other. Mail is store-and-forward — if the recipient is offline, the message is queued and delivered when they reconnect. Mail supports attachments via sidecar assets.

**Sidecar**: A local asset store attached to each node. Files (documents, audio, images) are stored as content-addressed blobs. Skills can read and write sidecar assets. Assets can be referenced in mail attachments.

## Design Philosophy

- **No gatekeepers.** Any node can join the network, offer skills, and consume skills. There is no approval process, no app store, no platform owner.
- **Receipts for everything.** Every skill execution, credit transfer, and mail delivery produces a cryptographic receipt. The audit trail is built into the protocol.
- **Bring your ticket.** Callers pay for what they use. Providers set their own prices. The credit system is bilateral — no intermediary takes a cut.
- **Everyone has a price.** There is no blacklist. If a node is willing to pay the price, the skill executes. Providers control access through pricing and credit policy, not bans.
- **Skills are the primitive.** Everything on the network is a skill call. Mail delivery is a skill. Asset storage is a skill. If it computes, it's a skill.

## Architecture Overview

A knarr node consists of several subsystems:

- **DHT / Discovery**: Peer discovery, skill announcements, gossip protocol
- **Transport**: TCP with length-prefixed JSON, Ed25519 message signing
- **Commerce**: Bilateral ledger, credit policy, pricing, group policies
- **Mail**: Store-and-forward messaging, attachments, sessions
- **Plugins**: Extensible hook system for custom behavior (firewalls, agents, etc.)
- **Cockpit**: Local HTTP API for node management, skill execution, and monitoring
- **Sidecar**: Content-addressed asset storage with TTL-based cleanup

## What Knarr Is Not

- **Not a blockchain.** No consensus, no mining, no tokens. Credits are bilateral accounting entries.
- **Not a marketplace.** There is no central catalog. Skills are discovered via DHT gossip.
- **Not a chatbot framework.** Knarr is infrastructure. What you build on it (chatbots, pipelines, agents) is up to you.
- **Not cloud computing.** Nodes run on your hardware, your network, your rules. There is no "knarr cloud."
