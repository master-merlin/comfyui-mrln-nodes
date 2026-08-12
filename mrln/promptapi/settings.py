"""User-tier persistence: settings.json (API keys, local backend URLs) and
the profiles.json overlay, plus the handlers that read and write them.
"""

import ipaddress
import json
import re
import threading
from urllib.parse import urlsplit

from .. import promptlib as pl
from .core import ApiError, _guarded, _require_str, _write_json_atomic

# settings.json and profiles.json are read-modify-write and the handlers
# run on the executor thread pool — one lock so two overlapping saves
# (e.g. the Settings tab's per-provider Save buttons) never drop a write
_SETTINGS_LOCK = threading.Lock()

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_LMSTUDIO_URL = "http://127.0.0.1:1234"

# SSRF hardening: the LLM backend URLs are FETCHED BY THE SERVER, so an
# attacker-supplied value would make ComfyUI probe whatever the attacker
# names. Two gates, both in _validate_backend_url below:
#   shape   — plain http(s), a host, no credentials, no query/fragment;
#   reach   — loopback only unless llm.allow_remote is explicitly enabled.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

BACKEND_URL_REMEDIATION = (
    "use a plain http:// or https:// URL with a host and no credentials, "
    "e.g. http://127.0.0.1:11434"
)
BACKEND_REMOTE_REMEDIATION = (
    "enable 'allow_remote' in the Composer's Settings tab (LLM section) to use "
    "an LLM backend on another machine — only do that on a trusted network"
)


class BackendUrlError(ApiError):
    """A refused LLM backend URL. Carries its own remediation so save,
    validate, pull and chat all tell the user the same actionable thing —
    especially that `allow_remote` is what unblocks a legitimate LAN box."""

    def __init__(self, message, remediation=BACKEND_URL_REMEDIATION):
        super().__init__(message)
        self.remediation = remediation

    def body(self):
        """The {"error", "remediation"} shape every handler returns."""
        return {"error": str(self), "remediation": self.remediation}


def _is_loopback_host(host):
    """True for this machine only. urlsplit() hands back IPv6 hosts without
    their brackets, so `::1` arrives bare."""
    host = (host or "").strip().strip("[]").lower()
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a name we do not know is not loopback


def _validate_backend_url(value, *, key="url", allow_remote=False):
    """Parse a local LLM backend URL and return it normalized (no trailing
    slash), or raise BackendUrlError. THE single gate — used at save time and
    again at every fetch site, so a LAN URL left behind in an old
    settings.json is refused when it is used, not just when it is written."""
    if not isinstance(value, str):
        raise BackendUrlError(f"'{key}' must be a string")
    raw = value.strip()
    if not raw:
        raise BackendUrlError(f"'{key}' must not be empty")
    try:
        parts = urlsplit(raw)
        host, _port = parts.hostname, parts.port  # .port raises on a bogus port
    except ValueError as exc:
        raise BackendUrlError(f"'{key}' is not a valid URL: {exc}") from None
    if parts.scheme not in ("http", "https"):
        raise BackendUrlError(
            f"'{key}' must be an http:// or https:// URL (got '{raw}') — "
            "other schemes (file:, gopher:, ...) are never fetched"
        )
    if not host:
        raise BackendUrlError(f"'{key}' needs a host, e.g. http://127.0.0.1:11434 (got '{raw}')")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BackendUrlError(f"'{key}' must not embed credentials (user:pass@host)")
    if parts.fragment or parts.query:
        raise BackendUrlError(f"'{key}' must not carry a query string or '#' fragment")
    if not allow_remote and not _is_loopback_host(host):
        raise BackendUrlError(
            f"'{key}' points at '{host}', which is not this machine (loopback)",
            BACKEND_REMOTE_REMEDIATION,
        )
    return raw.rstrip("/")


def allow_remote_backends(settings):
    """The llm.allow_remote flag — default False: out of the box the server
    only ever talks to an LLM backend on localhost."""
    return bool(_llm_settings(settings).get("allow_remote"))


def backend_url(settings, key, default):
    """The URL a fetch site should actually use: the stored value (or the
    packaged default) re-validated NOW. Raises BackendUrlError."""
    raw = _llm_settings(settings).get(key) or default
    remote_ok = allow_remote_backends(settings)
    return _validate_backend_url(raw, key=f"llm.{key}", allow_remote=remote_ok)


def _settings_path(lib):
    return lib.user_root / "settings.json"


def _read_settings(lib):
    if lib.user_root is None:
        return {}
    path = _settings_path(lib)
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _llm_settings(settings):
    llm = settings.get("llm")
    return llm if isinstance(llm, dict) else {}


@_guarded
def handle_settings(lib, payload):
    # secrets are NEVER echoed back — only whether one is stored; local
    # backend URLs are not secrets and round-trip for the settings UI
    from .llm import CLOUD_PROVIDERS  # lazy: llm.py reads this module at import

    settings = _read_settings(lib)
    llm = _llm_settings(settings)
    keys = settings.get("llm_api_keys") or {}
    return 200, {
        "civitai_key_set": bool(settings.get("civitai_api_key")),
        "llm": {
            "ollama_url": llm.get("ollama_url") or DEFAULT_OLLAMA_URL,
            "lmstudio_url": llm.get("lmstudio_url") or DEFAULT_LMSTUDIO_URL,
            # the stored value is echoed as-is even when it would be refused
            # now (old settings.json): the user has to SEE it to fix it
            "allow_remote": allow_remote_backends(settings),
        },
        "llm_keys_set": {p: bool(keys.get(p)) for p in CLOUD_PROVIDERS},
    }


