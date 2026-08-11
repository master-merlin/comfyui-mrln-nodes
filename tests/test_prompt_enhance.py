"""Prompt Enhance node (Phase B): spec parsing, pass-through resilience,
and backend failure behavior — all offline (unreachable ports), no live
LLM required."""

import json

import pytest
import support
from promptlib_fixtures import build_library

from mrln import promptapi


@pytest.fixture()
def classes(tmp_path, monkeypatch):
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))
    return support.load_pack().NODE_CLASS_MAPPINGS


def _run(node, **kw):
    args = {
        "prompt": "a bright red car",
        "backend": "ollama",
        "model": "gemma3",
        "temperature": 0.2,
        "seed": 0,
        "max_tokens": 64,
        "timeout": 5,
        "free_vram": "after call",
        "on_error": "pass through",
        "llm": "",
        "system": "",
    }
    args.update(kw)
    return node.execute(**args)


def test_parse_llm_spec_and_thinking_strip():
    from mrln.nodes.prompt import _strip_thinking, parse_llm_spec

    assert parse_llm_spec("") == {}
    assert parse_llm_spec("{}") == {}
    assert parse_llm_spec('{"target": "sdxl", "system": "S"}')["system"] == "S"
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_llm_spec("{nope")
    with pytest.raises(ValueError, match="JSON object"):
        parse_llm_spec("[1]")
    assert _strip_thinking("<think>step\nstep</think>tags here") == "tags here"


def test_registered_with_tooltips(classes):
    assert "MRLN_PromptEnhance" in classes
    cls = classes["MRLN_PromptEnhance"]
    for group in cls.INPUT_TYPES().values():
        for name, spec in group.items():
            assert len(spec) == 2 and "tooltip" in spec[1], name


def test_passthrough_without_system(classes):
    node = classes["MRLN_PromptEnhance"]()
    prompt, report = _run(node)
    assert prompt == "a bright red car"
    assert "no system prompt" in report


def test_single_wire_llm_carries_prompt(classes, tmp_path):
    # the Template node's llm output is self-sufficient: {target, prompt,
    # system} — no separate prompt wire needed
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "settings.json").write_text(
        json.dumps({"llm": {"ollama_url": "http://127.0.0.1:9"}}), encoding="utf-8"
    )
    node = classes["MRLN_PromptEnhance"]()
    spec = json.dumps({"target": "sdxl", "prompt": "wired via llm", "system": "rewrite"})
    prompt, report = _run(node, prompt="", llm=spec)
    assert prompt == "wired via llm"  # pass-through of the SPEC's prompt
    assert "pass-through" in report
    # an explicit prompt input wins over the one inside the llm wire
    prompt, _ = _run(node, prompt="explicit wins", llm=spec)
    assert prompt == "explicit wins"
    # nothing anywhere -> actionable pass-through, never a crash
    prompt, report = _run(node, prompt="", llm="")
    assert prompt == "" and "nothing to enhance" in report


def test_unreachable_backend_passes_through_or_raises(classes, tmp_path):
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "settings.json").write_text(
        json.dumps(
            {"llm": {"ollama_url": "http://127.0.0.1:9", "lmstudio_url": "http://127.0.0.1:9"}}
        ),
        encoding="utf-8",
    )
    node = classes["MRLN_PromptEnhance"]()
    prompt, report = _run(node, system="rewrite for sdxl")
    assert prompt == "a bright red car"
    assert "pass-through" in report and "failed" in report
    with pytest.raises(RuntimeError, match="LLM enhance failed"):
        _run(node, system="rewrite for sdxl", on_error="raise")
    # missing model name for ollama is an actionable failure, not a crash
    prompt, report = _run(node, model="", system="rewrite")
    assert prompt == "a bright red car" and "model" in report


def test_validate_inputs_llm_json(classes):
    cls = classes["MRLN_PromptEnhance"]
    assert cls.VALIDATE_INPUTS(llm="") is True
    assert cls.VALIDATE_INPUTS(llm='{"target": "sdxl"}') is True
    assert "not valid JSON" in cls.VALIDATE_INPUTS(llm="{broken")


def test_llm_validate_endpoint(tmp_path):
    lib = build_library(tmp_path)
    status, body = promptapi.handle_llm_validate(lib, {"provider": "bogus"})
    assert status == 400 and "unknown provider" in body["error"]
    (tmp_path / "user").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user" / "settings.json").write_text(
        json.dumps({"llm": {"ollama_url": "http://127.0.0.1:9"}}), encoding="utf-8"
    )
    status, body = promptapi.handle_llm_validate(lib, {"provider": "ollama"})
    assert status == 502 and "unreachable" in body["error"]


