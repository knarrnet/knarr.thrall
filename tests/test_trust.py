# tests/test_trust.py
import pytest
from loader import validate_knowledge_recipe

def test_safe_recipe_passes():
    recipe = {
        "mode": "automated",
        "trigger": {"type": "on_tick", "cooldown_seconds": 300},
        "evaluate": {"type": "hotwire", "default_action": "act"},
        "actions": {"act": {"skill": "local-skill", "input": {}}},
    }
    assert validate_knowledge_recipe(recipe) is True

def test_wake_action_blocked():
    recipe = {
        "mode": "automated",
        "trigger": {"type": "on_tick", "cooldown_seconds": 300},
        "evaluate": {"type": "hotwire", "default_action": "wake"},
        "actions": {"wake": {}},
    }
    errors = []
    assert validate_knowledge_recipe(recipe, errors=errors) is False
    assert any("wake" in e for e in errors)

def test_llm_eval_blocked():
    recipe = {
        "mode": "automated",
        "trigger": {"type": "on_mail"},
        "evaluate": {"type": "llm", "prompt": "triage"},
        "actions": {"act": {"skill": "local-skill", "input": {}}},
    }
    errors = []
    assert validate_knowledge_recipe(recipe, errors=errors) is False
    assert any("hotwire" in e for e in errors)

def test_fast_tick_blocked():
    recipe = {
        "mode": "automated",
        "trigger": {"type": "on_tick", "cooldown_seconds": 60},
        "evaluate": {"type": "hotwire", "default_action": "act"},
        "actions": {"act": {"skill": "x", "input": {}}},
    }
    errors = []
    assert validate_knowledge_recipe(recipe, errors=errors) is False
    assert any("300" in e for e in errors)
