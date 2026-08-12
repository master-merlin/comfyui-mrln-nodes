"""The route table and the only code that touches `server`/`aiohttp` —
imported lazily inside register_routes() so this package always imports
cleanly headless, under pytest and outside ComfyUI.
"""

import asyncio
import json
import threading

from .. import promptlib as pl
from ..pack import logger
from .core import MAX_BODY_BYTES
from .decompose import handle_decompose
from .library import (
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
from .llm import handle_llm_pull, handle_llm_validate
from .lora import (
    handle_lora_civitai,
    handle_lora_download,
    handle_lora_meta,
    handle_lora_status,
    lora_status,
)
from .settings import handle_profile, handle_save_profile, handle_save_settings, handle_settings

ROUTES = (
    ("get", "/mrln/prompt/library", handle_library, False),
    ("get", "/mrln/prompt/template", handle_template, False),
    ("get", "/mrln/prompt/section", handle_section, False),
    ("get", "/mrln/prompt/items", handle_items, False),
    ("get", "/mrln/prompt/lora-meta", handle_lora_meta, False),
    ("get", "/mrln/prompt/lora-status", handle_lora_status, False),
    ("get", "/mrln/prompt/lora-civitai", handle_lora_civitai, False),
    ("get", "/mrln/prompt/lora-download", handle_lora_download, False),
    ("post", "/mrln/prompt/lora-download", handle_lora_download, True),
    ("get", "/mrln/prompt/settings", handle_settings, False),
    ("post", "/mrln/prompt/save-settings", handle_save_settings, True),
    ("get", "/mrln/prompt/profile", handle_profile, False),
    ("post", "/mrln/prompt/save-profile", handle_save_profile, True),
    ("get", "/mrln/prompt/llm-validate", handle_llm_validate, False),
    ("get", "/mrln/prompt/llm-pull", handle_llm_pull, False),
    ("post", "/mrln/prompt/llm-pull", handle_llm_pull, True),
    ("post", "/mrln/prompt/preview", handle_preview, True),
    ("post", "/mrln/prompt/save-section", handle_save_section, True),
    ("post", "/mrln/prompt/save-template", handle_save_template, True),
    ("post", "/mrln/prompt/delete", handle_delete, True),
    ("post", "/mrln/prompt/decompose", handle_decompose, True),
    ("get", "/mrln/prompt/export", handle_export, False),
    ("post", "/mrln/prompt/import", handle_import, True),
)


def _warm_library_caches():
    """Populate the module-level parse cache once at server boot so the
    composer's first open never pays the cold-file cost (first-touch AV
    scanning of ~170 JSON files can cost seconds on Windows)."""
    try:
        lib = pl.open_library()
        count = 0
        for slug in lib.section_slugs():
            try:
                lib.load_section(slug)
                count += 1
            except pl.PromptLibError:
                pass
        for slug in lib.template_slugs():
            try:
                lib.load_template(slug)
                count += 1
            except pl.PromptLibError:
                pass
        logger.info("MRLN prompt library warmed (%d files)", count)
        # Startup LoRA audit: a missing file otherwise only surfaces when the
        # graph already died in LoRA Apply. Report it while the user is still
        # reading the boot log, and name the AIRs that can heal themselves.
        status = lora_status(lib)
        if status["can_download"] and status["missing"]:
            gone = [row for row in status["loras"] if not row["present"]]
            healable = sum(1 for row in gone if row["air"])
            logger.warning(
                "MRLN prompt: %d of %d referenced LoRA file(s) are missing "
                "(%d carry a Civitai AIR and can be fetched from the Composer, "
                "or by the LoRA Apply node with on_missing = 'download')",
                status["missing"],
                status["total"],
                healable,
            )
            for row in gone[:10]:
                logger.warning(
                    "MRLN prompt:   missing '%s' (%s/%s)%s",
                    row["file"],
                    row["section"],
                    row["item"],
                    "" if row["air"] else " — no AIR, needs a manual file pick",
                )
            if len(gone) > 10:
                logger.warning("MRLN prompt:   … and %d more", len(gone) - 10)
    except Exception:
        logger.debug("MRLN prompt library warm-up skipped", exc_info=True)


def register_routes():
    """Attach ROUTES to the running ComfyUI server. Returns False (never
    raises) when there is no server to attach to."""
    try:
        from aiohttp import web
        from server import PromptServer
    except Exception:
        return False  # headless / pytest / outside ComfyUI: endpoints are optional
    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        return False

    def adapt(handler, reads_body):
        async def endpoint(request):
            if reads_body:
                too_large = {"error": "request body too large", "remediation": "send less data"}
                # Content-Length is a hint, not a promise: a chunked request
                # carries none, so this header check is only the cheap early
                # out. The cap that actually holds is on the bytes read below —
                # one byte past the limit is enough to refuse, so an oversized
                # body is never buffered whole (aiohttp's client_max_size is
                # ComfyUI's ~100 MB upload cap, far above our 1 MB intent).
                if (request.content_length or 0) > MAX_BODY_BYTES:
                    return web.json_response(too_large, status=413)
                raw = b""
                while len(raw) <= MAX_BODY_BYTES:
                    # read(n) hands back what is buffered NOW, not n bytes:
                    # loop to EOF or the cap, never assume one read suffices
                    chunk = await request.content.read(MAX_BODY_BYTES + 1 - len(raw))
                    if not chunk:
                        break
                    raw += chunk
                if len(raw) > MAX_BODY_BYTES:
                    return web.json_response(too_large, status=413)
                try:
                    payload = json.loads(raw)
                except Exception:
                    body = {"error": "request body is not valid JSON", "remediation": "send JSON"}
                    return web.json_response(body, status=400)
                if not isinstance(payload, dict):
                    body = {"error": "request body must be a JSON object", "remediation": ""}
                    return web.json_response(body, status=400)
            else:
                payload = dict(request.rel_url.query)
                # GET only ever polls/reads: the state-changing 'start' flag
                # rides POST JSON bodies exclusively, so a bare cross-site
                # GET (no CORS preflight) can never trigger a download
                payload.pop("start", None)
            # handlers are pure/synchronous — run them in the executor so
            # they never block (or wait behind) the busy boot-time loop
            loop = asyncio.get_running_loop()
            status, data = await loop.run_in_executor(None, handler, pl.open_library(), payload)
            return web.json_response(data, status=status)

        return endpoint

    for method, path, handler, reads_body in ROUTES:
        getattr(instance.routes, method)(path)(adapt(handler, reads_body))
    threading.Thread(target=_warm_library_caches, name="mrln-prompt-warmup", daemon=True).start()
    return True
