"""Thrall Switchboard — Backend abstraction for LLM inference.

Three backends: local (llama-cpp-python), ollama (HTTP), openai (any OpenAI-compat API).
Pattern follows plugins/05-agent/llm.py but tuned for thrall constraints:
tighter timeouts, single inference slot (managed by Evaluator), cost budgeting.

All backends return raw text. The Evaluator handles JSON parsing.
"""

import asyncio
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger("thrall.backends")


class ThrallBackend(ABC):
    @abstractmethod
    async def infer(self, system_prompt: str, user_prompt: str) -> str:
        """Return raw LLM response text. Evaluator handles JSON parsing."""

    @abstractmethod
    def is_available(self) -> bool:
        """Health check — can this backend accept requests right now?"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier for logging/stats."""

    @property
    def model_name(self) -> str:
        """Human-readable model identifier."""
        return "unknown"

    def embed(self, text: str) -> list:
        """Return embedding vector as list of floats.
        Raises RuntimeError if embedding not supported by this backend.
        """
        raise RuntimeError(f"Embedding not available on {self.name} backend")


class LocalBackend(ThrallBackend):
    """llama-cpp-python backend. Lazy-loads GGUF model on first call."""

    def __init__(self, config: Dict[str, Any]):
        self._model_path = config.get("model_path", "")
        self._n_threads = int(config.get("n_threads", 4))
        self._n_ctx = int(config.get("n_ctx", 1024))
        self._max_tokens = int(config.get("max_tokens", 128))
        self._llm = None
        self._load_lock = threading.Lock()
        self._load_error: Optional[str] = None

    @property
    def name(self) -> str:
        return "local"

    @property
    def model_name(self) -> str:
        if self._model_path:
            # Extract filename from path
            return self._model_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:40]
        return "none"

    def is_available(self) -> bool:
        if self._llm is not None:
            return True
        if self._load_error:
            return False
        # Model not loaded yet but path exists — available (will lazy-load)
        return bool(self._model_path)

    def _ensure_model(self):
        if self._llm is not None:
            return
        with self._load_lock:
            if self._llm is not None:
                return
            if not self._model_path:
                self._load_error = "no model path configured"
                raise RuntimeError(self._load_error)
            try:
                from llama_cpp import Llama
                logger.info(f"Loading model: {self._model_path}")
                self._llm = Llama(
                    model_path=self._model_path,
                    n_threads=self._n_threads,
                    n_ctx=self._n_ctx,
                    verbose=False,
                )
                self._load_error = None
                logger.info("Model loaded")
            except Exception as e:
                self._load_error = str(e)
                logger.error(f"Failed to load model: {e}")
                raise

    async def infer(self, system_prompt: str, user_prompt: str) -> str:
        def _call():
            self._ensure_model()
            # gemma3 chat template requires multimodal content format
            response = self._llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
                    {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
                ],
                max_tokens=self._max_tokens,
                temperature=0.1,
            )
            return response["choices"][0]["message"]["content"]

        return await asyncio.to_thread(_call)

    def embed(self, text: str) -> list:
        """Embed text using the loaded GGUF model."""
        self._ensure_model()
        if self._llm is None:
            raise RuntimeError("Local model not available for embedding")
        result = self._llm.create_embedding(text)
        return result["data"][0]["embedding"]


