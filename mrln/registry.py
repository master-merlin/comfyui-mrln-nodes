"""Domain registration.

Each domain lives in `mrln/nodes/<domain>.py` and exports its own
NODE_CLASS_MAPPINGS / NODE_DISPLAY_NAME_MAPPINGS (built with
`pack.build_mappings`). Activating a domain is one entry in DOMAINS.

A domain that fails to import — missing optional dependency, work in
progress — is skipped with a logged warning instead of taking the whole
pack down with it. ComfyUI drops the entire pack if this module raises,
so nothing here may raise.
"""

import importlib
import traceback

from .pack import logger

# Activate domains here — one entry per module in mrln/nodes/, load order preserved.
DOMAINS = ("prompt",)

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


def _register(domain: str) -> None:
    try:
        module = importlib.import_module(f".nodes.{domain}", package=__package__)
    except Exception:
        logger.warning(
            "MRLN Nodes: domain '%s' is unavailable and was skipped:\n%s",
            domain,
            traceback.format_exc(),
        )
        return
    for key in module.NODE_CLASS_MAPPINGS:
        if key in NODE_CLASS_MAPPINGS:
            logger.warning(
                "MRLN Nodes: duplicate node ID '%s' (domain '%s') overrides earlier one.",
                key,
                domain,
            )
    NODE_CLASS_MAPPINGS.update(module.NODE_CLASS_MAPPINGS)
    NODE_DISPLAY_NAME_MAPPINGS.update(module.NODE_DISPLAY_NAME_MAPPINGS)


def _register_api() -> None:
    """Attach HTTP endpoints when running inside ComfyUI. Doubly guarded:
    this module must never raise, and register_routes() itself soft-fails
    outside a server."""
    try:
        from . import promptapi

        if promptapi.register_routes():
            logger.info("MRLN Nodes: prompt API endpoints registered.")
    except Exception:
        logger.warning("MRLN Nodes: prompt API unavailable:\n%s", traceback.format_exc())


for _domain in DOMAINS:
    _register(_domain)

_register_api()

if not DOMAINS:
    logger.info("MRLN Nodes: no domains activated yet (harness only).")
