"""LLM backends — local (Ollama, LM Studio) and cloud — shared by the
Enhance node and the LLM de-composer, plus the validate/pull endpoints
behind the Settings tab and the model dropdowns.
"""

import json
import threading

from .core import ApiError, _guarded, _require_str
from .settings import (
    DEFAULT_LMSTUDIO_URL,
    DEFAULT_OLLAMA_URL,
    _llm_settings,
    _read_settings,
)

CLOUD_PROVIDERS = ("anthropic", "openai", "gemini", "openrouter")

# editable defaults — used when the model widget is empty on a cloud backend
DEFAULT_CLOUD_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "openrouter": "",  # a router needs an explicit model choice
}

# Curated pull suggestions for the Enhance node's model dropdown — small
# instruct models that rewrite prompts well. Ollama downloads a pick via
# /mrln/prompt/llm-pull. Edit freely; installed models are filtered out.
SUGGESTED_OLLAMA_MODELS = (
    "gemma3:12b",
    "gemma3:4b",
    "qwen3:14b",
    "qwen3:8b",
    "llama3.2:3b",
    "phi4:14b",
    "mistral-small:24b",
)


# Curated cloud model suggestions for the model dropdowns; the FIRST entry
# per provider is the DEFAULT_CLOUD_MODELS fallback. Edit freely.
CLOUD_MODEL_SUGGESTIONS = {
    "anthropic": ("claude-haiku-4-5-20251001", "claude-sonnet-5"),
    "openai": ("gpt-4o-mini", "gpt-4o"),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-pro"),
    "openrouter": (),
}


def _post_json(url, payload, timeout, headers=None):
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ComfyUI-MRLN-Nodes",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cloud_request(backend, key, model, system, prompt, temperature, seed, max_tokens):
    """(url, headers, payload, extract) for a cloud chat call — pure, so
    tests cover the request shapes without any network."""
    if backend == "anthropic":
        return (
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": min(float(temperature), 1.0),  # anthropic range is 0..1
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            lambda d: "".join(
                b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text"
            ),
        )
    if backend == "gemini":
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": key},
            {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": float(temperature),
                    "maxOutputTokens": max_tokens,
                    "seed": seed,
                },
            },
            lambda d: "".join(
                p.get("text", "")
                for c in (d.get("candidates") or [])[:1]
                for p in ((c.get("content") or {}).get("parts") or [])
            ),
        )
    base = (
        "https://openrouter.ai/api/v1" if backend == "openrouter" else "https://api.openai.com/v1"
    )
    return (
        f"{base}/chat/completions",
        {"Authorization": f"Bearer {key}"},
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": float(temperature),
            "seed": seed,
            "max_tokens": max_tokens,
            "stream": False,
        },
        lambda d: str(((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""),
    )


def llm_chat(
    lib,
    *,
    backend,
    model,
    system,
    prompt,
    temperature,
    seed,
    max_tokens,
    timeout,
    free_vram="after call",
):
    """One entry point for every LLM backend, local and cloud. Returns the
    raw completion text; raises RuntimeError with an actionable message on
    any failure (callers decide pass-through vs raise). Keys live in the
    user tier's settings.json and are never echoed anywhere."""
    settings = _read_settings(lib)
    llm = _llm_settings(settings)
    model = str(model or "").strip()
    if backend == "ollama":
        if not model:
            raise RuntimeError(
                "Ollama needs a model name — the node's dropdown lists installed models"
            )
        url = llm.get("ollama_url") or DEFAULT_OLLAMA_URL
        data = _post_json(
            f"{url}/api/chat",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                # 0 unloads right after the call, -1 pins the model loaded
                "keep_alive": {"after call": 0, "always keep": -1}.get(free_vram, "5m"),
                "options": {"temperature": temperature, "seed": seed, "num_predict": max_tokens},
            },
            timeout,
        )
        return str((data.get("message") or {}).get("content") or "")
    if backend == "lm studio":
        url = llm.get("lmstudio_url") or DEFAULT_LMSTUDIO_URL
        _, _, payload, extract = _cloud_request(
            "openai", "", model or "local-model", system, prompt, temperature, seed, max_tokens
        )
        return extract(_post_json(f"{url}/v1/chat/completions", payload, timeout))
    if backend not in CLOUD_PROVIDERS:
        raise RuntimeError(f"unknown backend '{backend}'")
    key = str((settings.get("llm_api_keys") or {}).get(backend) or "")
    if not key:
        raise RuntimeError(f"no {backend} API key stored — add it in the Composer's Settings tab")
    model = model or DEFAULT_CLOUD_MODELS.get(backend, "")
    if not model:
        raise RuntimeError(f"{backend} needs a model name — set the model widget")
    url, headers, payload, extract = _cloud_request(
        backend, key, model, system, prompt, temperature, seed, max_tokens
    )
    return extract(_post_json(url, payload, timeout, headers))


