"""Prompt Enhance node (Phase B): spec parsing, pass-through resilience,
and backend failure behavior — all offline (unreachable ports), no live
LLM required."""

import importlib
import json
import re
import sys

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


def test_missing_system_uses_generic_contract(classes, monkeypatch):
    # 'standard' used to bypass the enhancer entirely — now a generic
    # fidelity/style-lock system prompt fills the gap and the report says so
    cls = classes["MRLN_PromptEnhance"]
    calls = _fake_chat(monkeypatch, cls, "a gleaming bright red car")
    prompt, report = _run(cls())
    assert prompt == "a gleaming bright red car"
    assert "generic system prompt" in report
    assert "FIDELITY" in calls[0]["system"] and "STYLE LOCK" in calls[0]["system"]


def test_explicit_system_beats_generic(classes, monkeypatch):
    cls = classes["MRLN_PromptEnhance"]
    calls = _fake_chat(monkeypatch, cls, "rewritten")
    _run(cls(), system="MY CONTRACT")
    assert calls[0]["system"] == "MY CONTRACT"


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


class _FakeResponse:
    """Minimal stand-in for what urlopen() yields: a context manager whose
    read() returns bytes."""

    def __init__(self, obj):
        self._body = json.dumps(obj).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(monkeypatch, obj):
    """Answer the next urllib call with `obj`; returns the URL log."""
    import urllib.request

    urls = []

    def fake(request, timeout=None):
        urls.append(request.full_url if hasattr(request, "full_url") else request)
        return _FakeResponse(obj)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return urls


def test_llm_validate_ollama_success_lists_models_and_filters_suggestions(tmp_path, monkeypatch):
    """The success branch drives every model dropdown (Enhance node,
    De-compose tab). The stem filter is the part with teeth: it must not
    re-offer a multi-GB pull of a family the user already has installed at a
    different size."""
    lib = build_library(tmp_path)
    urls = _fake_urlopen(monkeypatch, {"models": [{"name": "zzz:1b"}, {"name": "gemma3:4b"}]})
    status, body = promptapi.handle_llm_validate(lib, {"provider": "ollama"})
    assert status == 200 and body["state"] == "ok" and body["provider"] == "ollama"
    assert urls and urls[0].endswith("/api/tags")
    assert body["models"] == ["gemma3:4b", "zzz:1b"]  # sorted, not source order
    # gemma3 is installed (a different tag): the whole STEM drops out
    assert not [s for s in body["suggested"] if s.startswith("gemma3")]
    # ... while unrelated families stay offered
    assert "qwen3:8b" in body["suggested"]
    assert set(body["suggested"]) <= set(promptapi.SUGGESTED_OLLAMA_MODELS)


def test_llm_validate_ollama_exact_match_also_drops_the_suggestion(tmp_path, monkeypatch):
    lib = build_library(tmp_path)
    installed = list(promptapi.SUGGESTED_OLLAMA_MODELS)
    _fake_urlopen(monkeypatch, {"models": [{"name": name} for name in installed]})
    _status, body = promptapi.handle_llm_validate(lib, {"provider": "ollama"})
    assert body["models"] == sorted(installed)
    assert body["suggested"] == []  # everything curated is already there
    # nameless entries are skipped rather than becoming empty strings
    _fake_urlopen(monkeypatch, {"models": [{"name": ""}, {"digest": "x"}]})
    _status, body = promptapi.handle_llm_validate(lib, {"provider": "ollama"})
    assert body["models"] == []


def test_llm_validate_lmstudio_success_reads_the_openai_shape(tmp_path, monkeypatch):
    lib = build_library(tmp_path)
    urls = _fake_urlopen(monkeypatch, {"data": [{"id": "local-x"}, {"id": "a-model"}]})
    status, body = promptapi.handle_llm_validate(lib, {"provider": "lmstudio"})
    assert status == 200 and urls[0].endswith("/v1/models")
    assert body["models"] == ["a-model", "local-x"]
    assert body["suggested"] == []  # LM Studio has no pull API — never suggest