def test_llm_pull_endpoint(tmp_path):
    import time

    lib = build_library(tmp_path)
    (tmp_path / "user").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user" / "settings.json").write_text(
        json.dumps({"llm": {"ollama_url": "http://127.0.0.1:9"}}), encoding="utf-8"
    )
    assert promptapi.handle_llm_pull(lib, {"model": ""})[0] == 400
    status, body = promptapi.handle_llm_pull(lib, {"model": "tiny-test", "start": True})
    assert status == 200 and body["status"] == "pulling"
    for _ in range(100):  # connection-refused fails fast; poll like the UI does
        status, body = promptapi.handle_llm_pull(lib, {"model": "tiny-test"})
        if body["status"] != "pulling":
            break
        time.sleep(0.05)
    assert body["status"] == "error"
    # curated dropdown suggestions exist and are pullable names
    assert promptapi.SUGGESTED_OLLAMA_MODELS
    assert all(":" in m for m in promptapi.SUGGESTED_OLLAMA_MODELS)


def test_cloud_request_shapes():
    # pure builders — request shapes verified without any network
    from mrln.promptapi import _cloud_request

    url, headers, payload, extract = _cloud_request(
        "anthropic", "K", "claude-x", "SYS", "P", 1.5, 7, 128
    )
    assert url.endswith("/v1/messages") and headers["x-api-key"] == "K"
    assert "anthropic-version" in headers
    assert payload["temperature"] == 1.0  # anthropic caps at 1
    assert payload["system"] == "SYS" and payload["max_tokens"] == 128
    canned = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    assert extract(canned) == "ab"

    url, headers, payload, extract = _cloud_request(
        "gemini", "K", "gemini-x", "SYS", "P", 0.3, 7, 128
    )
    assert "gemini-x:generateContent" in url and headers["x-goog-api-key"] == "K"
    assert payload["generationConfig"]["seed"] == 7
    assert extract({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}) == "hi"

    url, headers, _payload, _ = _cloud_request("openrouter", "K", "m", "SYS", "P", 0.3, 7, 128)
    assert url.startswith("https://openrouter.ai/") and headers["Authorization"] == "Bearer K"
    url, _, payload, extract = _cloud_request("openai", "K", "m", "SYS", "P", 0.3, 7, 128)
    assert url.startswith("https://api.openai.com/")
    assert payload["messages"][0] == {"role": "system", "content": "SYS"}
    assert extract({"choices": [{"message": {"content": "out"}}]}) == "out"


def test_cloud_backend_requires_key(classes):
    node = classes["MRLN_PromptEnhance"]()
    prompt, report = _run(node, backend="anthropic", system="rewrite")
    assert prompt == "a bright red car"
    assert "pass-through" in report and "API key" in report
    with pytest.raises(RuntimeError, match="API key"):
        _run(node, backend="gemini", system="rewrite", on_error="raise")


def test_llm_chat_unknown_backend(tmp_path):
    lib = build_library(tmp_path)
    with pytest.raises(RuntimeError, match="unknown backend"):
        promptapi.llm_chat(
            lib,
            backend="bogus",
            model="",
            system="s",
            prompt="p",
            temperature=0.2,
            seed=1,
            max_tokens=10,
            timeout=5,
        )


def test_llm_keys_roundtrip_never_echoed(tmp_path):
    lib = build_library(tmp_path)
    status, body = promptapi.handle_save_settings(lib, {"llm_api_keys": {"anthropic": "sk-SECRET"}})
    assert status == 200 and body["llm_keys_set"]["anthropic"] is True
    assert "sk-SECRET" not in json.dumps(body)
    status, body = promptapi.handle_settings(lib, {})
    assert body["llm_keys_set"]["anthropic"] is True
    assert body["llm_keys_set"]["openai"] is False
    assert "sk-SECRET" not in json.dumps(body)  # keys are NEVER echoed
    status, body = promptapi.handle_save_settings(lib, {"llm_api_keys": {"anthropic": ""}})
    assert status == 200 and body["llm_keys_set"]["anthropic"] is False
    assert promptapi.handle_save_settings(lib, {"llm_api_keys": {"bogus": "x"}})[0] == 400
    assert promptapi.handle_save_settings(lib, {"llm_api_keys": "nope"})[0] == 400


def test_settings_roundtrip_llm_urls(tmp_path):
    lib = build_library(tmp_path)
    status, body = promptapi.handle_save_settings(
        lib, {"llm": {"ollama_url": "http://10.0.0.5:11434/"}}
    )
    assert status == 200
    status, body = promptapi.handle_settings(lib, {})
    assert body["llm"]["ollama_url"] == "http://10.0.0.5:11434"  # trailing slash stripped
    assert body["llm"]["lmstudio_url"] == promptapi.DEFAULT_LMSTUDIO_URL
    assert promptapi.handle_save_settings(lib, {"llm": "nope"})[0] == 400
