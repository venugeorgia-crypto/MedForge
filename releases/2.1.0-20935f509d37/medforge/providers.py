"""MedForge provider abstraction — local-first, pluggable model backends.

This module defines the Provider protocol and concrete implementations.
All providers must be synchronous, loopback-only by default, and
respect the 8 GB M1 hardware constraints.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from abc import abstractmethod
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

import medforge.types as T
from medforge.utils import mkdirs, utcnow


# ─── Provider Protocol ───

@runtime_checkable
class Provider(Protocol):
    """Local-first provider interface. All methods synchronous.
    
    Cloud providers require explicit user opt-in via env vars.
    """
    
    # Identity
    provider_id: str              # e.g. "ollama", "lmstudio", "openai"
    display_name: str             # human-readable
    
    # Capabilities
    def list_models(self) -> List[str]: ...
    def supports_chat(self) -> bool: ...
    def supports_embeddings(self) -> bool: ...
    def supports_reasoning(self) -> bool: ...
    
    # Model management
    def pull_model(self, name: str) -> None: ...
    def remove_model(self, name: str) -> None: ...
    def get_model_info(self, name: str) -> Dict[str, Any]: ...
    
    # Core operations
    def chat(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.15,
        num_ctx: int = 6144,
        num_predict: int = 2600,
        **params: Any,
    ) -> str: ...
    def embed(self, model: str, texts: List[str]) -> List[List[float]]: ...
    
    # Lifecycle
    def is_alive(self) -> bool: ...
    def stop_model(self, name: str) -> None: ...
    
    # Hardware constraints
    def max_model_size_gb(self) -> float: ...


# ─── Helpers ───

def _ollama_url() -> str:
    return T.OLLAMA_URL


def _http_json(path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 300) -> Dict[str, Any]:
    endpoint = urllib.parse.urlparse(_ollama_url())
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
        or endpoint.username
        or endpoint.password
    ):
        raise RuntimeError("This local edition requires a loopback Ollama URL.")
    data = None
    headers: Dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        _ollama_url() + path, data=data, headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            result = json.loads(r.read().decode())
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="ignore")[:500]
        except Exception:
            pass
        raise RuntimeError(f"Ollama HTTP {e.code} for {path}: {body or e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Could not reach Ollama at {_ollama_url()}: {e}") from e


# ─── Ollama Provider (default, local) ───

class OllamaProvider:
    """Ollama loopback provider — the default local backend."""
    
    provider_id = "ollama"
    display_name = "Ollama (local)"
    
    def __init__(self) -> None:
        self._model_info_cache: Dict[str, Dict[str, Any]] = {}
    
    # Capabilities
    def list_models(self) -> List[str]:
        return [m.get("name", "") for m in _http_json("/api/tags", timeout=10).get("models", [])]
    
    def supports_chat(self) -> bool:
        return True
    
    def supports_embeddings(self) -> bool:
        return True
    
    def supports_reasoning(self) -> bool:
        return True  # some models have thinking capability
    
    # Model management
    def pull_model(self, name: str) -> None:
        if T.OFFLINE:
            raise RuntimeError(f"Offline mode: required model {name} is not installed.")
        if shutil.disk_usage(T.BASE).free < 4 * 1024 ** 3:
            raise RuntimeError("Model download needs at least 4 GB free disk space.")
        print(f"  downloading Ollama model {name} ...")
        subprocess.run(["ollama", "pull", name], check=True, text=True)
    
    def remove_model(self, name: str) -> None:
        subprocess.run(["ollama", "rm", name], check=False, text=True, capture_output=True)
    
    def get_model_info(self, name: str) -> Dict[str, Any]:
        if name in self._model_info_cache:
            return self._model_info_cache[name]
        try:
            info = _http_json("/api/show", {"model": name}, timeout=30)
            self._model_info_cache[name] = info
            return info
        except Exception:
            return {}
    
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
        # Unload embeddings model to free RAM
        self.stop_model(T.EMBED_MODEL)
        
        info = self.get_model_info(model)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": "90s",
            "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
        }
        if "thinking" in info.get("capabilities", []):
            payload["think"] = False
        
        for attempt in range(2):
            try:
                d = _http_json("/api/chat", payload, timeout=1800)
                text = d.get("message", {}).get("content", "").strip()
                # Strip reasoning blocks if present
                import re
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                if not text:
                    raise RuntimeError("The model returned an empty answer.")
                if d.get("done_reason") == "length":
                    raise RuntimeError("Generation reached its output limit. Use a narrower topic.")
                return text
            except RuntimeError:
                if attempt:
                    raise
                self.stop_model(model)
                time.sleep(1)
        raise RuntimeError("Chat generation failed after retries.")
    
    def embed(self, model: str, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        self.stop_model(T.ACTIVE_CHAT)
        d = _http_json(
            "/api/embed",
            {"model": model, "input": texts, "truncate": True, "keep_alive": "60s"},
            timeout=900,
        )
        vecs = d.get("embeddings")
        if not vecs or len(vecs) != len(texts) or any(not v for v in vecs):
            raise RuntimeError("Embedding model returned no vectors.")
        return vecs
    
    # Lifecycle
    def is_alive(self) -> bool:
        try:
            _http_json("/api/tags", timeout=3)
            return True
        except Exception:
            return False
    
    def stop_model(self, name: str) -> None:
        try:
            subprocess.run(["ollama", "stop", name], check=False, capture_output=True, text=True)
        except Exception:
            pass
    
    def max_model_size_gb(self) -> float:
        return 3.6  # 8 GB M1 profile limit


# ─── Mock Provider (testing) ───

class MockProvider:
    """Deterministic mock provider for hermetic testing."""
    
    provider_id = "mock"
    display_name = "Mock Provider (testing)"
    
    def __init__(self, canned_chat: str = "OK", canned_embed: Optional[List[List[float]]] = None) -> None:
        self._canned_chat = canned_chat
        self._canned_embed = canned_embed or [[0.1] * 768]
        self._models = {"mock-model", "mock-embed"}
        self._stopped: set = set()
        self._model_info_cache: Dict[str, Dict[str, Any]] = {}
    
    def list_models(self) -> List[str]:
        return list(self._models)
    
    def supports_chat(self) -> bool:
        return True
    
    def supports_embeddings(self) -> bool:
        return True
    
    def supports_reasoning(self) -> bool:
        return False
    
    def pull_model(self, name: str) -> None:
        self._models.add(name)
    
    def remove_model(self, name: str) -> None:
        self._models.discard(name)
    
    def get_model_info(self, name: str) -> Dict[str, Any]:
        if name in self._model_info_cache:
            return self._model_info_cache[name]
        info = {"name": name, "capabilities": ["completion"], "size": 1000000, "context_length": 2048}
        self._model_info_cache[name] = info
        return info
    
    def chat(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.15,
        num_ctx: int = 6144,
        num_predict: int = 2600,
        **params: Any,
    ) -> str:
        return self._canned_chat
    
    def embed(self, model: str, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        # Return one embedding per input text
        base = self._canned_embed[0] if self._canned_embed else [0.1] * 768
        return [list(base) for _ in texts]
    
    def is_alive(self) -> bool:
        return True
    
    def stop_model(self, name: str) -> None:
        self._stopped.add(name)
    
    def max_model_size_gb(self) -> float:
        return 100.0  # no limit in tests


# ─── Provider Registry ───

_PROVIDER_CLASSES: Dict[str, type] = {
    "ollama": OllamaProvider,
    "mock": MockProvider,
}

# OpenAI provider only loaded on explicit opt-in
try:
    if os.getenv("MEDFORGE_PROVIDER") == "openai" and os.getenv("MEDFORGE_OPENAI_API_KEY"):
        from medforge.providers_openai import OpenAIProvider  # type: ignore
        _PROVIDER_CLASSES["openai"] = OpenAIProvider
except Exception:
    pass


def get_provider_class(provider_id: str) -> type:
    """Get provider class by ID."""
    if provider_id not in _PROVIDER_CLASSES:
        raise ValueError(f"Unknown provider: {provider_id}. Available: {list(_PROVIDER_CLASSES.keys())}")
    return _PROVIDER_CLASSES[provider_id]


def create_provider(provider_id: Optional[str] = None, **kwargs: Any) -> Provider:
    """Factory function to create a provider instance."""
    pid = provider_id or os.getenv("MEDFORGE_PROVIDER", "ollama")
    cls = get_provider_class(pid)
    return cls(**kwargs)


def list_available_providers() -> List[str]:
    return list(_PROVIDER_CLASSES.keys())


# ─── Legacy compatibility (thin wrappers) ───

_DEFAULT_PROVIDER: Optional[Provider] = None
_ACTIVE_CHAT_MODEL: str = ""


def _get_default_provider() -> Provider:
    global _DEFAULT_PROVIDER
    if _DEFAULT_PROVIDER is None:
        _DEFAULT_PROVIDER = create_provider()
    return _DEFAULT_PROVIDER


def _reset_default_provider() -> None:
    global _DEFAULT_PROVIDER, _ACTIVE_CHAT_MODEL
    _DEFAULT_PROVIDER = None
    _ACTIVE_CHAT_MODEL = ""


__all__ = [
    "Provider",
    "OllamaProvider",
    "MockProvider",
    "create_provider",
    "get_provider_class",
    "list_available_providers",
    "_get_default_provider",
    "_reset_default_provider",
]


import urllib.parse  # noqa: E402 (used in _http_json)