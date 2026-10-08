"""Explicit opt-in OpenAI-compatible provider (cloud — never the default).

This module only loads when the user sets BOTH:
    MEDFORGE_PROVIDER=openai
    MEDFORGE_OPENAI_API_KEY=<key>

It refuses to run in offline mode, refuses to run without a key, and never
receives learner data implicitly: callers of `chat`/`embed` decide what text to
send, exactly as they do for local providers. Local Ollama remains the default
for every phase; nothing in MedForge switches provider on its own.

The HTTP transport lives in `_post_json` so tests can inject a stub; the real
transport is `urllib.request` with a loopback-free HTTPS URL.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import medforge.types as T

OPENAI_BASE_URL = os.getenv("MEDFORGE_OPENAI_BASE", "https://api.openai.com/v1")
OPENAI_CHAT_MODEL = os.getenv("MEDFORGE_OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBED_MODEL = os.getenv("MEDFORGE_OPENAI_EMBED_MODEL", "text-embedding-3-small")


def _api_key() -> str:
    return os.getenv("MEDFORGE_OPENAI_API_KEY", "")


def _post_json(path: str, payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
    """HTTPS JSON POST to the OpenAI-compatible endpoint (injectable in tests)."""
    key = _api_key()
    if not key:
        raise RuntimeError("MEDFORGE_OPENAI_API_KEY is required for the openai provider.")
    if T.OFFLINE:
        raise RuntimeError("Offline mode: the openai provider cannot be used.")
    req = urllib.request.Request(
        OPENAI_BASE_URL.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="ignore")[:500]
        except Exception:
            pass
        raise RuntimeError(f"OpenAI HTTP {e.code}: {body or e.reason}") from e
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e}") from e


class OpenAIProvider:
    """Cloud chat/embeddings provider — explicit opt-in only."""

    provider_id = "openai"
    display_name = "OpenAI (cloud, opt-in)"

    def __init__(self, chat_model: str = OPENAI_CHAT_MODEL,
                 embed_model: str = OPENAI_EMBED_MODEL) -> None:
        self._chat_model = chat_model
        self._embed_model = embed_model

    # Capabilities
    def list_models(self) -> List[str]:
        return [self._chat_model, self._embed_model]

    def supports_chat(self) -> bool:
        return True

    def supports_embeddings(self) -> bool:
        return True

    def supports_reasoning(self) -> bool:
        return True

    # Model management (cloud models are selected, not downloaded)
    def pull_model(self, name: str) -> None:
        raise RuntimeError("Cloud models are not downloaded; set the model name instead.")

    def remove_model(self, name: str) -> None:
        raise RuntimeError("Cloud models are not stored locally.")

    def get_model_info(self, name: str) -> Dict[str, Any]:
        return {"name": name, "provider": self.provider_id,
                "capabilities": ["completion", "embedding"]}

    # Core operations
    def chat(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.15,
        num_ctx: int = 6144,
        num_predict: int = 2600,
        **params: Any,
    ) -> str:
        payload = {
            "model": model or self._chat_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": num_predict,
        }
        data = _post_json("/chat/completions", payload)
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"OpenAI response missing content: {exc}") from exc
        if not text:
            raise RuntimeError("The model returned an empty answer.")
        return text

    def embed(self, model: str, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        data = _post_json("/embeddings", {"model": model or self._embed_model,
                                          "input": texts})
        try:
            vectors = [item["embedding"] for item in data["data"]]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"OpenAI response missing embeddings: {exc}") from exc
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding model returned the wrong number of vectors.")
        return vectors

    # Lifecycle
    def is_alive(self) -> bool:
        return bool(_api_key()) and not T.OFFLINE

    def stop_model(self, name: str) -> None:
        return None  # nothing local to unload

    def max_model_size_gb(self) -> float:
        return 0.0  # no local storage


__all__ = ["OpenAIProvider", "OPENAI_BASE_URL"]
