# skills/knowledge_template.py
"""
Template for a knowledge seller skill.

Copy this file, populate the DOMAIN, VERSION, DESCRIPTION, and file paths.
Register in knarr.toml as a standard skill.

Requires: NODE.sign_bytes() (PluginContext extension — CR to Forseti).
Until that lands, use the fallback signing approach below.
"""

import hashlib
import json
import os

# Configuration — edit these
DOMAIN = "casino"
VERSION = "1.0.0"
DESCRIPTION = "Number game hosting and strategy"
KNOWLEDGE_DIR = "knowledge/casino"  # relative to skill handler
RECIPE_FILES = ["casino-player.toml"]  # optional


async def handle(input_data: dict) -> dict:
    """Returns a signed knowledge pack for the configured domain."""
    base = os.path.dirname(os.path.abspath(__file__))
    kdir = os.path.join(base, "..", KNOWLEDGE_DIR)

    # Load files
    files = {}
    for fname in os.listdir(kdir):
        if fname.endswith(".md"):
            with open(os.path.join(kdir, fname), "r", encoding="utf-8") as f:
                files[fname] = f.read()

    # Load recipes
    recipes = {}
    recipe_dir = os.path.join(kdir, "recipes")
    if os.path.isdir(recipe_dir):
        for fname in RECIPE_FILES:
            path = os.path.join(recipe_dir, fname)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    recipes[fname] = f.read()

    # Build canonical content and hash
    canonical = json.dumps({"files": files, "recipe": recipes},
                           sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(canonical.encode()).hexdigest()

    # Sign — uses NODE.sign_bytes() when available (CR pending).
    # Fallback: unsigned pack (buyer verifies hash only).
    try:
        signature, pubkey_hex = NODE.sign_bytes(content_hash.encode())
        sig_hex = signature.hex()
    except (AttributeError, NameError):
        # NODE.sign_bytes not available yet — return unsigned
        sig_hex = ""
        pubkey_hex = ""

    return {
        "domain": DOMAIN,
        "version": VERSION,
        "description": DESCRIPTION,
        "files": files,
        "recipe": recipes,
        "metadata": {
            "author": getattr(NODE, 'node_id', ''),
            "author_pubkey": pubkey_hex,
            "tags": input_data.get("tags", []),
            "sha256": content_hash,
            "signature": sig_hex,
        }
    }
