"""The pack-wide user directory — `<ComfyUI>/user/mrln/`.

Pack level on purpose (ECOSYSTEM E2, §5.5): this root is not the prompt
domain's. Every domain keeps its own state in `<root>/<domain>/`, and the few
things that belong to the whole pack — settings, and whatever later joins
them — live at the root itself. A domain that reaches for `user/mrln/` builds
the path from here rather than deriving one of its own.

The root is always **one level above a domain's own root**, taken from the
Library it is handed rather than from the environment. That is deliberate:
tests and the `MRLN_PROMPT_DIR` override redirect a domain root to a
temporary directory, and a root computed independently would happily write a
real API key into the real ComfyUI user directory during a test run.

Layering (E4): this is the root layer, so libraries and the HTTP layer may
import it and it imports nothing of theirs.
"""

from pathlib import Path

SETTINGS_NAME = "settings.json"


def pack_root(domain_root):
    """`user/mrln/` from a domain's own root (`user/mrln/prompt`). None when
    the caller has no user tier at all — headless with nowhere to write."""
    if domain_root is None:
        return None
    return Path(domain_root).parent


def settings_path(domain_root):
    """Where pack-wide settings are WRITTEN. See `legacy_settings_path` for
    where they may still be read from."""
    root = pack_root(domain_root)
    return None if root is None else root / SETTINGS_NAME


def legacy_settings_path(domain_root):
    """Where settings lived up to v0.1.1: inside the prompt domain's folder,
    although their content was already pack-wide (ECOSYSTEM S2, decided
    2026-08-16). Still read when the new path is absent, so an update never
    loses a stored key; the first save writes the new path and the old file
    is left untouched rather than deleted — it holds API keys, and deleting a
    user's secrets to tidy a path is not a trade this pack makes.

    Reading does not migrate: E7 says reading never mutates disk."""
    if domain_root is None:
        return None
    return Path(domain_root) / SETTINGS_NAME
