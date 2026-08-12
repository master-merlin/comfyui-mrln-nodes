"""HTTP API backing the Prompt Composer sidebar panel.

Two layers, deliberately separated:
- pure handlers `handle_*(lib, payload) -> (status, body_dict)` — no ComfyUI
  and no aiohttp imports, so pytest exercises them directly;
- `register_routes()` — the only code touching `server`/`aiohttp`, imported
  lazily inside the function so this module always imports cleanly
  (headless, pytest, non-ComfyUI). It soft-fails by returning False.

Security: saves go through Library.save_user only (slug validation +
parse-before-write, user tier only); no request string reaches the
filesystem any other way. JSON in, JSON out; errors are
{"error": ..., "remediation": ...} with a matching HTTP status.

Split across submodules (it was one 1.7k-line file):
  core      — ApiError, the handler guard, raw-file readers, JSON shaping,
              the atomic writer
  settings  — settings.json + profiles.json persistence and their handlers
  llm       — local/cloud chat backends, validate + model-pull endpoints
  lora      — Civitai lookups, AIR downloads, install status, item healing
  decompose — the LLM/hybrid de-compose engines
  intake    — image metadata -> extraction, then the verbatim/de-compose paths
  library   — library listing, detail, preview, saves, bundles
  routes    — the ROUTES table and register_routes()

This module IS the public surface: everything below is re-exported so
`promptapi.<name>` keeps resolving exactly as it did before the split
(private helpers included — the test suite reaches for them by name).
"""

# `promptapi.json` is a documented monkeypatch seam: a test patches
# json.dump to simulate a crash mid-write. json is a singleton module, so
# patching it here patches the very object core._write_json_atomic uses.
import json

from .core import (
    MAX_BODY_BYTES,
    ApiError,
    _factory_raw,
    _guarded,
    _kv_map,
    _pool,
    _raw_file,
    _require_str,
    _resolved_slot_json,
    _slot_detail,
    _write_json_atomic,
)
from .decompose import (
    _DECOMPOSE_SYSTEM,
    _decompose_catalog,
    _extract_json,
    _llm_decompose,
    _validate_llm_fragments,
    handle_decompose,
)
from .intake import (
    CIVITAI_IMAGES_ENDPOINT,
    MAX_IMAGE_BYTES,
    VERBATIM_RENDER,
    IntakeError,
    attach_local_files,
    build_lora_section,
    build_verbatim_template,
    civitai_image_id,
    decode_image_payload,
    decode_user_comment,
    escape_braces,
    extraction_from_candidates,
    extraction_from_civitai_item,
    extraction_from_fields,
    fetch_civitai_image,
    graph_candidates,
    handle_extract_apply,
    handle_extract_image,
    is_param_tail,
    merge_lora_resources,
    params_summary,
    parse_a1111_parameters,
    parse_civitai_resources,
    parse_param_tail,
    read_image_metadata,
    resolve_air,
    strip_lora_tags,
    workflow_candidates,
)
from .library import (
    _BUNDLE_KINDS,
    _propagate_item_renames,
    _retarget_default,
    _save,
    handle_delete,
    handle_export,
    handle_import,
    handle_items,
    handle_library,
    handle_preview,
    handle_save_section,
    handle_save_template,
    handle_section,
    handle_template,
)
from .llm import (
    _PULL_LOCK,
    _PULL_STATUS,
    CLOUD_MODEL_SUGGESTIONS,
    CLOUD_PROVIDERS,
    DEFAULT_CLOUD_MODELS,
    LMSTUDIO_SPELLINGS,
    SUGGESTED_OLLAMA_MODELS,
    _backend_url_or_raise,
    _cloud_request,
    _exc_detail,
    _post_json,
    _pull_worker,
    _reflected_text,
    handle_llm_pull,
    handle_llm_validate,
    llm_chat,
)
from .lora import (
    _AIR_RE,
    _DL_LOCK,
    _ECO_MAP,
    _HASH_CACHE,
    _HASH_LOCKS,
    _KNOWN_SECRETS,
    _LORA_DL_STATUS,
    _SAFE_SEGMENT,
    CATCHWORD_JOINER,
    _civitai_summary,
    _fetch_lora_file,
    _hash_key,
    _heal_section_lora,
    _lora_download_worker,
    _lora_items,
    _open_download,
    _remember_secret,
    _resolve_lora_file,
    _sanitize_lora_filename,
    _sanitize_subfolder,
    _scrub_secrets,
    _sha256_of,
    default_trigger_selection,
    download_lora_by_air,
    handle_lora_civitai,
    handle_lora_download,
    handle_lora_meta,
    handle_lora_status,
    lora_info,
    lora_status,
    parse_air,
    render_catchword,
    split_catchword,
    trigger_selection,
)
from .routes import ROUTES, _warm_library_caches, register_routes
from .settings import (
    _SETTINGS_LOCK,
    BACKEND_REMOTE_REMEDIATION,
    BACKEND_URL_REMEDIATION,
    DEFAULT_LMSTUDIO_URL,
    DEFAULT_OLLAMA_URL,
    LOOPBACK_HOSTS,
    BackendUrlError,
    _is_loopback_host,
    _llm_settings,
    _profiles_file,
    _read_settings,
    _settings_path,
    _validate_backend_url,
    allow_remote_backends,
    backend_url,
    handle_profile,
    handle_save_profile,
    handle_save_settings,
    handle_settings,
)