@_guarded
def handle_save_settings(lib, payload):
    from .llm import CLOUD_PROVIDERS  # lazy: llm.py reads this module at import

    if lib.user_root is None:
        return 400, {
            "error": "no user library root configured",
            "remediation": "set MRLN_PROMPT_DIR or run inside ComfyUI",
        }
    with _SETTINGS_LOCK:
        settings = _read_settings(lib)
        if "civitai_api_key" in payload:
            raw = payload.get("civitai_api_key")
            if not isinstance(raw, str):
                raise ApiError("'civitai_api_key' must be a string")
            if raw.strip():
                settings["civitai_api_key"] = raw.strip()
            else:
                settings.pop("civitai_api_key", None)  # empty clears
        if "llm" in payload:
            raw_llm = payload.get("llm")
            if not isinstance(raw_llm, dict):
                raise ApiError("'llm' must be an object")
            llm = _llm_settings(settings)
            # parse the flag FIRST: one save may legitimately turn remote
            # backends on and set the LAN URL in the same request
            if "allow_remote" in raw_llm:
                flag = raw_llm["allow_remote"]
                if not isinstance(flag, bool):
                    raise ApiError("'llm.allow_remote' must be true or false")
                llm["allow_remote"] = flag
            remote_ok = bool(llm.get("allow_remote"))
            for key in ("ollama_url", "lmstudio_url"):
                if key in raw_llm:
                    value = raw_llm[key]
                    if not isinstance(value, str):
                        raise ApiError(f"'llm.{key}' must be a string")
                    if value.strip():
                        try:
                            llm[key] = _validate_backend_url(
                                value, key=f"llm.{key}", allow_remote=remote_ok
                            )
                        except BackendUrlError as exc:
                            # nothing is written: the whole save aborts, so an
                            # invalid URL never reaches settings.json
                            return 400, exc.body()
                    else:
                        llm.pop(key, None)  # empty reverts to the default
            settings["llm"] = llm
        if "llm_api_keys" in payload:
            raw_keys = payload.get("llm_api_keys")
            if not isinstance(raw_keys, dict):
                raise ApiError("'llm_api_keys' must be an object of provider -> key")
            keys = settings.get("llm_api_keys")
            keys = keys if isinstance(keys, dict) else {}
            for provider, value in raw_keys.items():
                if provider not in CLOUD_PROVIDERS:
                    raise ApiError(
                        f"unknown provider '{provider}' (have: {', '.join(CLOUD_PROVIDERS)})"
                    )
                if not isinstance(value, str):
                    raise ApiError(f"'llm_api_keys.{provider}' must be a string")
                if value.strip():
                    keys[provider] = value.strip()
                else:
                    keys.pop(provider, None)  # empty clears, like the Civitai key
            settings["llm_api_keys"] = keys
        lib.user_root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(_settings_path(lib), settings)
    keys = settings.get("llm_api_keys") or {}
    return 200, {
        "ok": True,
        "civitai_key_set": bool(settings.get("civitai_api_key")),
        "llm_keys_set": {p: bool(keys.get(p)) for p in CLOUD_PROVIDERS},
    }


def _profiles_file(root):
    """Raw 'profiles' map of one tier's profiles.json ({} if absent/broken)."""
    if not root:
        return {}
    path = root / "profiles.json"
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        profiles = data.get("profiles")
        return profiles if isinstance(profiles, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@_guarded
def handle_profile(lib, payload):
    name = _require_str(payload, "name")
    factory = _profiles_file(lib.factory_root).get(name)
    user = _profiles_file(lib.user_root).get(name)
    merged = lib.pack_profiles().get(name)
    if merged is None and factory is None and user is None:
        return 404, {
            "error": f"profile '{name}' not found",
            "remediation": "list names via GET /mrln/prompt/library",
        }
    return 200, {"name": name, "merged": merged or {}, "factory": factory, "user": user}


@_guarded
def handle_save_profile(lib, payload):
    """Write (or with data=null delete) a USER-tier profile entry — the
    overlay above factory profiles.json. The Composer's Profiles editor
    calls this; users need their own system prompts per target model."""
    if lib.user_root is None:
        return 400, {
            "error": "no user library root configured",
            "remediation": "set MRLN_PROMPT_DIR or run inside ComfyUI",
        }
    name = _require_str(payload, "name")
    if name == pl.STANDARD or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ApiError(
            f"profile name '{name}' must be lowercase-kebab and not 'standard' (reserved)"
        )
    data = payload.get("data")
    with _SETTINGS_LOCK:
        user_profiles = dict(_profiles_file(lib.user_root))
        if data is None:
            if name not in user_profiles:
                return 404, {
                    "error": f"no user-tier entry for profile '{name}'",
                    "remediation": "only user-tier entries can be deleted; factory "
                    "profiles are read-only",
                }
            user_profiles.pop(name)
            action = "deleted"
        else:
            if not isinstance(data, dict):
                raise ApiError("'data' must be an object (or null to delete the user entry)")
            render_over = data.get("render") or {}
            if not isinstance(render_over, dict):
                raise ApiError("'render' must be an object")
            if "format" in render_over and render_over["format"] not in pl.FORMATS:
                raise ApiError(f"unknown render format '{render_over['format']}'")
            if "text_length" in render_over and render_over["text_length"] not in pl.TEXT_LENGTHS:
                raise ApiError("unknown text_length (lengths: long, short)")
            user_profiles[name] = data
            action = "saved"
        lib.user_root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            lib.user_root / "profiles.json", {"profiles": user_profiles}, ensure_ascii=False
        )
    lib.invalidate()
    return 200, {
        "ok": True,
        "name": name,
        "action": action,
        "profiles": sorted(lib.pack_profiles()),
    }
