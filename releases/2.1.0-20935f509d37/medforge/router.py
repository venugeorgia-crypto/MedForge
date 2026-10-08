"""MedForge model router — task-aware model selection with local-first policy."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import medforge.types as T
from medforge.providers import create_provider, Provider


class ModelRouter:
    """Selects the best available model for a task, respecting local-first
    policy and hardware constraints (8 GB M1)."""
    
    def __init__(self, provider: Optional[Provider] = None) -> None:
        self._provider = provider or create_provider()
        self._model_metadata_cache: Dict[str, Dict[str, Any]] = {}
    
    @property
    def provider(self) -> Provider:
        return self._provider
    
    def _get_model_info(self, model: str) -> Dict[str, Any]:
        if model not in self._model_metadata_cache:
            self._model_metadata_cache[model] = self._provider.get_model_info(model)
        return self._model_metadata_cache[model]
    
    def _model_size_gb(self, model: str) -> float:
        info = self._get_model_info(model)
        size = info.get("size", 0)
        return size / (1024 ** 3) if size else 0.0
    
    def _model_context(self, model: str) -> int:
        info = self._get_model_info(model)
        return info.get("context_length", info.get("num_ctx", 2048))
    
    def _model_capabilities(self, model: str) -> List[str]:
        info = self._get_model_info(model)
        return info.get("capabilities", [])
    
    def _available_models(self) -> List[str]:
        return [m for m in self._provider.list_models() if m]
    
    def _filter_by_size(self, models: List[str], max_gb: Optional[float] = None) -> List[str]:
        limit = max_gb or float(os.getenv("MEDFORGE_MODEL_SIZE_GB", "3.6"))
        return [m for m in models if self._model_size_gb(m) <= limit]
    
    def select_chat_model(self, task: str = "general") -> str:
        """Select best chat model for a task.
        
        Tasks:
        - "teaching": needs large context, good instruction following
        - "grading": needs reliability, deterministic output
        - "generation": needs creativity within constraints
        - "assessment": needs structured output, reasoning
        - "reasoning": needs thinking/reasoning capability
        - "general": balanced default
        """
        # Explicit override
        explicit = os.getenv("MEDFORGE_CHAT_MODEL")
        if explicit and explicit in self._available_models():
            return explicit
        
        models = self._filter_by_size(
            [m for m in self._available_models() if self._provider.supports_chat()]
        )
        if not models:
            raise RuntimeError("No suitable chat model available.")
        
        # Task-specific preferences
        if task in ("teaching", "generation"):
            # Prefer larger context window
            models.sort(key=lambda m: self._model_context(m), reverse=True)
        elif task in ("grading", "assessment"):
            # Prefer smaller, faster, more deterministic models
            models.sort(key=lambda m: self._model_size_gb(m))
        elif task == "reasoning":
            # Must have thinking capability
            models = [m for m in models if "thinking" in self._model_capabilities(m)]
            if not models:
                # Fall back to largest context
                models = self._filter_by_size(
                    [m for m in self._available_models() if self._provider.supports_chat()]
                )
                models.sort(key=lambda m: self._model_context(m), reverse=True)
        
        # Prefer configured preferred model if available
        if T.PREFERRED_CHAT and T.PREFERRED_CHAT in models:
            return T.PREFERRED_CHAT
        
        return models[0]
    
    def select_embed_model(self) -> str:
        """Select embedding model."""
        explicit = os.getenv("MEDFORGE_EMBED_MODEL")
        if explicit and explicit in self._available_models():
            return explicit
        
        models = self._filter_by_size(
            [m for m in self._available_models() if self._provider.supports_embeddings()]
        )
        if T.EMBED_MODEL in models:
            return T.EMBED_MODEL
        if models:
            return models[0]
        raise RuntimeError("No suitable embedding model available.")
    
    def select_reasoning_model(self) -> str:
        """Select model with reasoning/thinking capability."""
        return self.select_chat_model(task="reasoning")
    
    def get_model_metadata(self, model: str) -> Dict[str, Any]:
        """Get full metadata for a model including provider info."""
        info = self._get_model_info(model)
        return {
            "model": model,
            "provider": self._provider.provider_id,
            "provider_display": self._provider.display_name,
            "size_gb": self._model_size_gb(model),
            "context": self._model_context(model),
            "capabilities": self._model_capabilities(model),
            "info": info,
        }
    
    def record_model_run(
        self,
        task: str,
        model: str,
        content_id: Optional[str],
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        success: bool,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a model invocation for auditing."""
        import sqlite3
        from medforge.utils import utcnow
        
        metadata = self.get_model_metadata(model)
        run_id = f"run-{T.utils.uid()}" if hasattr(T, 'utils') else f"run-{int(time.time()*1000)}"
        
        # This will be persisted via the V13 migration
        record = {
            "run_id": run_id,
            "task": task,
            "model": model,
            "provider": metadata["provider"],
            "provider_display": metadata["provider_display"],
            "content_id": content_id,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "success": success,
            "error": error,
            "model_size_gb": metadata["size_gb"],
            "model_context": metadata["context"],
            "created_at": utcnow(),
        }
        return record


# Global router instance
_DEFAULT_ROUTER: Optional[ModelRouter] = None


def get_router() -> ModelRouter:
    global _DEFAULT_ROUTER
    if _DEFAULT_ROUTER is None:
        _DEFAULT_ROUTER = ModelRouter()
    return _DEFAULT_ROUTER


def reset_router() -> None:
    global _DEFAULT_ROUTER
    _DEFAULT_ROUTER = None


__all__ = [
    "ModelRouter",
    "get_router",
    "reset_router",
]


import time  # noqa: E402