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


# ── Knowledge Recipe Trust Validation ──

_ALLOWED_ACTIONS = {"log", "act"}


def validate_knowledge_recipe(config: dict,
                              errors: list = None) -> bool:
    """Validate a downloaded recipe for 'trusted' trust level.

    Returns True if safe. Populates errors list if provided.
    Uses allowlist: only 'log' and 'act' are permitted.
    """
    if errors is None:
        errors = []
    valid = True

    # Check evaluate type — must be hotwire
    eval_cfg = config.get("evaluate", {})
    eval_type = eval_cfg.get("type", "")
    if eval_type != "hotwire":
        errors.append(
            f"evaluate.type must be 'hotwire', got '{eval_type}'")
        valid = False

    # Check trigger cooldown for on_tick
    trigger = config.get("trigger", {})
    if trigger.get("type") == "on_tick":
        cooldown = int(trigger.get("cooldown_seconds",
                       config.get("filter", {}).get("cooldown_seconds", 0)))
        if cooldown < 300:
            errors.append(
                f"on_tick cooldown must be >= 300s, got {cooldown}")
            valid = False

    # Check actions — allowlist only
    actions = config.get("actions", {})
    for action_name in actions:
        if action_name not in _ALLOWED_ACTIONS:
            errors.append(
                f"action '{action_name}' not in allowlist "
                f"{sorted(_ALLOWED_ACTIONS)}")
            valid = False

    # Check default_action in evaluate
    default_action = eval_cfg.get("default_action", "")
    if default_action and default_action not in _ALLOWED_ACTIONS:
        errors.append(
            f"default_action '{default_action}' not in allowlist")
        valid = False

    return valid


def load_knowledge_recipes(knowledge_dir: str, domain: str,
                           trust_level: str, db,
                           evaluator=None) -> int:
    """Load recipes from a knowledge domain's recipes/ directory.

    Returns count of loaded recipes.
    """
    if trust_level == "none":
        return 0

    recipes_dir = os.path.join(knowledge_dir, domain, "recipes")
    if not os.path.isdir(recipes_dir):
        return 0

    count = 0
    for f in sorted(Path(recipes_dir).glob("*.toml")):
        try:
            with open(f, "rb") as fh:
                config = tomllib.load(fh)

            name = f"knowledge-{domain}-{f.stem}"
            config["_name"] = name
            config["_source"] = str(f)
            config["_knowledge_domain"] = domain

            if trust_level == "trusted":
                errors = []
                if not validate_knowledge_recipe(config, errors):
                    logger.warning(
                        f"KNOWLEDGE_RECIPE_BLOCKED domain={domain} "
                        f"recipe={f.name} reason={'; '.join(errors)}")
                    continue

            mode = config.get("mode", "automated")
            db.upsert_recipe(name, config, str(f), mode)
            logger.info(
                f"KNOWLEDGE_RECIPE_LOADED domain={domain} "
                f"recipe={name} trust={trust_level}")
            count += 1
        except Exception as e:
            logger.error(
                f"KNOWLEDGE_RECIPE_FAILED domain={domain} "
                f"recipe={f.name}: {e}")

    return count