@_guarded
def handle_llm_validate(lib, payload):
    """Local providers: ping the backend and list installed models. Cloud
    providers: no network — answer with the stored-key state and curated
    model suggestions. Powers the green checkmarks in Settings and every
    model dropdown (Enhance node, De-compose tab)."""
    provider = _require_str(payload, "provider")
    settings = _read_settings(lib)
    llm = _llm_settings(settings)
    if provider in CLOUD_PROVIDERS:
        key_set = bool((settings.get("llm_api_keys") or {}).get(provider))
        return 200, {
            "state": "ok",
            "provider": provider,
            "models": [],
            "suggested": list(CLOUD_MODEL_SUGGESTIONS.get(provider, ())),
            "key_set": key_set,
        }
    import urllib.error
    import urllib.request

    if provider == "ollama":
        url = f"{llm.get('ollama_url') or DEFAULT_OLLAMA_URL}/api/tags"
    elif provider == "lmstudio":
        url = f"{llm.get('lmstudio_url') or DEFAULT_LMSTUDIO_URL}/v1/models"
    else:
        raise ApiError(
            f"unknown provider '{provider}' (have: ollama, lmstudio, {', '.join(CLOUD_PROVIDERS)})"
        )
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-MRLN-Nodes"})
        with urllib.request.urlopen(request, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # URLError / timeout / bad JSON — all mean "not reachable"
        return 502, {
            "error": f"{provider} unreachable at {url}: {exc}",
            "remediation": "start the server or fix the URL, then Validate again",
        }
    if provider == "ollama":
        models = sorted(m.get("name", "") for m in data.get("models") or [] if m.get("name"))
        stems = {m.split(":")[0] for m in models}
        suggested = [
            s for s in SUGGESTED_OLLAMA_MODELS if s not in models and s.split(":")[0] not in stems
        ]
    else:
        models = sorted(m.get("id", "") for m in data.get("data") or [] if m.get("id"))
        suggested = []  # LM Studio has no pull API — install via its own UI
    return 200, {"state": "ok", "provider": provider, "models": models, "suggested": suggested}


# model name -> {"status": "pulling"|"done"|"error", "detail": str}; module
# scope like _ENHANCE_CACHE — worker threads write, the poll endpoint reads.
_PULL_STATUS = {}


def _pull_worker(url, model):
    import urllib.request

    try:
        request = urllib.request.Request(
            f"{url}/api/pull",
            data=json.dumps({"model": model, "stream": False}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "ComfyUI-MRLN-Nodes"},
        )
        # a multi-GB pull is legitimately slow — generous cap, not the 5s ping
        with urllib.request.urlopen(request, timeout=3600) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _PULL_STATUS[model] = {"status": "done", "detail": str(data.get("status") or "success")}
    except Exception as exc:
        _PULL_STATUS[model] = {"status": "error", "detail": str(exc)}


@_guarded
def handle_llm_pull(lib, payload):
    """POST starts an Ollama model download in the background; GET (same
    route) polls its status. The dropdown suggestion click lands here.
    Only JSON `true` counts as start — GET query values are strings, so a
    polling (or cross-site) GET can never kick off a pull."""
    model = _require_str(payload, "model")
    if payload.get("start") is True:
        current = _PULL_STATUS.get(model)
        if current and current.get("status") == "pulling":
            return 200, {"model": model, "status": "pulling", "detail": "already running"}
        llm = _llm_settings(_read_settings(lib))
        url = llm.get("ollama_url") or DEFAULT_OLLAMA_URL
        _PULL_STATUS[model] = {"status": "pulling", "detail": ""}
        threading.Thread(target=_pull_worker, args=(url, model), daemon=True).start()
        return 200, {"model": model, "status": "pulling", "detail": "started"}
    status = _PULL_STATUS.get(model) or {"status": "unknown", "detail": "no pull started"}
    return 200, {"model": model, **status}
