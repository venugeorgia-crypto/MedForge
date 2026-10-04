"""MedForge models: Ollama lifecycle, embeddings, chat, model selection.

Imports from medforge.types + medforge.utils only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import medforge.types as T
from medforge.utils import mkdirs, utcnow, atomic_text, sh

# Runtime state local to this module
ACTIVE_CHAT: str = ""
MODEL_INFO: Dict[str, Dict[str, Any]] = {}


def http_json(path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 300) -> Dict[str, Any]:
    """Send a JSON request to the Ollama API (loopback URLs only)."""
    endpoint = urlparse(T.OLLAMA_URL)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
        or endpoint.username
        or endpoint.password
    ):
        raise RuntimeError("This local edition requires a loopback Ollama URL, such as http://127.0.0.1:11434.")
    data = None
    headers: Dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        T.OLLAMA_URL + path, data=data, headers=headers,
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
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Could not reach Ollama at {T.OLLAMA_URL}: {e}") from e


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
    if not shutil.which("ollama"):
        raise RuntimeError("Ollama is not installed. Install/open Ollama once, then run MedForge again.")
    env = os.environ.copy()
    env.update(
        OLLAMA_HOST="127.0.0.1:11434",
        OLLAMA_NUM_PARALLEL="1",
        OLLAMA_MAX_LOADED_MODELS="1",
        OLLAMA_NO_CLOUD="1",
    )
    with open(T.LOGS / "ollama.log", "ab") as log:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    for _ in range(30):
        if ollama_alive():
            return
        time.sleep(0.5)
    raise RuntimeError("Ollama did not start automatically.")


def model_names() -> List[str]:
    """List installed Ollama model names."""
    return [m.get("name", "") for m in http_json("/api/tags", timeout=10).get("models", [])]


def model_exists(name: str, names: Iterable[str]) -> bool:
    """Check model presence, treating missing tag as :latest."""

    def canonical(value: str) -> str:
        return value if ":" in value.rsplit("/", 1)[-1] else value + ":latest"

    return any(canonical(n) == canonical(name) for n in names)


def pull_model(name: str) -> None:
    """Download a model via the Ollama CLI (respects offline mode)."""
    if T.OFFLINE:
        raise RuntimeError(f"Offline mode: required model {name} is not installed.")
    if shutil.disk_usage(T.BASE).free < 4 * 1024 ** 3:
        raise RuntimeError("Model download needs at least 4 GB free disk space.")
    print(f"  downloading Ollama model {name} ...")
    sh(["ollama", "pull", name])


def test_chat_model(name: str) -> bool:
    """Return True when the model can answer a tiny prompt locally."""
    try:
        info = http_json("/api/show", {"model": name}, timeout=30)
        MODEL_INFO[name] = info
        if info.get("remote_host") or info.get("remote_model") or "cloud" in name.lower():
            return False
        if info.get("capabilities") and "completion" not in info["capabilities"]:
            return False
        stop_model(T.EMBED_MODEL)
        payload: Dict[str, Any] = {
            "model": name,
            "messages": [{"role": "user", "content": "Reply with only OK"}],
            "stream": False,
            "keep_alive": 0,
            "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 24},
        }
        if "thinking" in info.get("capabilities", []):
            payload["think"] = False
        d = http_json("/api/chat", payload, timeout=240)
        return bool(d.get("message", {}).get("content", "").strip())
    except Exception as e:
        print(f"  Model check failed for {name}: {e}")
        return False


def ensure_models() -> str:
    """Ensure the embedding model and a working local chat model are available."""
    global ACTIVE_CHAT
    ensure_ollama_running()
    names = model_names()

    if not model_exists(T.EMBED_MODEL, names):
        pull_model(T.EMBED_MODEL)
        names = model_names()

    embed(["MedForge embedding readiness check"])
    stop_model(T.EMBED_MODEL)

    installed = http_json("/api/tags", timeout=10).get("models", [])
    sizes = {m.get("name"): int(m.get("size", 0)) for m in installed}
    candidates: List[str] = []
    if T.PREFERRED_CHAT:
        candidates.append(T.PREFERRED_CHAT)
    for n in sorted(names, key=lambda x: (0 if model_exists(T.FALLBACK_MODELS[0], [x]) else 1, sizes.get(x, 0))):
        low = n.lower()
        if "embed" not in low and "cloud" not in low and n not in candidates:
            candidates.append(n)
    for n in T.FALLBACK_MODELS:
        if n not in candidates:
            candidates.append(n)

    for candidate in candidates:
        if not model_exists(candidate, names):
            if candidate not in T.FALLBACK_MODELS:
                continue
            try:
                pull_model(candidate)
                names = model_names()
            except Exception:
                continue
        actual = next((n for n in names if model_exists(candidate, [n])), candidate)
        if sizes.get(actual, 0) > 3.6 * 1024 ** 3:
            print(f"  Skipping {actual}: too large for this 8 GB profile.")
            continue
        if test_chat_model(actual):
            ACTIVE_CHAT = actual
            atomic_text(T.BASE / "model-status.json", json.dumps({"model": actual, "tested_at": utcnow()}))
            return actual

    raise RuntimeError("MedForge could not install/test a usable Ollama chat model.")


def stop_model(name: str) -> None:
    """Unload a model to free RAM/VRAM (best effort)."""
    try:
        sh(["ollama", "stop", name], check=False, capture=True)
    except Exception:
        pass


def embed(texts: List[str]) -> List[List[float]]:
    """Generate embeddings for a list of texts via Ollama."""
    if not texts:
        return []
    if ACTIVE_CHAT:
        stop_model(ACTIVE_CHAT)
    d = http_json(
        "/api/embed",
        {"model": T.EMBED_MODEL, "input": texts, "truncate": True, "keep_alive": "60s"},
        timeout=900,
    )
    vecs = d.get("embeddings")
    if not vecs or len(vecs) != len(texts) or any(not v for v in vecs):
        raise RuntimeError("Embedding model returned no vectors.")
    return vecs


def chat(model: str, prompt: str, system: str, temperature: float = 0.15, num_ctx: int = 6144) -> str:
    """Generate a chat completion, stripping any residual reasoning block."""
    stop_model(T.EMBED_MODEL)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "keep_alive": "90s",
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": 2600},
    }
    if "thinking" in MODEL_INFO.get(model, {}).get("capabilities", []):
        payload["think"] = False
    for attempt in range(2):
        try:
            d = http_json("/api/chat", payload, timeout=1800)
            text = re.sub(r"	hink>.*?<ink>", "", d.get("message", {}).get("content", ""), flags=re.S).strip()
            if not text:
                raise RuntimeError("The model returned an empty answer.")
            if d.get("done_reason") == "length":
                raise RuntimeError("Generation reached its output limit. Use a narrower topic.")
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
]
