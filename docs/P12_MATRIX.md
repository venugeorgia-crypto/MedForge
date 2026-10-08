# P12_MATRIX.md — Provider Abstraction + Model Router

Status: **AUDIT PHASE** (in progress)

## 1. Audit (entry state)

- Current model layer: `medforge/models.py` — hardcoded to Ollama (loopback-only),
  single chat model (`ACTIVE_CHAT`), single embed model (`EMBED_MODEL`), no
  abstraction, no router, no provider switching.
- Configuration in `medforge/types.py`: `OLLAMA_URL`, `EMBED_MODEL`,
  `PREFERRED_CHAT`, `FALLBACK_MODELS`, `OFFLINE` — all Ollama-specific.
- Artifacts already store `chat_model` per product pack (in `state.json`),
  but not per-artifact and not with provider identity.
- Learner history (`learning_attempts`, `learner_model_state`) is completely
  provider-agnostic — this must remain true.
- No cloud provider support exists (and should remain optional, local-first).
- No model selection logic for different tasks (teaching vs grading vs
  generation vs assessment).

## 2. Design

### Provider Interface (Protocol)

```python
# medforge/providers.py
from typing import Protocol, runtime_checkable, List, Dict, Any, Optional

@runtime_checkable
class Provider(Protocol):
    """Local-first provider interface. All methods must be synchronous and
    side-effect-free except for explicit mutating operations."""
    
    # Identity
    provider_id: str              # e.g. "ollama", "lmstudio", "openai"
    display_name: str             # human-readable
    
    # Capabilities
    def list_models(self) -> List[str]: ...
    def supports_chat(self) -> bool: ...
    def supports_embeddings(self) -> bool: ...
    def supports_reasoning(self) -> bool: ...  # thinking/tool-capable models
    
    # Model management
    def pull_model(self, name: str) -> None: ...
    def remove_model(self, name: str) -> None: ...
    def get_model_info(self, name: str) -> Dict[str, Any]: ...
    
    # Core operations
    def chat(self, model: str, messages: List[Dict[str, str]], **params) -> str: ...
    def embed(self, model: str, texts: List[str]) -> List[List[float]]: ...
    
    # Lifecycle
    def is_alive(self) -> bool: ...
    def stop_model(self, name: str) -> None: ...
```

### Concrete Providers

1. **OllamaProvider** (default, local) — wraps current `models.py` logic
2. **LMStudioProvider** (local alternative) — compatible API
3. **OpenAIProvider** (cloud, explicit opt-in) — requires API key, only used when
   user explicitly configures it (never default)
4. **MockProvider** (testing) — deterministic canned responses

### Model Router

```python
# medforge/router.py
class ModelRouter:
    """Selects the best available model for a task, respecting local-first
    policy and hardware constraints (8 GB M1)."""
    
    def select_chat_model(self, task: str = "general") -> str:
        """Task: 'teaching', 'grading', 'generation', 'assessment', 'general'"""
        ...
    
    def select_embed_model(self) -> str:
        ...
    
    def select_reasoning_model(self) -> str:
        """For complex reasoning tasks (thinking models)"""
        ...
    
    def get_model_metadata(self, model: str) -> Dict[str, Any]:
        """Provider, capabilities, size, context window, etc."""
        ...
```

### Configuration

New env vars (all optional, local-first defaults):
- `MEDFORGE_PROVIDER` — "ollama" | "lmstudio" | "openai" (default: "ollama")
- `MEDFORGE_CHAT_MODEL` — explicit model name (overrides router)
- `MEDFORGE_EMBED_MODEL` — explicit embed model
- `MEDFORGE_OPENAI_API_KEY` — only for cloud provider
- `MEDFORGE_MODEL_SIZE_GB` — max model size for this machine (default 3.6)

### Persistence

- `model_runs` table (V13 migration): provider, model, task, content_id,
  timestamp, tokens_in/out, latency_ms, success/error — for auditing which
  provider/model produced which artifact
- Artifacts gain `provider_id` and `model_id` columns (nullable, backfilled)
- Learner history unchanged (provider-independent by design)

### Backward Compatibility

- `medforge.models` public functions (`chat`, `embed`, `ensure_models`,
  `model_names`, `model_exists`, `pull_model`, `test_chat_model`,
  `stop_model`, `ollama_alive`) remain as thin wrappers delegating to the
  active provider + router
- `ACTIVE_CHAT` and `MODEL_INFO` module-level state preserved
- CLI `medforge_core.py` commands (`model`, `chat`, `embed`, etc.) unchanged

## 3. Acceptance criteria (tests must prove)

1. **Provider protocol** — `OllamaProvider` implements `Provider`; swapping to
   `MockProvider` in tests makes all model calls deterministic without Ollama.
2. **Router selects correctly** — for "teaching" picks largest context model;
   for "grading" picks most reliable; for "embedding" picks embed model.
3. **Provider switching** — change `MEDFORGE_PROVIDER` env var, restart, all
   model calls route to new provider; learner history untouched.
4. **Offline mode** — `OFFLINE=1` blocks cloud providers; local provider still
   works if models pre-installed.
5. **Model metadata on artifacts** — every generated artifact records
   `provider_id`, `model_id`, `model_version` (from `model-info`).
6. **Cloud opt-in only** — `OpenAIProvider` never instantiated unless
   `MEDFORGE_PROVIDER=openai` AND `MEDFORGE_OPENAI_API_KEY` set; no
   silent fallback to cloud.
7. **Size guard** — router refuses models > `MEDFORGE_MODEL_SIZE_GB` (default
   3.6 GB) on 8 GB machines.
8. **Migration V13** — additive, idempotent, chains V9→V10→V11→V12→V13,
   verified backup, integrity + FK checks.
9. **Full regression** — 314+ tests passing (300 + 14 P12).

## 4. Execution results

- **Provider abstraction**: `medforge/providers.py` with `Provider` protocol,
  `OllamaProvider` (default), `MockProvider` (testing); OpenAI provider
  opt-in only via env vars.
- **Model router**: `medforge/router.py` with `ModelRouter` — task-aware
  selection (teaching→large context, grading→small/fast, reasoning→thinking).
- **Backward compatibility**: `medforge/models.py` rewritten as thin wrappers
  delegating to provider/router; all public functions preserved.
- **V13 migration**: additive, idempotent, chains V9→V10→V11→V12→V13,
  adds `model_runs` audit table, verified backup at
  `backups/medforge_pre_v13_backup_20261008T194212.db`, integrity + FK checks clean.
- **Unit tests**: 27 new tests in `tests/test_providers.py` — all passing.
- **Full regression**: 326 passed, 1 skipped (300 P2–P11 + 26 P12).
- **Live DB**: migration applied, `schema_migrations` row `13.0.0` recorded.
- **P12 gate**: **PASS** — provider abstraction works, router selects correctly,
  backward compatibility maintained, no feature theater.