def test_llm_validate_cloud_providers_offline(tmp_path):
    # cloud providers answer WITHOUT network: key state + curated suggestions
    lib = build_library(tmp_path)
    status, body = promptapi.handle_llm_validate(lib, {"provider": "anthropic"})
    assert status == 200 and body["models"] == []
    assert body["key_set"] is False and body["suggested"]
    promptapi.handle_save_settings(lib, {"llm_api_keys": {"anthropic": "k"}})
    status, body = promptapi.handle_llm_validate(lib, {"provider": "anthropic"})
    assert body["key_set"] is True
    # the first suggestion IS the empty-model fallback — keep them aligned
    for provider, default in promptapi.DEFAULT_CLOUD_MODELS.items():
        if default:
            assert promptapi.CLOUD_MODEL_SUGGESTIONS[provider][0] == default


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


def test_effective_max_tokens_floor():
    from mrln.nodes.prompt import _effective_max_tokens

    short = "a red car at dusk"
    assert _effective_max_tokens(short, 512) == 512  # ample cap untouched
    long_prompt = "word " * 400  # ~400-word composed prompt
    floor = _effective_max_tokens(long_prompt, 512)
    assert floor > 512  # 512 would truncate a keep-everything rewrite
    assert _effective_max_tokens("word " * 20000, 512) == 8192  # hard ceiling
    assert _effective_max_tokens(short, 2048) == 2048  # explicit user cap wins


def test_enforce_protected_trigger_words():
    from mrln.nodes.prompt import _enforce_protected

    # survived verbatim -> untouched
    text, missing = _enforce_protected("a sleek BMWM4CS_G82 at dusk", ["BMWM4CS_G82"])
    assert missing == [] and text == "a sleek BMWM4CS_G82 at dusk"
    # the LLM "improved" the trigger away -> re-injected, reported
    text, missing = _enforce_protected("a sleek coupe at dusk.", ["BMWM4CS_G82"])
    assert missing == ["BMWM4CS_G82"]
    assert text == "a sleek coupe at dusk. BMWM4CS_G82"
    # tag-flow output joins with a comma; several spans in order
    text, missing = _enforce_protected("tag flow, sharp", ["TRIG_A", "TRIG_B"])
    assert text == "tag flow, sharp, TRIG_A, TRIG_B" and missing == ["TRIG_A", "TRIG_B"]
    # case mutation counts as dropped — exact characters or nothing
    text, missing = _enforce_protected("a bmwm4cs_g82 side view", ["BMWM4CS_G82"])
    assert missing == ["BMWM4CS_G82"] and text.endswith("BMWM4CS_G82")
    assert _enforce_protected("", ["X"])[0] == "X"


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
        lib, {"llm": {"ollama_url": "http://127.0.0.1:11434/"}}
    )
    assert status == 200
    status, body = promptapi.handle_settings(lib, {})
    assert body["llm"]["ollama_url"] == "http://127.0.0.1:11434"  # trailing slash stripped
    assert body["llm"]["lmstudio_url"] == promptapi.DEFAULT_LMSTUDIO_URL
    assert promptapi.handle_save_settings(lib, {"llm": "nope"})[0] == 400
    # A non-loopback backend now needs llm.allow_remote (SSRF gate, spec 2.1).
    lan = {"llm": {"ollama_url": "http://10.0.0.5:11434"}}
    assert promptapi.handle_save_settings(lib, lan)[0] == 400


# -- success path (canned llm_chat — no network) -------------------------------


def _node_api(cls):
    """The promptapi module the node's `from .. import promptapi` binds.
    The pack loads ComfyUI-style under its own package name, so this is a
    DIFFERENT module object from tests' `mrln.promptapi` — patch the one
    the node actually calls."""
    package = sys.modules[cls.__module__].__package__  # <pack>.mrln.nodes
    return importlib.import_module(package.rsplit(".", 1)[0] + ".promptapi")


def _fake_chat(monkeypatch, cls, reply):
    """monkeypatch llm_chat with a canned rewrite; returns the call log."""
    calls = []

    def fake(lib, **kwargs):
        calls.append(kwargs)
        return reply

    monkeypatch.setattr(_node_api(cls), "llm_chat", fake)
    return calls


