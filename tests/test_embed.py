# tests/test_embed.py
import pytest
from backends import ThrallBackend, LocalBackend, OllamaBackend, OpenAIBackend

def test_abc_has_embed():
    assert hasattr(ThrallBackend, 'embed')

def test_local_embed_not_available_without_model():
    backend = LocalBackend({"model_path": ""})
    with pytest.raises(RuntimeError, match="no model path configured"):
        backend.embed("test text")

def test_ollama_embed_method_exists():
    backend = OllamaBackend({"url": "http://localhost:11434", "model": "test"})
    assert callable(getattr(backend, 'embed', None))

def test_openai_embed_method_exists():
    backend = OpenAIBackend({"url": "http://localhost:8000/v1"}, api_key="test")
    assert callable(getattr(backend, 'embed', None))
