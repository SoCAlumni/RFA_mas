"""RFA_LLM_PROVIDER (.env) picks the hosted LLM behind every alias; NVIDIA and Gemini get the same
OpenAI-shaped call and the same response shape from the egress-proxy."""

from __future__ import annotations

from pathlib import Path

import pytest

from rfa_mas.nemoclaw import config as cfg
from rfa_mas.nemoclaw.proxy import UPSTREAM_FIELDS, normalize_completion, upstream_payload


def test_default_provider_is_nvidia(monkeypatch):
    monkeypatch.delenv("RFA_LLM_PROVIDER", raising=False)
    routing = cfg.load_routing(apply_env=False)
    routing = cfg.apply_llm_provider(routing, root=Path("/nonexistent"))
    assert routing.provider == "nvidia"
    build = routing.backends["build"]
    assert build.url == "https://integrate.api.nvidia.com/v1" and build.credential_env == "NVIDIA_API_KEY"
    assert build.chat_template_kwargs == {"enable_thinking": False} and build.extras is None
    assert {a.model for a in routing.aliases.values()} == {"nvidia/nemotron-3.5-lightning-30b-a3b"}


def test_gemini_from_env_file_rewrites_every_alias(tmp_path, monkeypatch):
    monkeypatch.delenv("RFA_LLM_PROVIDER", raising=False)
    (tmp_path / ".env.dev").write_text("RFA_LLM_PROVIDER=gemini\nGEMINI_API_KEY=x\n", encoding="utf-8")
    routing = cfg.apply_llm_provider(cfg.load_routing(apply_env=False), root=tmp_path)
    assert routing.provider == "gemini"
    build = routing.backends["build"]
    assert build.url.startswith("https://generativelanguage.googleapis.com/v1beta/openai")
    assert build.credential_env == "GEMINI_API_KEY" and build.chat_template_kwargs is None
    assert build.extras == {"reasoning_effort": "low"}
    assert build.tool_call_extras == {"extra_content": {"google": {"thought_signature": "skip_thought_signature_validator"}}}
    assert {a.model for a in routing.aliases.values()} == {"gemini-3.5-flash-lite"}


def test_gemini_model_env_limited_to_the_two_offered_models(tmp_path, monkeypatch):
    monkeypatch.delenv("RFA_LLM_PROVIDER", raising=False)
    env = tmp_path / ".env.dev"
    env.write_text("RFA_LLM_PROVIDER=gemini\nGEMINI_MODEL=gemini-3.8-flash\n", encoding="utf-8")
    routing = cfg.apply_llm_provider(cfg.load_routing(apply_env=False), root=tmp_path)
    assert routing.aliases["rfa-internal"].model == "gemini-3.8-flash"
    env.write_text("RFA_LLM_PROVIDER=gemini\nGEMINI_MODEL=gemini-2.5-pro\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.apply_llm_provider(cfg.load_routing(apply_env=False), root=tmp_path)
    env.write_text("RFA_LLM_PROVIDER=nvidia\nNVIDIA_MODEL=nvidia/nemotron-3-nano-30b\n", encoding="utf-8")
    routing = cfg.apply_llm_provider(cfg.load_routing(apply_env=False), root=tmp_path)
    assert routing.aliases["rfa-internal"].model == "nvidia/nemotron-3-nano-30b"  # nvidia: any model


def test_os_environ_wins_and_model_override(tmp_path, monkeypatch):
    (tmp_path / ".env.dev").write_text("RFA_LLM_PROVIDER=nvidia\n", encoding="utf-8")
    monkeypatch.setenv("RFA_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("RFA_LLM_MODEL", "gemini-2.5-pro")
    routing = cfg.apply_llm_provider(cfg.load_routing(apply_env=False), root=tmp_path)
    assert routing.provider == "gemini" and routing.aliases["rfa-external"].model == "gemini-2.5-pro"


def test_unknown_provider_is_a_config_error(monkeypatch):
    monkeypatch.setenv("RFA_LLM_PROVIDER", "openai")
    with pytest.raises(cfg.ConfigError):
        cfg.apply_llm_provider(cfg.load_routing(apply_env=False), root=Path("/nonexistent"))


def test_upstream_payload_is_identical_for_both_providers():
    body = {"model": "rfa-auto", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            "stream_options": {"include_usage": True}, "temperature": 0.2, "tools": [{"type": "function"}],
            "reasoning": {"effort": "low"}, "metadata": {"x": 1}}
    msgs = [{"role": "user", "content": "censored"}]
    nvidia = upstream_payload(body, "m", msgs, {"enable_thinking": False}, None)
    gemini = upstream_payload(body, "m", msgs, None, {"reasoning_effort": "low"})
    assert nvidia.pop("chat_template_kwargs") == {"enable_thinking": False}
    assert gemini.pop("reasoning_effort") == "low"
    assert nvidia == gemini == {"model": "m", "messages": msgs, "stream": False, "temperature": 0.2,
                                "tools": [{"type": "function"}]}
    assert "stream_options" not in UPSTREAM_FIELDS and "reasoning" not in UPSTREAM_FIELDS


def test_replayed_tool_calls_get_the_provider_signature_bypass():
    msgs = [{"role": "user", "content": "x"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "read", "arguments": "{}"},
                 "extra_content": {"google": {"thought_signature": "real"}}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r"}]
    extras = {"extra_content": {"google": {"thought_signature": "skip_thought_signature_validator"}}}
    out = upstream_payload({}, "m", msgs, None, None, extras)["messages"]
    calls = out[1]["tool_calls"]
    assert calls[0]["extra_content"]["google"]["thought_signature"] == "skip_thought_signature_validator"
    assert calls[1]["extra_content"]["google"]["thought_signature"] == "real"  # a real signature is never replaced
    assert out[0] == msgs[0] and out[2] == msgs[2] and "extra_content" not in msgs[1]["tool_calls"][0]  # input untouched
    assert upstream_payload({}, "m", msgs, None, None, None)["messages"] == msgs  # nvidia: nothing added


def test_normalize_completion_gives_one_shape():
    gemini_like = {"id": "g1", "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                   "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": {"path": "a"}}}]},
                   "finish_reason": None}], "usage": {"prompt_tokens": 3, "completion_tokens": None}, "vendor": "extra"}
    nvidia_like = {"id": "n1", "object": "chat.completion", "created": 1, "model": "x",
                   "choices": [{"index": 0, "message": {"role": "assistant", "content": [{"type": "text", "text": "hel"}, {"type": "text", "text": "lo"}]},
                                "finish_reason": "stop", "logprobs": None}], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    g = normalize_completion(gemini_like, "m")
    n = normalize_completion(nvidia_like, "m")
    assert set(g) == set(n) == {"id", "object", "created", "model", "choices", "usage"}
    assert g["choices"][0]["message"] == {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a"}'}}]}
    assert g["choices"][0]["finish_reason"] == "tool_calls" and g["usage"] == {"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 0}
    assert n["choices"][0]["message"] == {"role": "assistant", "content": "hello"} and n["choices"][0]["finish_reason"] == "stop"
    assert "vendor" not in g and "logprobs" not in n["choices"][0]
