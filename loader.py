"""Thrall Switchboard — Recipe & prompt loader.

Reads TOML config files from recipes/ and prompts/ directories.
Files are the authoring format; DB is the runtime format.
Sentinel reload: touch knarr.reload to reload without restart.
"""

import logging
import os
from pathlib import Path
from typing import Dict, List

from db import ThrallDB
from evaluate import Evaluator

logger = logging.getLogger("thrall.loader")

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Python < 3.11


def load_recipes(recipes_dir: str, db: ThrallDB) -> int:
    """Load all .toml files from recipes/ dir into DB. Returns count."""
    path = Path(recipes_dir)
    if not path.is_dir():
        logger.warning(f"Recipes directory not found: {recipes_dir}")
        return 0

    loaded_names = []
    count = 0
    for f in sorted(path.glob("*.toml")):
        try:
            with open(f, "rb") as fh:
                config = tomllib.load(fh)
            name = f.stem  # filename without .toml
            config["_name"] = name
            config["_source"] = str(f)
            mode = config.get("mode", "automated")
            # Upsert ALL recipes (including disabled) so DB stays in sync
            # with files on disk. Engine filters disabled at match time.
            db.upsert_recipe(name, config, str(f), mode)
            loaded_names.append(name)
            if mode == "disabled":
                logger.info(f"Recipe loaded (disabled): {name}")
            else:
                logger.info(f"Recipe loaded: {name} (mode={mode})")
                count += 1
        except Exception as e:
            logger.error(f"Failed to load recipe {f.name}: {e}")

    # Prune stale DB rows for recipes no longer on disk
    pruned = db.prune_recipes(loaded_names)
    if pruned:
        logger.info(f"Pruned {pruned} stale recipe(s) from DB")

    return count


def load_prompts(prompts_dir: str, evaluator: Evaluator) -> int:
    """Load all .toml prompt files into the evaluator. Returns count."""
    path = Path(prompts_dir)
    if not path.is_dir():
        logger.warning(f"Prompts directory not found: {prompts_dir}")
        return 0

    count = 0
    for f in sorted(path.glob("*.toml")):
        try:
            with open(f, "rb") as fh:
                config = tomllib.load(fh)
            name = f.stem
            content = config.get("content", "")
            if not content:
                logger.warning(f"Prompt {f.name} has no 'content' field")
                continue
            evaluator.load_prompt(name, content)
            count += 1
        except Exception as e:
            logger.error(f"Failed to load prompt {f.name}: {e}")

    return count


def load_all(plugin_dir: str, db: ThrallDB, evaluator: Evaluator) -> dict:
    """Load recipes + prompts from plugin directory. Returns summary."""
    recipes_dir = os.path.join(plugin_dir, "recipes")
    prompts_dir = os.path.join(plugin_dir, "prompts")

    recipe_count = load_recipes(recipes_dir, db)
    prompt_count = load_prompts(prompts_dir, evaluator)

    return {"recipes": recipe_count, "prompts": prompt_count}