class OllamaBackend(ThrallBackend):
    """HTTP backend for ollama server. Local or LAN, zero cost."""

    def __init__(self, config: Dict[str, Any]):
        self._url = config.get("url", "http://localhost:11434").rstrip("/")
        self._model = config.get("model", "gemma3:1b")
        self._temperature = float(config.get("temperature", 0.1))
        self._max_tokens = int(config.get("max_tokens", 128))
        self._num_ctx = int(config.get("num_ctx", 1024))
        self._timeout = int(config.get("timeout", 5))
        # Availability cache (avoid hammering /api/tags on every check)
        self._available_cache: Optional[bool] = None
        self._available_cache_ts: float = 0
        self._cache_ttl = 60.0  # seconds

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model_name(self) -> str:
        return self._model

    def is_available(self) -> bool:
        now = time.time()
        if self._available_cache is not None and (now - self._available_cache_ts) < self._cache_ttl:
            return self._available_cache

        try:
            req = Request(f"{self._url}/api/tags")
            resp = urlopen(req, timeout=3)
            resp.read()
            self._available_cache = True
        except Exception:
            self._available_cache = False
        self._available_cache_ts = now
        return self._available_cache

    async def infer(self, system_prompt: str, user_prompt: str) -> str:
        def _call():
            payload = json.dumps({
                "model": self._model,
                "stream": False,
                "think": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "options": {
                    "temperature": self._temperature,
                    "num_predict": self._max_tokens,
                    "num_ctx": self._num_ctx,
                },
            }).encode()

            req = Request(
                f"{self._url}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = urlopen(req, timeout=self._timeout)
            data = json.loads(resp.read())
            content = (data.get("message") or {}).get("content", "")
            # Invalidate availability cache on success
            self._available_cache = True
            self._available_cache_ts = time.time()
            return content

        return await asyncio.to_thread(_call)

    def embed(self, text: str) -> list:
        """Embed text via ollama /api/embed endpoint."""
        payload = json.dumps({
            "model": self._model,
            "input": text,
        }).encode()
        req = Request(
            f"{self._url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urlopen(req, timeout=self._timeout)
            data = json.loads(resp.read())
            embeddings = data.get("embeddings", [])
            if not embeddings:
                raise RuntimeError("Empty embedding response from ollama")
            return embeddings[0]
        except Exception as e:
            raise RuntimeError(f"Ollama embed failed: {e}") from e


class OpenAIBackend(ThrallBackend):
    """Any OpenAI-compatible API (OpenAI, Gemini via OpenAI compat, vLLM, etc.)."""

    def __init__(self, config: Dict[str, Any], api_key: str):
        self._url = config.get("url", "https://api.openai.com/v1").rstrip("/")
        self._model = config.get("model", "gpt-4o-mini")
        self._temperature = float(config.get("temperature", 0.1))
        self._max_tokens = int(config.get("max_tokens", 128))
        self._timeout = int(config.get("timeout", 10))
        self._api_key = api_key
        # Cost tracking fields (populated after each inference)
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self._embed_available = None

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self._model

    def is_available(self) -> bool:
        return bool(self._api_key)

    def _is_gemini_url(self) -> bool:
        return "generativelanguage.googleapis.com" in self._url

    async def infer(self, system_prompt: str, user_prompt: str) -> str:
        def _call():
            if self._is_gemini_url():
                return self._call_gemini(system_prompt, user_prompt)
            return self._call_openai(system_prompt, user_prompt)

        return await asyncio.to_thread(_call)

    def _probe_embedding_support(self):
        """Check if the OpenAI-compatible endpoint supports /v1/embeddings."""
        if self._embed_available is not None:
            return self._embed_available
        try:
            req = Request(
                f"{self._url}/embeddings",
                data=json.dumps({"model": self._model, "input": "probe"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
            resp = urlopen(req, timeout=5)
            data = json.loads(resp.read())
            self._embed_available = bool(data.get("data"))
        except Exception:
            self._embed_available = False
        return self._embed_available

    def embed(self, text: str) -> list:
        """Embed text via OpenAI-compatible /v1/embeddings (works with vLLM)."""
        if not self._probe_embedding_support():
            raise RuntimeError(
                f"OpenAI-compatible endpoint {self._url} does not support embeddings"
            )
        payload = json.dumps({
            "model": self._model,
            "input": text,
        }).encode()
        req = Request(
            f"{self._url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        resp = urlopen(req, timeout=self._timeout)
        data = json.loads(resp.read())
        return data["data"][0]["embedding"]

    def _call_openai(self, system_prompt: str, user_prompt: str) -> str:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
            # Disable thinking for models that support it (Qwen3.5 via vLLM)
            "chat_template_kwargs": {"enable_thinking": False},
        }
        payload = json.dumps(body).encode()

        req = Request(
            f"{self._url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        resp = urlopen(req, timeout=self._timeout)
        data = json.loads(resp.read())

        # Track usage for cost budgeting
        usage = data.get("usage", {})
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)

        return data["choices"][0]["message"]["content"]

    def _call_gemini(self, system_prompt: str, user_prompt: str) -> str:
        payload = json.dumps({
            "contents": [{"parts": [{"text": user_prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_tokens,
                "responseMimeType": "application/json",
            },
        }).encode()

        req = Request(
            f"{self._url}/models/{self._model}:generateContent?key={self._api_key}",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urlopen(req, timeout=self._timeout)
        data = json.loads(resp.read())

        # Track usage
        usage_meta = data.get("usageMetadata", {})
        self.last_prompt_tokens = usage_meta.get("promptTokenCount", 0)
        self.last_completion_tokens = usage_meta.get("candidatesTokenCount", 0)

        candidates = data.get("candidates", [])
        if not candidates:
            return json.dumps({"action": "log", "reason": "Gemini returned no candidates"})
        parts = candidates[0].get("content", {}).get("parts", [])
        return parts[0].get("text", "") if parts else ""


def create_backend(config: Dict[str, Any], vault_get=None) -> ThrallBackend:
    """Factory: create the configured thrall backend.

    config is the [config.thrall] section from plugin.toml.
    """
    backend_name = config.get("backend", "local")

    if backend_name == "local":
        return LocalBackend(config.get("local", {}))
    elif backend_name == "ollama":
        return OllamaBackend(config.get("ollama", {}))
    elif backend_name == "openai":
        api_key = ""
        openai_cfg = config.get("openai", {})
        vault_key = openai_cfg.get("api_key_vault", "")
        if vault_key and vault_get:
            try:
                api_key = vault_get(vault_key) or ""
            except Exception:
                pass
        if not api_key:
            # Fall back to plaintext key in config (dev only)
            api_key = openai_cfg.get("api_key", "")
        return OpenAIBackend(openai_cfg, api_key)
    else:
        raise ValueError(f"Unknown thrall backend: {backend_name}")
