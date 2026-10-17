"""Deployment-pinned Ollama residency: one context size and keep-alive for every call."""

from jarvis.llm.ollama import _pinned


def test_unset_leaves_requests_unchanged(monkeypatch):
    monkeypatch.delenv("JARVIS_OLLAMA_NUM_CTX", raising=False)
    monkeypatch.delenv("JARVIS_OLLAMA_KEEP_ALIVE", raising=False)
    payload = {"model": "m", "options": {"num_ctx": 4096}, "keep_alive": "30m"}
    assert _pinned(dict(payload, options=dict(payload["options"]))) == payload


def test_pins_context_and_keep_alive(monkeypatch):
    monkeypatch.setenv("JARVIS_OLLAMA_NUM_CTX", "8192")
    monkeypatch.setenv("JARVIS_OLLAMA_KEEP_ALIVE", "-1")
    assert _pinned({"model": "m", "options": {"num_ctx": 4096}, "keep_alive": "30m"}) == {
        "model": "m", "options": {"num_ctx": 8192}, "keep_alive": -1,
    }
    assert _pinned({"model": "m"})["options"] == {"num_ctx": 8192}


def test_embeddings_keep_their_own_context(monkeypatch):
    monkeypatch.setenv("JARVIS_OLLAMA_NUM_CTX", "8192")
    monkeypatch.setenv("JARVIS_OLLAMA_KEEP_ALIVE", "1h")
    assert _pinned({"model": "e", "prompt": "x"}, num_ctx=False) == {"model": "e", "prompt": "x", "keep_alive": "1h"}
