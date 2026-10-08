"""P15 security tests: loopback guard, SSRF allowlist, shell safety,
cloud opt-in gate, and offline refusal for the cloud provider.

These assert real behaviour of the shipped code (plus source-level invariants
where a runtime test is impossible), not documentation claims.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "current"))

import medforge.types as T
from medforge import providers as P


# ─── Local-first: the model endpoint must stay on loopback ───

class TestLoopbackGuard:
    def test_non_loopback_ollama_url_is_refused(self, monkeypatch):
        from medforge.providers import _http_json
        monkeypatch.setattr(T, "OLLAMA_URL", "http://evil.example.com:11434")
        with pytest.raises(RuntimeError) as err:
            _http_json("/api/tags", timeout=1)
        assert "loopback" in str(err.value)

    def test_ipv4_private_and_public_hosts_both_refused(self, monkeypatch):
        from medforge.providers import _http_json
        for url in ("http://10.0.0.5:11434", "http://192.168.1.10:11434",
                    "http://203.0.113.9:11434"):
            monkeypatch.setattr(T, "OLLAMA_URL", url)
            with pytest.raises(RuntimeError):
                _http_json("/api/tags", timeout=1)

    def test_scheme_and_credential_rejected(self, monkeypatch):
        from medforge.providers import _http_json
        for url in ("https://127.0.0.1:11434", "http://user:pass@127.0.0.1:11434"):
            monkeypatch.setattr(T, "OLLAMA_URL", url)
            with pytest.raises(RuntimeError):
                _http_json("/api/tags", timeout=1)


# ─── SSRF: web research must pass the allowlist before any request ───

class TestSSRFAllowlist:
    def test_http_scheme_refused(self):
        from medforge.ingestion import _resolve_and_validate
        with pytest.raises(ValueError):
            _resolve_and_validate("http://pubmed.ncbi.nlm.nih.gov/")

    def test_non_allowlisted_host_refused(self):
        from medforge.ingestion import _resolve_and_validate
        with pytest.raises(ValueError):
            _resolve_and_validate("https://evil.example.com/x")

    def test_non_443_port_refused(self):
        from medforge.ingestion import _resolve_and_validate
        with pytest.raises(ValueError):
            _resolve_and_validate("https://pubmed.ncbi.nlm.nih.gov:8443/x")

    def test_credentials_in_url_refused(self):
        from medforge.ingestion import _resolve_and_validate
        with pytest.raises(ValueError):
            _resolve_and_validate("https://user:pw@pubmed.ncbi.nlm.nih.gov/x")

    def test_lookalike_suffix_refused(self):
        from medforge.ingestion import _resolve_and_validate
        # 'ncbi.nlm.nih.gov.evil.com' must not pass a naive endswith check.
        with pytest.raises(ValueError):
            _resolve_and_validate("https://ncbi.nlm.nih.gov.evil.com/x")


# ─── Shell / subprocess safety in shipped code ───

class TestShellSafety:
    def _shipped_files(self):
        roots = [REPO / "current" / "medforge", REPO / "current" / "medforge_core.py",
                 REPO / "core" / "database"]
        for root in roots:
            if root.is_dir():
                yield from root.rglob("*.py")
            elif root.is_file():
                yield root

    def test_no_shell_true_anywhere(self):
        offenders = []
        for path in self._shipped_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "shell=True" in text:
                offenders.append(str(path))
        assert offenders == [], f"shell=True found in: {offenders}"

    def test_no_os_system_usage(self):
        offenders = []
        for path in self._shipped_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "os.system(" in text:
                offenders.append(str(path))
        assert offenders == [], f"os.system found in: {offenders}"

    def test_subprocess_calls_never_pass_string_commands(self):
        """Commands must be argument lists or list-valued variables — never a
        shell string (a literal string command is the shell-injection shape)."""
        offenders = []
        for path in self._shipped_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in ("subprocess.run(", "subprocess.Popen("):
                idx = 0
                while True:
                    idx = text.find(needle, idx)
                    if idx == -1:
                        break
                    following = text[idx + len(needle): idx + len(needle) + 1]
                    if following in ("\"", "'"):
                        offenders.append(f"{path}: {needle}{following}...")
                    idx += len(needle)
        assert offenders == [], f"shell-string subprocess calls: {offenders}"


# ─── Cloud provider gate (explicit opt-in; refuses offline) ───

class TestCloudGate:
    def _env_probe(self, extra: dict) -> str:
        env = dict(os.environ)
        env.pop("MEDFORGE_PROVIDER", None)
        env.pop("MEDFORGE_OPENAI_API_KEY", None)
        env.update(extra)
        code = (
            "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
            "from medforge.providers import list_available_providers;"
            "print(','.join(list_available_providers()))"
        ) % (str(REPO), str(REPO / "current"))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, cwd=str(REPO), check=True)
        return out.stdout.strip()

    def test_openai_not_registered_by_default(self):
        providers = self._env_probe({})
        assert "openai" not in providers.split(",")

    def test_openai_requires_key_and_opt_in(self):
        only_provider = self._env_probe({"MEDFORGE_PROVIDER": "openai"})
        assert "openai" not in only_provider.split(",")
        only_key = self._env_probe({"MEDFORGE_OPENAI_API_KEY": "sk-test"})
        assert "openai" not in only_key.split(",")
        both = self._env_probe({"MEDFORGE_PROVIDER": "openai",
                                "MEDFORGE_OPENAI_API_KEY": "sk-test"})
        assert "openai" in both.split(",")

    def test_openai_refuses_offline(self, monkeypatch):
        from medforge.providers_openai import OpenAIProvider, _post_json
        monkeypatch.setenv("MEDFORGE_OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(T, "OFFLINE", True)
        provider = OpenAIProvider()
        assert provider.is_alive() is False
        with pytest.raises(RuntimeError):
            _post_json("/chat/completions", {"model": "x", "messages": []})

    def test_openai_refuses_without_key(self, monkeypatch):
        from medforge.providers_openai import _post_json
        monkeypatch.delenv("MEDFORGE_OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError):
            _post_json("/chat/completions", {"model": "x", "messages": []})


class TestOpenAITransportSeam:
    """The cloud client is exercised through an injected transport (no network)."""

    def test_chat_parses_response(self, monkeypatch):
        from medforge import providers_openai as O
        captured = {}

        def fake_post(path, payload, timeout=120):
            captured["path"] = path
            captured["payload"] = payload
            return {"choices": [{"message": {"content": "Answer text"}}]}

        monkeypatch.setattr(O, "_post_json", fake_post)
        provider = O.OpenAIProvider()
        out = provider.chat("gpt-x", [{"role": "user", "content": "hi"}])
        assert out == "Answer text"
        assert captured["path"] == "/chat/completions"
        assert captured["payload"]["model"] == "gpt-x"
        assert captured["payload"]["messages"][0]["content"] == "hi"

    def test_embed_parses_response(self, monkeypatch):
        from medforge import providers_openai as O

        def fake_post(path, payload, timeout=120):
            return {"data": [{"embedding": [0.1, 0.2]},
                             {"embedding": [0.3, 0.4]}]}

        monkeypatch.setattr(O, "_post_json", fake_post)
        provider = O.OpenAIProvider()
        vectors = provider.embed("embed-x", ["a", "b"])
        assert vectors == [[0.1, 0.2], [0.3, 0.4]]

    def test_chat_rejects_malformed_response(self, monkeypatch):
        from medforge import providers_openai as O
        monkeypatch.setattr(O, "_post_json", lambda *a, **k: {"choices": []})
        with pytest.raises(RuntimeError):
            O.OpenAIProvider().chat("gpt-x", [{"role": "user", "content": "hi"}])

    def test_summary_of_providers_is_honest(self):
        providers = P.list_available_providers()
        assert "ollama" in providers and "mock" in providers


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
