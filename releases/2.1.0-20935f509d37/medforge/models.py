"""MedForge models: Ollama lifecycle, embeddings, chat, model selection.

This module now delegates to the provider/router abstraction (providers.py, router.py)
while maintaining full backward compatibility for existing callers.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional

import medforge.types as T
from medforge.utils import mkdirs, utcnow, atomic_text

# Import provider/router system
from medforge.providers import (
    _get_default_provider,
    _reset_default_provider,
    OllamaProvider,
)
from medforge.router import get_router, reset_router

# Runtime state local to this module (backward compat)
ACTIVE_CHAT: str = ""
MODEL_INFO: Dict[str, Dict[str, Any]] = {}


def http_json(path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 300) -> Dict[str, Any]:
    """Send a JSON request to the Ollama API (loopback URLs only).

    Kept for backward compatibility; new code should use the provider abstraction.
    """
    provider = _get_default_provider()
    if not isinstance(provider, OllamaProvider):
        raise RuntimeError("http_json only works with OllamaProvider")
    # Access private method via the provider instance
    from medforge.providers import _http_json as _ollama_http_json
    return _ollama_http_json(path, payload, timeout)


def ollama_alive() -> bool:
    """Return True when the Ollama server answers /api/tags."""
    try:
        http_json("/api/tags", timeout=3)
        return True
    except Exception:
        return False


def ensure_ollama_running() -> None:
    """Start `ollama serve` in the background when it is not yet running."""
    mkdirs()
    if ollama_alive():
        return
    provider = _get_default_provider()
    if not isinstance(provider, OllamaProvider):
        raise RuntimeError("ensure_ollama_running only works with OllamaProvider")
    provider.is_alive()  # This will start it if needed
    # Wait for ready
    for _ in range(30):
        if ollama_alive():
            return
        time.sleep(0.5)
    raise RuntimeError("Ollama did not start automatically.")


def model_names() -> List[str]:
    """List installed Ollama model names."""
    provider = _get_default_provider()
    return provider.list_models()


def model_exists(name: str, names: Iterable[str]) -> bool:
    """Check model presence, treating missing tag as :latest."""

    def canonical(value: str) -> str:
        return value if ":" in value.rsplit("/", 1)[-1] else value + ":latest"

    return any(canonical(n) == canonical(name) for n in names)


def pull_model(name: str) -> None:
    """Download a model via the Ollama CLI (respects offline mode)."""
    provider = _get_default_provider()
    provider.pull_model(name)


def test_chat_model(name: str) -> bool:
    """Return True when the model can answer a tiny prompt locally."""
    provider = _get_default_provider()
    try:
        info = provider.get_model_info(name)
        MODEL_INFO[name] = info
        if info.get("remote_host") or info.get("remote_model") or "cloud" in name.lower():
            return False
        if info.get("capabilities") and "completion" not in info["capabilities"]:
            return False
        stop_model(T.EMBED_MODEL)
        # Use the provider's chat method directly
        result = provider.chat(
            model=name,
            messages=[{"role": "user", "content": "Reply with only OK"}],
            temperature=0,
            num_ctx=2048,
            num_predict=24,
        )
        return bool(result.strip())
    except Exception as e:
        print(f"  Model check failed for {name}: {e}")
        return False


def ensure_models() -> str:
    """Ensure the embedding model and a working local chat model are available."""
    global ACTIVE_CHAT
    ensure_ollama_running()
    router = get_router()

    # Ensure embedding model
    embed_model = router.select_embed_model()
    if embed_model not in model_names():
        pull_model(embed_model)

    # Test embeddings
    embed(["MedForge embedding readiness check"])
    stop_model(T.EMBED_MODEL)

    # Select chat model via router
    chat_model = router.select_chat_model(task="general")
    ACTIVE_CHAT = chat_model
    atomic_text(T.BASE / "model-status.json", json.dumps({"model": chat_model, "tested_at": utcnow()}))
    return chat_model


def stop_model(name: str) -> None:
    """Unload a model to free RAM/VRAM (best effort)."""
    provider = _get_default_provider()
    provider.stop_model(name)


def embed(texts: List[str]) -> List[List[float]]:
    """Generate embeddings for a list of texts via the active provider."""
    if not texts:
        return []
    router = get_router()
    embed_model = router.select_embed_model()
    provider = router.provider
    # Unload chat model to free RAM
    if ACTIVE_CHAT:
        stop_model(ACTIVE_CHAT)
    return provider.embed(embed_model, texts)


def chat(model: str, prompt: str, system: str, temperature: float = 0.15, num_ctx: int = 6144) -> str:
    """Generate a chat completion, stripping any residual reasoning block."""
    provider = _get_default_provider()
    stop_model(T.EMBED_MODEL)

    info = provider.get_model_info(model)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    for attempt in range(2):
        try:
            text = provider.chat(
                model=model,
                messages=messages,
                temperature=temperature,
                num_ctx=num_ctx,
                num_predict=2600,
            )
            if not text:
                raise RuntimeError("The model returned an empty answer.")
            return text
        except RuntimeError:
            if attempt:
                raise
            stop_model(model)
            time.sleep(1)
    raise RuntimeError("Chat generation failed after retries.")


__all__ = [
    "http_json", "ollama_alive", "ensure_ollama_running", "model_names", "model_exists",
    "pull_model", "test_chat_model", "ensure_models", "stop_model", "embed", "chat",
    "ACTIVE_CHAT", "MODEL_INFO",
]