def test_enhance_success_reenforces_protected_spans(classes, monkeypatch):
    cls = classes["MRLN_PromptEnhance"]
    calls = _fake_chat(monkeypatch, cls, "a sleek coupe at dusk.")  # trigger "improved" away
    spec = json.dumps(
        {"prompt": "photo of a BMWM4CS_G82 coupe", "system": "rewrite", "protect": ["BMWM4CS_G82"]}
    )
    text, report = _run(cls(), prompt="", llm=spec)
    assert text == "a sleek coupe at dusk. BMWM4CS_G82"  # re-appended verbatim
    assert "re-injected 1 protected span(s): BMWM4CS_G82" in report
    # the span was demanded verbatim in the system prompt sent to the LLM
    assert "PROTECTED SPANS" in calls[0]["system"] and '"BMWM4CS_G82"' in calls[0]["system"]
    assert calls[0]["prompt"] == "photo of a BMWM4CS_G82 coupe"


def test_enhance_cache_stores_enforced_text_and_skips_backend(classes, monkeypatch):
    cls = classes["MRLN_PromptEnhance"]
    calls = _fake_chat(monkeypatch, cls, "cache probe rewrite, glossy")
    spec = json.dumps(
        {"prompt": "cache probe input CACHTRIG_77", "system": "rewrite", "protect": ["CACHTRIG_77"]}
    )
    first, _ = _run(cls(), prompt="", llm=spec)
    assert first.endswith("CACHTRIG_77") and len(calls) == 1
    second, report = _run(cls(), prompt="", llm=spec)
    assert len(calls) == 1  # served from the memo cache — no second LLM call
    assert second == first  # the cache holds the ENFORCED text, not the raw rewrite
    assert "(cached)" in report


def test_enhance_seed_zero_derives_stable_seed(classes, monkeypatch):
    cls = classes["MRLN_PromptEnhance"]
    calls = _fake_chat(monkeypatch, cls, "seed probe rewrite")
    _, report_a = _run(cls(), prompt="seed probe alpha", system="rewrite", seed=0)
    _, report_b = _run(cls(), prompt="seed probe alpha", system="rewrite", seed=0)
    seed_a = int(re.search(r"seed (\d+)", report_a).group(1))
    seed_b = int(re.search(r"seed (\d+)", report_b).group(1))
    assert seed_a == seed_b != 0  # identical inputs -> identical derived seed
    assert calls[0]["seed"] == seed_a  # and the backend saw that very seed
    # a different prompt derives a different seed — derived, not constant
    _, report_c = _run(cls(), prompt="seed probe beta", system="rewrite", seed=0)
    assert int(re.search(r"seed (\d+)", report_c).group(1)) != seed_a


def test_enhance_token_cap_auto_raise_reaches_backend(classes, monkeypatch):
    from mrln.nodes.prompt import _effective_max_tokens

    cls = classes["MRLN_PromptEnhance"]
    calls = _fake_chat(monkeypatch, cls, "token cap probe rewrite")
    long_prompt = "token cap probe " + "word " * 400
    _, report = _run(cls(), prompt=long_prompt, system="rewrite", max_tokens=64)
    effective = _effective_max_tokens(long_prompt, 64)
    assert calls[0]["max_tokens"] == effective > 64  # effective cap, not the widget value
    assert f"token cap auto-raised 64→{effective}" in report


def test_enhance_protect_skips_spans_absent_from_override(classes, monkeypatch):
    # an overridden prompt input may not contain the llm wire's spans —
    # those must NOT be enforced (or appended) onto unrelated text
    cls = classes["MRLN_PromptEnhance"]
    calls = _fake_chat(monkeypatch, cls, "override rewrite, clean")
    spec = json.dumps(
        {"prompt": "original with OVTRIG_9", "system": "rewrite", "protect": ["OVTRIG_9"]}
    )
    text, report = _run(cls(), prompt="a totally different override text", llm=spec)
    assert text == "override rewrite, clean"  # nothing re-injected
    assert "OVTRIG_9" not in text and "protected" not in report
    assert "PROTECTED SPANS" not in calls[0]["system"]
