"""JARVIS_CFG_<KEY> environment variables override config.json for deployments."""

import json

from jarvis.config import load_settings


def _config(tmp_path, monkeypatch, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))


def test_env_selects_a_remote_chat_backend_over_the_file(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"ollama_chat_model": "gemma4:26b"})
    monkeypatch.setenv("JARVIS_CFG_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("JARVIS_CFG_LLM_BASE_URL", "https://example.openai.azure.com/openai/v1")
    monkeypatch.setenv("JARVIS_CFG_LLM_API_KEY", "key")
    monkeypatch.setenv("JARVIS_CFG_LLM_CHAT_MODEL", "chat-deployment")
    monkeypatch.setenv("JARVIS_CFG_FAST_MODEL", "chat-deployment")

    cfg = load_settings()

    assert cfg.llm_provider == "openai_compatible"
    assert cfg.llm_base_url == "https://example.openai.azure.com/openai/v1"
    assert cfg.llm_api_key == "key"
    assert cfg.llm_chat_model == "chat-deployment"
    assert cfg.fast_model == "chat-deployment"


def test_json_values_are_typed_and_blank_values_are_ignored(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"planner_enabled": True, "ollama_chat_model": "gemma4:26b"})
    monkeypatch.setenv("JARVIS_CFG_PLANNER_ENABLED", "false")
    monkeypatch.setenv("JARVIS_CFG_LLM_CHAT_MODEL", "")

    cfg = load_settings()

    assert cfg.planner_enabled is False
    assert cfg.llm_chat_model == "gemma4:26b"


def test_unknown_keys_are_ignored(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {})
    monkeypatch.setenv("JARVIS_CFG_NOT_A_SETTING", "x")
    assert not hasattr(load_settings(), "not_a_setting")
