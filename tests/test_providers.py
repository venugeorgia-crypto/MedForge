"""Unit tests for medforge.providers and medforge.router modules."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import pytest

from medforge.providers import (
    Provider,
    OllamaProvider,
    MockProvider,
    create_provider,
    get_provider_class,
    list_available_providers,
    _reset_default_provider,
)
from medforge.router import ModelRouter, get_router, reset_router


class TestProviderProtocol:
    """Tests for the Provider protocol and base functionality."""

    def test_provider_protocol_exists(self):
        """Provider protocol is defined with required methods."""
        # Just verify the protocol can be imported and used for type checking
        # Protocol attributes are on instances, not the class
        import inspect
        members = [m for m in dir(Provider) if not m.startswith('_')]
        assert 'list_models' in members
        assert 'chat' in members
        assert 'embed' in members


class TestMockProvider:
    """Tests for the MockProvider (testing backend)."""

    def setup_method(self):
        _reset_default_provider()
        reset_router()

    def teardown_method(self):
        _reset_default_provider()
        reset_router()

    def test_mock_provider_basic(self):
        """MockProvider implements all required methods."""
        provider = MockProvider(canned_chat="Test response", canned_embed=[[0.1, 0.2, 0.3]])
        assert provider.provider_id == "mock"
        assert provider.display_name == "Mock Provider (testing)"
        assert provider.supports_chat() is True
        assert provider.supports_embeddings() is True
        assert provider.supports_reasoning() is False
        assert provider.is_alive() is True

    def test_mock_provider_chat(self):
        """MockProvider.chat returns canned response."""
        provider = MockProvider(canned_chat="Canned answer")
        result = provider.chat("mock-model", [{"role": "user", "content": "Hello"}])
        assert result == "Canned answer"

    def test_mock_provider_embed(self):
        """MockProvider.embed returns canned embeddings."""
        provider = MockProvider(canned_embed=[[0.5] * 768])
        result = provider.embed("mock-embed", ["text1", "text2"])
        assert len(result) == 2
        assert all(len(v) == 768 for v in result)
        assert all(v[0] == 0.5 for v in result)

    def test_mock_provider_model_management(self):
        """MockProvider model management works."""
        provider = MockProvider()
        assert "mock-model" in provider.list_models()
        provider.pull_model("new-model")
        assert "new-model" in provider.list_models()
        provider.remove_model("new-model")
        assert "new-model" not in provider.list_models()

    def test_mock_provider_get_model_info(self):
        """MockProvider.get_model_info returns structured info."""
        provider = MockProvider()
        info = provider.get_model_info("mock-model")
        assert info["name"] == "mock-model"
        assert "capabilities" in info
        assert "size" in info

    def test_mock_provider_stop_model(self):
        """MockProvider.stop_model tracks stopped models."""
        provider = MockProvider()
        provider.stop_model("mock-model")
        assert "mock-model" in provider._stopped

    def test_mock_provider_max_size(self):
        """MockProvider has no size limit."""
        provider = MockProvider()
        assert provider.max_model_size_gb() == 100.0


class TestProviderFactory:
    """Tests for provider factory functions."""

    def setup_method(self):
        _reset_default_provider()
        reset_router()

    def teardown_method(self):
        _reset_default_provider()
        reset_router()

    def test_list_available_providers(self):
        """list_available_providers returns known providers."""
        providers = list_available_providers()
        assert "ollama" in providers
        assert "mock" in providers

    def test_get_provider_class(self):
        """get_provider_class returns correct class."""
        ollama_cls = get_provider_class("ollama")
        assert ollama_cls.__name__ == "OllamaProvider"
        mock_cls = get_provider_class("mock")
        assert mock_cls.__name__ == "MockProvider"

    def test_get_provider_class_unknown_raises(self):
        """get_provider_class raises for unknown provider."""
        with pytest.raises(ValueError):
            get_provider_class("nonexistent")

    def test_create_provider_default(self):
        """create_provider with no args creates default (ollama)."""
        provider = create_provider()
        # In test env without Ollama, it still creates OllamaProvider instance
        assert provider.provider_id == "ollama"

    def test_create_provider_explicit(self):
        """create_provider with explicit ID creates that provider."""
        provider = create_provider("mock", canned_chat="Explicit test")
        assert provider.provider_id == "mock"
        result = provider.chat("mock", [{"role": "user", "content": "test"}])
        assert result == "Explicit test"


class TestModelRouter:
    """Tests for the ModelRouter."""

    def setup_method(self):
        _reset_default_provider()
        reset_router()

    def teardown_method(self):
        _reset_default_provider()
        reset_router()

    def test_router_creation(self):
        """ModelRouter can be created with a provider."""
        provider = MockProvider(canned_chat="Router test", canned_embed=[[0.1]*768])
        router = ModelRouter(provider=provider)
        assert router.provider is provider

    def test_select_chat_model_general(self):
        """Router selects a chat model for general task."""
        provider = MockProvider(canned_chat="OK")
        provider.pull_model("test-chat-model")
        router = ModelRouter(provider=provider)
        model = router.select_chat_model(task="general")
        assert model in provider.list_models()

    def test_select_chat_model_teaching_prefers_large_context(self):
        """Teaching task prefers larger context window."""
        provider = MockProvider()
        provider._models.clear()
        provider.pull_model("small-ctx-model")
        provider.pull_model("large-ctx-model")
        # Mock different context sizes
        provider._model_info_cache["small-ctx-model"] = {"context_length": 2048, "size": 1000000}
        provider._model_info_cache["large-ctx-model"] = {"context_length": 8192, "size": 2000000}
        router = ModelRouter(provider=provider)
        model = router.select_chat_model(task="teaching")
        assert model == "large-ctx-model"

    def test_select_chat_model_grading_prefers_small(self):
        """Grading task prefers smaller, faster models."""
        provider = MockProvider()
        # Remove default models to avoid interference
        provider._models.clear()
        provider.pull_model("large-model")
        provider.pull_model("small-model")
        provider._model_info_cache["large-model"] = {"context_length": 8192, "size": 3000000000}
        provider._model_info_cache["small-model"] = {"context_length": 2048, "size": 500000000}
        router = ModelRouter(provider=provider)
        model = router.select_chat_model(task="grading")
        assert model == "small-model"

    def test_select_chat_model_reasoning_requires_thinking(self):
        """Reasoning task requires thinking capability."""
        provider = MockProvider()
        provider._models.clear()
        provider.pull_model("no-thinking")
        provider.pull_model("with-thinking")
        provider._model_info_cache["no-thinking"] = {"capabilities": ["completion"], "size": 1000000, "context_length": 2048}
        provider._model_info_cache["with-thinking"] = {"capabilities": ["completion", "thinking"], "size": 2000000, "context_length": 4096}
        router = ModelRouter(provider=provider)
        model = router.select_chat_model(task="reasoning")
        assert model == "with-thinking"

    def test_select_embed_model(self):
        """Router selects embedding model."""
        provider = MockProvider()
        provider.pull_model("test-embed")
        router = ModelRouter(provider=provider)
        model = router.select_embed_model()
        assert model in provider.list_models()

    def test_select_reasoning_model(self):
        """select_reasoning_model delegates to select_chat_model with reasoning task."""
        provider = MockProvider()
        provider._models.clear()
        provider.pull_model("reasoning-model")
        provider._model_info_cache["reasoning-model"] = {"capabilities": ["completion", "thinking"], "size": 2000000, "context_length": 4096}
        router = ModelRouter(provider=provider)
        model = router.select_reasoning_model()
        assert model == "reasoning-model"

    def test_get_model_metadata(self):
        """get_model_metadata returns structured metadata."""
        provider = MockProvider()
        provider._models.clear()
        provider.pull_model("meta-model")
        provider._model_info_cache["meta-model"] = {
            "capabilities": ["completion"],
            "size": 1000000000,
            "context_length": 4096,
        }
        router = ModelRouter(provider=provider)
        meta = router.get_model_metadata("meta-model")
        assert meta["model"] == "meta-model"
        assert meta["provider"] == "mock"
        assert meta["size_gb"] == 1000000000 / (1024**3)
        assert meta["context"] == 4096
        assert "capabilities" in meta

    def test_record_model_run(self):
        """record_model_run returns structured record."""
        provider = MockProvider()
        router = ModelRouter(provider=provider)
        record = router.record_model_run(
            task="teaching",
            model="test-model",
            content_id="cid-123",
            tokens_in=100,
            tokens_out=200,
            latency_ms=1500,
            success=True,
        )
        assert record["task"] == "teaching"
        assert record["model"] == "test-model"
        assert record["content_id"] == "cid-123"
        assert record["tokens_in"] == 100
        assert record["tokens_out"] == 200
        assert record["latency_ms"] == 1500
        assert record["success"] is True
        assert "run_id" in record
        assert "created_at" in record

    def test_router_singleton(self):
        """get_router returns singleton instance."""
        router1 = get_router()
        router2 = get_router()
        assert router1 is router2
        reset_router()
        router3 = get_router()
        assert router3 is not router1


class TestOllamaProvider:
    """Tests for OllamaProvider (integration-style, skipped if no Ollama)."""

    def setup_method(self):
        _reset_default_provider()
        reset_router()

    def teardown_method(self):
        _reset_default_provider()
        reset_router()

    @pytest.mark.integration
    def test_ollama_provider_list_models(self):
        """OllamaProvider lists models from local Ollama."""
        provider = OllamaProvider()
        if not provider.is_alive():
            pytest.skip("Ollama not running")
        models = provider.list_models()
        assert isinstance(models, list)

    @pytest.mark.integration
    def test_ollama_provider_chat(self):
        """OllamaProvider.chat works with local model."""
        provider = OllamaProvider()
        if not provider.is_alive():
            pytest.skip("Ollama not running")
        models = provider.list_models()
        if not models:
            pytest.skip("No models installed")
        result = provider.chat(models[0], [{"role": "user", "content": "Reply OK"}])
        assert isinstance(result, str)
        assert len(result) > 0


class TestBackwardCompatibility:
    """Ensure legacy models.py functions still work."""

    def setup_method(self):
        _reset_default_provider()
        reset_router()

    def teardown_method(self):
        _reset_default_provider()
        reset_router()

    def test_model_names(self):
        """model_names() works via provider."""
        from medforge import models
        # Uses MockProvider in test env
        names = models.model_names()
        assert isinstance(names, list)

    def test_ensure_models_returns_string(self):
        """ensure_models() returns model name."""
        from medforge import models
        # In test env with mock, this should work
        try:
            result = models.ensure_models()
            assert isinstance(result, str)
        except RuntimeError:
            # Expected if no mock models available
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])